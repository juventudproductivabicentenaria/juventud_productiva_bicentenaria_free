
# -*- coding: utf-8 -*-
import sys
#from builtins import str
from datetime import datetime
from socket import AF_INET, SOCK_DGRAM, SOCK_STREAM, socket, timeout
from struct import pack, unpack
import codecs

from . import const

from .exception import ZKErrorResponse, ZKNetworkError
from .user import User

def safe_cast(val, to_type, default=None):
    #https://stackoverflow.com/questions/6330071/safe-casting-in-python
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default


def make_commkey(key, session_id, ticks=50):
    """take a password and session_id and scramble them to send to the time
    clock.
    copied from commpro.c - MakeKey"""
    key = int(key)
    session_id = int(session_id)
    k = 0
    for i in range(32):
        if (key & (1 << i)):
            k = (k << 1 | 1)
        else:
            k = k << 1
    k += session_id

    k = pack(b'I', k)
    k = unpack(b'BBBB', k)
    k = pack(
        b'BBBB',
        k[0] ^ ord('Z'),
        k[1] ^ ord('K'),
        k[2] ^ ord('S'),
        k[3] ^ ord('O'))
    k = unpack(b'HH', k)
    k = pack(b'HH', k[1], k[0])

    B = 0xff & ticks
    k = unpack(b'BBBB', k)
    k = pack(
        b'BBBB',
        k[0] ^ B,
        k[1] ^ B,
        B,
        k[3] ^ B)
    return k

class ZK_helper(object):
    """ helper class """
    def __init__(self, ip, port=4370):
        self.address = (ip, port)
        self.ip = ip
        self.port = port
        #self.timeout = timeout
        #self.password = password # passint
        #self.firmware = int(firmware) #TODO check minor version?
        #self.tcp = tcp

    def test_ping(self):
        """
        Returns True if host responds to a ping request
        """
        import subprocess, platform
        # Ping parameters as function of OS
        ping_str = "-n 1" if  platform.system().lower()=="windows" else "-c 1 -W 5"
        args = "ping " + " " + ping_str + " " + self.ip
        need_sh = False if  platform.system().lower()=="windows" else True
        # Ping
        return subprocess.call(args,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=need_sh) == 0

    def test_tcp(self):
        self.client = socket(AF_INET, SOCK_STREAM)
        self.client.settimeout(10) # fixed test
        res = self.client.connect_ex(self.address)
        self.client.close()
        return res

    def test_udp(self): # WIP:
        self.client = socket(AF_INET, SOCK_DGRAM)
        self.client.settimeout(10) # fixed test

class ZK(object):
    """ Clase ZK """
    def __init__(self, ip, port=4370, timeout=60, password=0, force_udp=False, ommit_ping=False, verbose=False, encoding='UTF-8'):
        """ initialize instance """
        self.is_connect = False
        self.is_enabled = True #let's asume
        self.helper = ZK_helper(ip, port)
        self.__address = (ip, port)
        self.__sock = socket(AF_INET, SOCK_DGRAM)
        self.__sock.settimeout(timeout)
        self.__timeout = timeout
        self.__password = password # passint
        #self.firmware = int(firmware) #dummy
        self.force_udp = force_udp
        self.ommit_ping = ommit_ping
        self.verbose = verbose
        self.encoding = encoding
        User.encoding = encoding
        self.tcp = False
        self.users = 0
        self.fingers = 0
        self.records = 0
        self.dummy = 0
        self.cards = 0
        self.fingers_cap = 0
        self.users_cap = 0
        self.rec_cap = 0
        self.faces = 0
        self.faces_cap = 0
        self.fingers_av = 0
        self.users_av = 0
        self.rec_av = 0
        self.next_uid = 1
        self.next_user_id='1'
        self.user_packet_size = 28 # default zk6
        self.end_live_capture = False
        self.__session_id = 0
        self.__reply_id = const.USHRT_MAX-1
        self.__data_recv = None
        self.__data = None

    def __nonzero__(self):
        """ for boolean test"""
        return self.is_connect

    def __create_socket(self):
        """ based on self.tcp"""
        if self.tcp:
            self.__sock = socket(AF_INET, SOCK_STREAM)
            self.__sock.settimeout(self.__timeout)
            self.__sock.connect_ex(self.__address)
        else:
            self.__sock = socket(AF_INET, SOCK_DGRAM)
            self.__sock.settimeout(self.__timeout)

    def __create_tcp_top(self, packet):
        """ witch the complete packet set top header """
        length = len(packet)
        top = pack('<HHI', const.MACHINE_PREPARE_DATA_1, const.MACHINE_PREPARE_DATA_2, length)
        return top + packet

    def __create_header(self, command, command_string, session_id, reply_id):
        '''
        Puts a the parts that make up a packet together and packs them into a byte string

        MODIFIED now, without initial checksum
        '''
        #checksum = 0 always? for calculating
        buf = pack('<4H', command, 0, session_id, reply_id) + command_string
        buf = unpack('8B' + '%sB' % len(command_string), buf)
        checksum = unpack('H', self.__create_checksum(buf))[0]
        reply_id += 1
        if reply_id >= const.USHRT_MAX:
            reply_id -= const.USHRT_MAX

        buf = pack('<4H', command, checksum, session_id, reply_id)
        return buf + command_string

    def __create_checksum(self, p):
        '''
        Calculates the checksum of the packet to be sent to the time clock
        Copied from zkemsdk.c
        '''
        l = len(p)
        checksum = 0
        while l > 1:
            checksum += unpack('H', pack('BB', p[0], p[1]))[0]
            p = p[2:]
            if checksum > const.USHRT_MAX:
                checksum -= const.USHRT_MAX
            l -= 2
        if l:
            checksum = checksum + p[-1]

        while checksum > const.USHRT_MAX:
            checksum -= const.USHRT_MAX

        checksum = ~checksum

        while checksum < 0:
            checksum += const.USHRT_MAX

        return pack('H', checksum)

    def __test_tcp_top(self, packet):
        """ return size!"""
        if len(packet)<=8:
            return 0 # invalid packet
        tcp_header = unpack('<HHI', packet[:8])
        if tcp_header[0] == const.MACHINE_PREPARE_DATA_1 and tcp_header[1] == const.MACHINE_PREPARE_DATA_2:
            return tcp_header[2]
        return 0 #never everis 0!

    def __send_command(self, command, command_string=b'', response_size=8):
        '''
        send command to the terminal
        '''
        buf = self.__create_header(command, command_string, self.__session_id, self.__reply_id)
        try:
            if self.tcp:
                top = self.__create_tcp_top(buf)
                self.__sock.send(top)
                self.__tcp_data_recv = self.__sock.recv(response_size + 8)
                self.__tcp_length = self.__test_tcp_top(self.__tcp_data_recv)
                if self.__tcp_length == 0:
                    raise ZKNetworkError("TCP packet invalid")
                self.__header = unpack('<4H', self.__tcp_data_recv[8:16])
                self.__data_recv = self.__tcp_data_recv[8:] # dirty hack
            else:
                self.__sock.sendto(buf, self.__address)
                self.__data_recv = self.__sock.recv(response_size)
                self.__header = unpack('<4H', self.__data_recv[:8])
        except Exception as e:
            raise ZKNetworkError(str(e))

        self.__response = self.__header[0]
        self.__reply_id = self.__header[3]
        self.__data = self.__data_recv[8:] #could be empty
        if self.__response in [const.CMD_ACK_OK, const.CMD_PREPARE_DATA, const.CMD_DATA]:
            return {
                'status': True,
                'code': self.__response
            }
        return {
            'status': False,
            'code': self.__response
        }

    def __ack_ok(self):
        """ event ack ok """
        buf = self.__create_header(const.CMD_ACK_OK, b'', self.__session_id, const.USHRT_MAX - 1)
        try:
            if self.tcp:
                top = self.__create_tcp_top(buf)
                self.__sock.send(top)
            else:
                self.__sock.sendto(buf, self.__address)
        except Exception as e:
            raise ZKNetworkError(str(e))

    def __get_data_size(self):
        """Checks a returned packet to see if it returned CMD_PREPARE_DATA,
        indicating that data packets are to be sent

        Returns the amount of bytes that are going to be sent"""
        response = self.__response
        if response == const.CMD_PREPARE_DATA:
            size = unpack('I', self.__data[:4])[0]
            return size
        else:
            return 0

    def __reverse_hex(self, hex):
        data = ''
        for i in reversed(xrange(len(hex) / 2)):
            data += hex[i * 2:(i * 2) + 2]
        return data

    def __decode_time(self, t):
        """Decode a timestamp retrieved from the timeclock

        copied from zkemsdk.c - DecodeTime"""
        """
        t = t.encode('hex')
        t = int(self.__reverse_hex(t), 16)
        if self.verbose: print ("decode from  %s "% format(t, '04x'))
        """
        t = unpack("<I", t)[0]
        second = t % 60
        t = t // 60

        minute = t % 60
        t = t // 60

        hour = t % 24
        t = t // 24

        day = t % 31 + 1
        t = t // 31

        month = t % 12 + 1
        t = t // 12

        year = t + 2000

        d = datetime(year, month, day, hour, minute, second)

        return d
    def __decode_timehex(self, timehex):
        """timehex string of six bytes"""
        year, month, day, hour, minute, second = unpack("6B", timehex)
        year += 2000
        d = datetime(year, month, day, hour, minute, second)
        return d
    def __encode_time(self, t):
        """Encode a timestamp so that it can be read on the timeclock
        """
        # formula taken from zkemsdk.c - EncodeTime
        # can also be found in the technical manual
        d = (
            ((t.year % 100) * 12 * 31 + ((t.month - 1) * 31) + t.day - 1) *
            (24 * 60 * 60) + (t.hour * 60 + t.minute) * 60 + t.second
        )
        return d

    def connect(self):
        '''
        connect to the device
        '''
        self.end_live_capture = False # jic
        if not self.ommit_ping and not self.helper.test_ping():
            raise ZKNetworkError("can't reach device (ping %s)" % self.__address[0])
        if not self.force_udp and self.helper.test_tcp() == 0: #ok
            self.tcp = True
            self.user_packet_size = 72 # default zk8
        self.__create_socket()# tcp based
        self.__session_id = 0
        self.__reply_id = const.USHRT_MAX - 1
        cmd_response = self.__send_command(const.CMD_CONNECT)
        self.__session_id = self.__header[2]
        if cmd_response.get('code') == const.CMD_ACK_UNAUTH:
            if self.verbose: print ("try auth")
            command_string = make_commkey(self.__password, self.__session_id)
            cmd_response = self.__send_command(const.CMD_AUTH, command_string)
        if cmd_response.get('status'):
            self.is_connect = True
            # set the session id
            return self
        else:
            if cmd_response["code"] == const.CMD_ACK_UNAUTH:
                raise ZKErrorResponse("Unauthenticated")
            if self.verbose: print ("connect err response {} ".format(cmd_response["code"]))
            raise ZKErrorResponse("Invalid response: Can't connect")

    def disconnect(self):
        '''
        diconnect from the connected device
        '''
        self.is_connect = False
        cmd_response = self.__send_command(const.CMD_EXIT)
        if cmd_response.get('status'):
            if self.__sock:
                self.__sock.close() #leave to GC
            return True
        else:
            raise ZKErrorResponse("can't disconnect")


    def get_platform(self):
        '''
        return the platform name
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'~Platform\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            platform = self.__data.split(b'=', 1)[-1].split(b'\x00')[0]
            platform = platform.replace(b'=', b'')
            return platform.decode()
        else:
            raise ZKErrorResponse("Can't get platform")

    def get_mac(self):
        '''
        return the mac
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'MAC\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            mac = self.__data.split(b'=', 1)[-1].split(b'\x00')[0]
            return mac.decode()
        else:
            raise ZKErrorResponse("can't get mac")

    def get_device_name(self):
        '''
        return the device name
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'~DeviceName\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            device = self.__data.split(b'=', 1)[-1].split(b'\x00')[0]
            return device.decode()
        else:
            return "" #no name
            #raise ZKErrorResponse("can't read device name")

    def get_face_version(self):
        '''
        return the face version
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'ZKFaceVersion\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            response = self.__data.split(b'=', 1)[-1].split(b'\x00')[0]
            return safe_cast(response, int, 0)  if response else 0
        else:
            return None

    def get_fp_version(self):
        '''
        return the fingerprint version
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'~ZKFPVersion\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            response = self.__data.split(b'=', 1)[-1].split(b'\x00')[0]
            response = response.replace(b'=', b'')
            return safe_cast(response, int, 0) if response else 0
        else:
            return None
    def _clear_error(self, command_string=b''):
        """ clear ACK_error """
        cmd_response = self.__send_command(const.CMD_ACK_ERROR, command_string, 1024)
        # cmd_response['code'] shuld be CMD_ACK_UNKNOWN
        cmd_response = self.__send_command(const.CMD_ACK_UNKNOWN, command_string, 1024)
        cmd_response = self.__send_command(const.CMD_ACK_UNKNOWN, command_string, 1024)
        cmd_response = self.__send_command(const.CMD_ACK_UNKNOWN, command_string, 1024)

    def get_extend_fmt(self):
        '''
        determine extend fmt
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'~ExtendFmt\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            fmt = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
            #definitivo? seleccionar firmware aqui?
            return safe_cast(fmt, int, 0) if fmt else 0
        else:
            self._clear_error(command_string)
            return None
            #raise ZKErrorResponse("Can't read extend fmt")

    def get_user_extend_fmt(self):
        '''
        determine user extend fmt
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'~UserExtFmt\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            fmt = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
            #definitivo? seleccionar firmware aqui?
            return safe_cast(fmt, int, 0) if fmt else 0
        else:
            self._clear_error(command_string)
            return None

    def get_face_fun_on(self):
        '''
        determine extend fmt
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'FaceFunOn\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            response = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
            #definitivo? seleccionar firmware aqui?
            return safe_cast(response, int ,0) if response else 0
        else:
            self._clear_error(command_string)
            return None

    def get_compat_old_firmware(self):
        '''
        determine old firmware
        '''
        command = const.CMD_OPTIONS_RRQ
        command_string = b'CompatOldFirmware\x00'
        response_size = 1024

        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            response = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
            #definitivo? seleccionar firmware aqui?
            return safe_cast(response, int, 0) if response else 0
        else:
            self._clear_error(command_string)
            return None

    def get_network_params(self):
        ip = self.__address[0]
        mask = b''
        gate = b''
        cmd_response = self.__send_command(const.CMD_OPTIONS_RRQ, b'IPAddress\x00', 1024)
        if cmd_response.get('status'):
            ip = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
        cmd_response = self.__send_command(const.CMD_OPTIONS_RRQ, b'NetMask\x00', 1024)
        if cmd_response.get('status'):
            mask = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
        cmd_response = self.__send_command(const.CMD_OPTIONS_RRQ, b'GATEIPAddress\x00', 1024)
        if cmd_response.get('status'):
            gate = (self.__data.split(b'=', 1)[-1].split(b'\x00')[0])
        return {'ip': ip.decode(), 'mask': mask.decode(), 'gateway': gate.decode()}

    def get_pin_width(self):
        '''
        return the serial number
        '''
        command = const.CMD_GET_PINWIDTH
        command_string = b' P'
        response_size = 9
        cmd_response = self.__send_command(command, command_string, response_size)
        if cmd_response.get('status'):
            width = self.__data.split(b'\x00')[0]
            return bytearray(width)[0]
        else:
            raise ZKErrorResponse("can0t get pin width")

    def free_data(self):
        """ clear buffer"""
        command = const.CMD_FREE_DATA
        cmd_response = self.__send_command(command)
        if cmd_response.get('status'):
            return True
        else:
            raise ZKErrorResponse("can't free data")

    def read_sizes(self):
        """ read sizes """
        command = const.CMD_GET_FREE_SIZES
        response_size = 1024
        cmd_response = self.__send_command(command,b'', response_size)
        if cmd_response.get('status'):
            if self.verbose: print(codecs.encode(self.__data,'hex'))
            size = len(self.__data)
            if len(self.__data) >= 80:
                fields = unpack('20i', self.__data[:80])
                self.users = fields[4]
                self.fingers = fields[6]
                self.records = fields[8]
                self.dummy = fields[10] #???
                self.cards = fields[12]
                self.fingers_cap = fields[14]
                self.users_cap = fields[15]
                self.rec_cap = fields[16]
                self.fingers_av = fields[17]
                self.users_av = fields[18]
                self.rec_av = fields[19]
                self.__data = self.__data[80:]
            if len(self.__data) >= 12: #face info
                fields = unpack('3i', self.__data[:12]) #dirty hack! we need more information
                self.faces = fields[0]
                self.faces_cap = fields[2]
            return True
        else:
            raise ZKErrorResponse("can't read sizes")

    def unlock(self, time=3):
        '''
        :param time: define time in seconds
        :return:
        thanks to https://github.com/SoftwareHouseMerida/pyzk/
        '''
        command = const.CMD_UNLOCK
        command_string = pack("I",int(time)*10)
        cmd_response = self.__send_command(command, command_string)
        if cmd_response.get('status'):
            return True
        else:
            raise ZKErrorResponse("Can't open door")

    def __str__(self):
        """ for debug"""
        return "ZK %s://%s:%s users[%i]:%i/%i fingers:%i/%i, records:%i/%i faces:%i/%i" % (
            "tcp" if self.tcp else "udp", self.__address[0], self.__address[1],
            self.user_packet_size, self.users, self.users_cap,
            self.fingers, self.fingers_cap,
            self.records, self.rec_cap,
            self.faces, self.faces_cap
        )

    def restart(self):
        '''
        restart the device
        '''
        command = const.CMD_RESTART
        cmd_response = self.__send_command(command)
        if cmd_response.get('status'):
            return True
        else:
            raise ZKErrorResponse("can't restart device")

    def get_time(self):
        """get Device Time"""
        command = const.CMD_GET_TIME
        response_size = 1032
        cmd_response = self.__send_command(command, b'', response_size)
        if cmd_response.get('status'):
            return self.__decode_time(self.__data[:4])
        else:
            raise ZKErrorResponse("can't get time")

    def set_time(self, timestamp):
        """ set Device time (pass datetime object)"""
        command = const.CMD_SET_TIME
        command_string = pack(b'I', self.__encode_time(timestamp))
        cmd_response = self.__send_command(command, command_string)
        if cmd_response.get('status'):
            return True
        else:
            raise ZKErrorResponse("can't set time")

   
    def _send_with_buffer(self, buffer):
        MAX_CHUNK = 1024
        size = len(buffer)
        #free_Data
        self.free_data()
        # send prepare_data
        command = const.CMD_PREPARE_DATA
        command_string = pack('I', size)
        cmd_response = self.__send_command(command, command_string)
        if not cmd_response.get('status'):
            raise ZKErrorResponse("Can't prepare data")
        remain = size % MAX_CHUNK
        packets = (size - remain) // MAX_CHUNK
        start = 0
        for _wlk in range(packets):
            self.__send_chunk(buffer[start:start+MAX_CHUNK])
            start += MAX_CHUNK
        if remain:
            self.__send_chunk(buffer[start:start+remain])

    def __send_chunk(self, command_string):
        command = const.CMD_DATA
        cmd_response = self.__send_command(command, command_string)
        if cmd_response.get('status'):
            return True #refres_data (1013)?
        else:
            raise ZKErrorResponse("Can't send chunk")


    def get_users(self): #ALWAYS CALL TO GET correct user_packet_size
        """ return all user """
        self.read_sizes() # last update
        if self.users == 0: #lazy
            self.next_uid = 1
            self.next_user_id='1'
            return []
        users = []
        max_uid = 0
        userdata, size = self.read_with_buffer(const.CMD_USERTEMP_RRQ, const.FCT_USER)
        if self.verbose: print("user size {} (= {})".format(size, len(userdata)))
        if size <= 4:
            print("WRN: missing user data")# debug
            return []
        total_size = unpack("I",userdata[:4])[0]
        self.user_packet_size = total_size / self.users
        if not self.user_packet_size in [28, 72]:
            if self.verbose: print("WRN packet size would be  %i" % self.user_packet_size)
        userdata = userdata[4:]
        if self.user_packet_size == 28:
            while len(userdata) >= 28:
                uid, privilege, password, name, card, group_id, timezone, user_id = unpack('<HB5s8sIxBhI',userdata.ljust(28, b'\x00')[:28])
                if uid > max_uid: max_uid = uid
                password = (password.split(b'\x00')[0]).decode(self.encoding, errors='ignore')
                name = (name.split(b'\x00')[0]).decode(self.encoding, errors='ignore').strip()
                #card = unpack('I', card)[0] #or hex value?
                group_id = str(group_id)
                user_id = str(user_id)
                #TODO: check card value and find in ver8
                if not name:
                    name = "NN-%s" % user_id
                user = User(uid, name, privilege, password, group_id, user_id, card)
                users.append(user)
                if self.verbose: print("[6]user:",uid, privilege, password, name, card, group_id, timezone, user_id)
                userdata = userdata[28:]
        else:
            while len(userdata) >= 72:
                uid, privilege, password, name, card, group_id, user_id = unpack('<HB8s24sIx7sx24s', userdata.ljust(72, b'\x00')[:72])
                #u1 = int(uid[0].encode("hex"), 16)
                #u2 = int(uid[1].encode("hex"), 16)
                #uid = u1 + (u2 * 256)
                #privilege = int(privilege.encode("hex"), 16)
                password = (password.split(b'\x00')[0]).decode(self.encoding, errors='ignore')
                name = (name.split(b'\x00')[0]).decode(self.encoding, errors='ignore').strip()
                group_id = (group_id.split(b'\x00')[0]).decode(self.encoding, errors='ignore').strip()
                user_id = (user_id.split(b'\x00')[0]).decode(self.encoding, errors='ignore')
                if uid > max_uid: max_uid = uid
                #card = int(unpack('I', separator)[0])
                if not name:
                    name = "NN-%s" % user_id
                user = User(uid, name, privilege, password, group_id, user_id, card)
                users.append(user)
                userdata = userdata[72:]
        #get limits!
        max_uid += 1
        self.next_uid = max_uid
        self.next_user_id = str(max_uid)
        #re check
        while True:
            if any(u for u in users if u.user_id == self.next_user_id):
                max_uid += 1
                self.next_user_id = str(max_uid)
            else:
                break
        return users


    def __recieve_raw_data(self, size):
        """ partial data ? """
        data = []
        if self.verbose: print ("expecting {} bytes raw data".format(size))
        while size > 0:
            data_recv = self.__sock.recv(size) #ideal limit?
            recieved = len(data_recv)
            if self.verbose: print ("partial recv {}".format(recieved))
            if recieved < 100 and self.verbose: print ("   recv {}".format(codecs.encode(data_recv, 'hex')))
            data.append(data_recv) # w/o tcp and header
            size -= recieved
            if self.verbose: print ("still need {}".format(size))
        return b''.join(data)

    def __recieve_chunk(self):
        """ recieve a chunk """
        if self.__response == const.CMD_DATA: # less than 1024!!!
            if self.tcp: #MUST CHECK TCP SIZE
                if self.verbose: print ("_rc_DATA! is {} bytes, tcp length is {}".format(len(self.__data), self.__tcp_length))
                if len(self.__data) < (self.__tcp_length - 8):
                    need = (self.__tcp_length - 8) - len(self.__data)
                    if self.verbose: print ("need more data: {}".format(need))
                    more_data = self.__recieve_raw_data(need)
                    return b''.join([self.__data, more_data])
                else: #enough data
                    if self.verbose: print ("Enough data")
                    return self.__data
            else: #UDP
                if self.verbose: print ("_rc len is {}".format(len(self.__data)))
                return self.__data #without headers
        elif self.__response == const.CMD_PREPARE_DATA:
            data = []
            size = self.__get_data_size() # from prepare data response...
            if self.verbose: print ("recieve chunk: prepare data size is {}".format(size))
            if self.tcp:
                if self.verbose: print ("recieve chunk: len data is {}".format(len(self.__data)))
                #ideally 8 bytes of PREPARE_DATA only...
                #but sometimes it comes with data...

                if len(self.__data) >= (8 + size): #prepare data with actual data! should be 8+size+32
                    data_recv = self.__data[8:] #  no need for more data! test, maybe -32
                else:
                    data_recv = self.__data[8:] + self.__sock.recv(size + 32) #could have two commands
                resp, broken_header = self.__recieve_tcp_data(data_recv, size)
                data.append(resp)
                # get CMD_ACK_OK
                if len(broken_header) < 16:
                    data_recv = broken_header + self.__sock.recv(16)
                else:
                    data_recv = broken_header
                #could be broken
                if len(data_recv) < 16:
                    print ("trying to complete broken ACK %s /16" % len(data_recv))
                    if self.verbose: print (data_recv.encode('hex')) #todo python3
                    data_recv += self.__sock.recv(16 - len(data_recv)) #TODO: CHECK HERE_!
                if not self.__test_tcp_top(data_recv):
                    if self.verbose: print ("invalid chunk tcp ACK OK")
                    return None #b''.join(data) # incomplete?
                response = unpack('HHHH', data_recv[8:16])[0]
                if response == const.CMD_ACK_OK:
                    if self.verbose: print ("chunk tcp ACK OK!")
                    return b''.join(data)
                if self.verbose: print("bad response %s" % data_recv)
                if self.verbose: print (codecs.encode(data,'hex'))
                return None

                return resp
            #else udp
            while True: #limitado por respuesta no por tamaño
                data_recv = self.__sock.recv(1024+8)
                response = unpack('<4H', data_recv[:8])[0]
                if self.verbose: print ("# packet response is: {}".format(response))
                if response == const.CMD_DATA:
                    data.append(data_recv[8:]) #header turncated
                    size -= 1024 #UDP
                elif response == const.CMD_ACK_OK:
                    break #without problem.
                else:
                    #truncado! continuar?
                    if self.verbose: print ("broken!")
                    break
                if self.verbose: print ("still needs %s" % size)
            return b''.join(data)
        else:
            if self.verbose: print ("invalid response %s" % self.__response)
            return None #("can't get user template")

    def __read_chunk(self, start, size):
        """ read a chunk from buffer """
        for _retries in range(3):
            command = 1504 #CMD_READ_BUFFER
            command_string = pack('<ii', start, size)
            if self.tcp:
                response_size = size + 32
            else:
                response_size = 1024 + 8
            cmd_response = self.__send_command(command, command_string, response_size)
            data = self.__recieve_chunk()
            if data is not None:
                return data
        else:
            raise ZKErrorResponse("can't read chunk %i:[%i]" % (start, size))

    def read_with_buffer(self, command, fct=0 ,ext=0):
        """ Test read info with buffered command (ZK6: 1503) """
        if self.tcp:
            MAX_CHUNK = 0xFFc0 #arbitrary, below 0x10008
        else:
            MAX_CHUNK = 16 * 1024
        command_string = pack('<bhii', 1, command, fct, ext)
        if self.verbose: print ("rwb cs", command_string)
        response_size = 1024
        data = []
        start = 0
        cmd_response = self.__send_command(1503, command_string, response_size)
        if not cmd_response.get('status'):
            raise ZKErrorResponse("RWB Not supported")
        if cmd_response['code'] == const.CMD_DATA:
            #direct!!! small!!!
            if self.tcp: #MUST CHECK TCP SIZE
                if self.verbose: print ("DATA! is {} bytes, tcp length is {}".format(len(self.__data), self.__tcp_length))
                if len(self.__data) < (self.__tcp_length - 8):
                    need = (self.__tcp_length - 8) - len(self.__data)
                    if self.verbose: print ("need more data: {}".format(need))
                    more_data = self.__recieve_raw_data(need)
                    return b''.join([self.__data, more_data]), len(self.__data) + len(more_data)
                else: #enough data
                    if self.verbose: print ("Enough data")
                    size = len(self.__data)
                    return self.__data, size
            else: #udp is direct
                size = len(self.__data)
                return self.__data, size
        #else ACK_OK with size
        size = unpack('I', self.__data[1:5])[0]  # extra info???
        if self.verbose: print ("size fill be %i" % size)
        remain = size % MAX_CHUNK
        packets = (size-remain) // MAX_CHUNK # should be size /16k
        if self.verbose: print ("rwb: #{} packets of max {} bytes, and extra {} bytes remain".format(packets, MAX_CHUNK, remain))
        for _wlk in range(packets):
            data.append(self.__read_chunk(start,MAX_CHUNK))
            start += MAX_CHUNK
        if remain:
            data.append(self.__read_chunk(start, remain))
            start += remain # Debug
        self.free_data()
        if self.verbose: print ("_read w/chunk %i bytes" % start)
        return b''.join(data), start
