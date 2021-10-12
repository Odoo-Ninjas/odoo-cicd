import socket
import os
import logging
import arrow
logger = logging.getLogger(__name__)

class LogsIOWriter(object):
    def __init__(self, stream, source, host='logs', port=6689):
        if isinstance(stream, dict):
            stream = stream['name']
        stream = stream.replace("|", "_")
        source = source.replace("|", "_")
        self.stream = stream
        self.source = source
        try:
            host = socket.gethostbyname_ex(host)
            host = host[-1][0]
        except Exception as ex:
            logger.error(ex)
            logger.error(f"Could not resolve {host}")
            self.host = None
            self.port = None
        else:
            self.host = host
            self.port = port
        self.tz = os.getenv("TIMEZONE", 'utc')
        self._send(f"+input|{self.stream}|{self.source}")

    def _send(self, txt):
        try:
            socket = self._get_socket()
        except:
            logger.info(txt)
        else:
            socket.send(f"{txt}\0".encode())

    def _get_socket(self):
        if not self.host:
            raise Exception(f"Host missing")
        serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        serversocket.connect((self.host, self.port))
        return serversocket

    def _format(self, level, txt):
        dt = arrow.get().to(self.tz).strftime("%Y-%m-%d %H:%M:%S")
        txt = f"[{level}] {dt} {txt}"
        return txt

    def _write_text(self, msg):
        self._send(f"+msg|{self.stream}|{self.source}|{msg.replace('|', '_')}")

    def write_text(self, msg):
        self.info(msg)

    def info(self, msg):
        msg = self._format('INFO', msg)
        self._write_text(msg)

    def error(self, msg):
        msg = self._format('ERROR', msg)
        self._write_text(msg)

    def warn(self, msg):
        msg = self._format('WARN', msg)
        self._write_text(msg)

    def debug(self, msg):
        msg = self._format('DEBUG', msg)
        self._write_text(msg)