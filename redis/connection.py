import errno
import os
import socket
import threading
from itertools import imap
from redis.exceptions import ConnectionError, ResponseError, InvalidResponse

class PythonParser(object):
    def on_connect(self, connection):
        "Called when the socket connects"
        self._fp = connection._sock.makefile('r')

    def on_disconnect(self):
        "Called when the socket disconnects"
        self._fp.close()
        self._fp = None

    def read(self, length=None):
        """
        Read a line from the socket is no length is specified,
        otherwise read ``length`` bytes. Always strip away the newlines.
        """
        try:
            if length is not None:
                return self._fp.read(length+2)[:-2]
            return self._fp.readline()[:-2]
        except socket.error, e:
            raise ConnectionError("Error while reading from socket: %s" % \
                e.args[1])

    def read_response(self):
        response = self.read()
        if not response:
            raise ConnectionError("Socket closed on remote end")

        byte, response = response[0], response[1:]

        # server returned an error
        if byte == '-':
            if response.startswith('ERR '):
                response = response[4:]
                return ResponseError(response)
            if response.startswith('LOADING '):
                # If we're loading the dataset into memory, kill the socket
                # so we re-initialize (and re-SELECT) next time.
                raise ConnectionError("Redis is loading data into memory")
        # single value
        elif byte == '+':
            return response
        # int value
        elif byte == ':':
            return long(response)
        # bulk response
        elif byte == '$':
            length = int(response)
            if length == -1:
                return None
            response = length and self.read(length) or ''
            return response
        # multi-bulk response
        elif byte == '*':
            length = int(response)
            if length == -1:
                return None
            return [self.read_response() for i in xrange(length)]
        raise InvalidResponse("Protocol Error")

class HiredisParser(object):
    def on_connect(self, connection):
        self._sock = connection._sock
        self._reader = hiredis.Reader(
            protocolError=InvalidResponse,
            replyError=ResponseError)

    def on_disconnect(self):
        self._sock = None
        self._reader = None

    def read_response(self):
        response = self._reader.gets()
        while response is False:
            buffer = self._sock.recv(4096)
            if not buffer:
                raise ConnectionError("Socket closed on remote end")
            self._reader.feed(buffer)
            # if the data received doesn't end with \r\n, then there's more in
            # the socket
            if not buffer.endswith('\r\n'):
                continue
            response = self._reader.gets()
        return response

try:
    import hiredis
    DefaultParser = HiredisParser
except ImportError:
    DefaultParser = PythonParser

class Connection(object):
    "Manages TCP communication to and from a Redis server"
    def __init__(self, host='localhost', port=6379, db=0, password=None,
                 socket_timeout=None, encoding='utf-8',
                 encoding_errors='strict', parser_class=DefaultParser):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.socket_timeout = socket_timeout
        self.encoding = encoding
        self.encoding_errors = encoding_errors
        self._sock = None
        self._parser = parser_class()

    def connect(self):
        "Connects to the Redis server if not already connected"
        if self._sock:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.socket_timeout)
            sock.connect((self.host, self.port))
        except socket.error, e:
            # args for socket.error can either be (errno, "message")
            # or just "message"
            if len(e.args) == 1:
                error_message = "Error connecting to %s:%s. %s." % \
                    (self.host, self.port, e.args[0])
            else:
                error_message = "Error %s connecting %s:%s. %s." % \
                    (e.args[0], self.host, self.port, e.args[1])
            raise ConnectionError(error_message)

        self._sock = sock
        self.on_connect()

    def on_connect(self):
        "Initialize the connection, authenticate and select a database"
        self._parser.on_connect(self)

        # if a password is specified, authenticate
        if self.password:
            self.send_command('AUTH', self.password)
            if self.read_response() != 'OK':
                raise ConnectionError('Invalid Password')

        # if a database is specified, switch to it
        if self.db:
            self.send_command('SELECT', self.db)
            if self.read_response() != 'OK':
                raise ConnectionError('Invalid Database')

    def disconnect(self):
        "Disconnects from the Redis server"
        self._parser.on_disconnect()
        if self._sock is None:
            return
        try:
            self._sock.close()
        except socket.error:
            pass
        self._sock = None

    def _send(self, command):
        "Send the command to the socket"
        if not self._sock:
            self.connect()
        try:
            self._sock.sendall(command)
        except socket.error, e:
            if e.args[0] == errno.EPIPE:
                self.disconnect()
            if len(e.args) == 1:
                _errno, errmsg = 'UNKNOWN', e.args[0]
            else:
                _errno, errmsg = e.args
            raise ConnectionError("Error %s while writing to socket. %s." % \
                (_errno, errmsg))

    def send_packed_command(self, command):
        "Send an already packed command to the Redis server"
        try:
            self._send(command)
        except ConnectionError:
            # retry the command once in case the socket connection simply
            # timed out
            self.disconnect()
            # if this _send() call fails, then the error will be raised
            self._send(command)

    def send_command(self, *args):
        "Pack and send a command to the Redis server"
        self.send_packed_command(self.pack_command(*args))

    def read_response(self):
        "Read the response from a previously sent command"
        response = self._parser.read_response()
        if response.__class__ == ResponseError:
            raise response
        return response

    def encode(self, value):
        "Return a bytestring of the value"
        if isinstance(value, unicode):
            return value.encode(self.encoding, self.encoding_errors)
        return str(value)

    def pack_command(self, *args):
        "Pack a series of arguments into a value Redis command"
        command = ['$%s\r\n%s\r\n' % (len(enc_value), enc_value)
                   for enc_value in imap(self.encode, args)]
        return '*%s\r\n%s' % (len(command), ''.join(command))

class ConnectionPool(threading.local):
    "Manages a list of connections on the local thread"
    def __init__(self, connection_class=None):
        self.connections = {}
        self.connection_class = connection_class or Connection

    def make_connection_key(self, host, port, db):
        "Create a unique key for the specified host, port and db"
        return '%s:%s:%s' % (host, port, db)

    def get_connection(self, host, port, db, password, socket_timeout):
        "Return a specific connection for the specified host, port and db"
        key = self.make_connection_key(host, port, db)
        if key not in self.connections:
            self.connections[key] = self.connection_class(
                host, port, db, password, socket_timeout)
        return self.connections[key]

    def get_all_connections(self):
        "Return a list of all connection objects the manager knows about"
        return self.connections.values()
