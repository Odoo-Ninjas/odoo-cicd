import threading
from contextlib import contextmanager
import time
import socket
import os
import logging
import arrow
logger = logging.getLogger(__name__)

KEEP_ALIVE_MESSAGE = "Keep alive signal - still working"


class LogsIOWriter(object):
    def __init__(self, stream, source, host='cicdlogs', port=6689):
        if isinstance(stream, dict):
            stream = stream['name']
        self.lines = []
        stream = stream.replace("|", "_")
        source = source.replace("|", "_")
        self.stream = stream
        self.source = source
        self.keep_alive_thread = None
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

    def __del__(self):
        if self.keep_alive_thread:
            self.keep_alive_thread = False

    @contextmanager
    def GET(stream, source, host='cicdlogs', port=6689):
        res = LogsIOWriter(stream=stream, source=source, host=host, port=port)
        try:
            # res.start_keepalive()
            yield res
        finally:
            res.stop_keepalive()

    def get_lines(self):
        return list(filter(lambda x: KEEP_ALIVE_MESSAGE not in x, self.lines))

    def start_keepalive(self):
        def keep_alive(self):
            i = 0
            while self.keep_alive_thread:
                i += 1
                time.sleep(5 if os.getenv("DEVMODE") == "1" else 20)
                self.info(KEEP_ALIVE_MESSAGE + " " + str(i))

        self.keep_alive_thread = threading.Thread(target=keep_alive, args=(self,))
        self.keep_alive_thread.background = True
        self.keep_alive_thread.start()

    def stop_keepalive(self):
        self.keep_alive_thread = False

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
        self.lines += msg.split("\n")

    def write_text(self, msg):
        self.info(msg)

    def info(self, msg):
        msg = self._format('INFO', msg)
        self._write_text(msg)
        # logger.info(msg)

    def error(self, msg):
        msg = self._format('ERROR', msg)
        self._write_text(msg)
        logger.error(msg)

    def warn(self, msg):
        msg = self._format('WARN', msg)
        self._write_text(msg)
        logger.warn(msg)

    def debug(self, msg):
        msg = self._format('DEBUG', msg)
        self._write_text(msg)
        # logger.debug(msg)