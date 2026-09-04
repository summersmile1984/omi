#!/usr/bin/env python3
"""Expose fixture HTTP ports while application containers remain internal-only."""

from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading

BODY_LIMIT = 1024 * 1024
LINE_LIMIT = 8192

HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
}


def handler(host, port):
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def exact(self, size):
            value = self.rfile.read(size)
            if len(value) != size:
                raise ValueError('incomplete body')
            return value

        def body(self):
            lengths = self.headers.get_all('Content-Length', [])
            encodings = self.headers.get_all('Transfer-Encoding', [])
            if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
                raise ValueError('ambiguous body framing')
            if not encodings:
                length = lengths[0] if lengths else '0'
                if not re.fullmatch(r'[0-9]+', length) or int(length) > BODY_LIMIT:
                    raise ValueError('invalid body length')
                return self.exact(int(length))
            if encodings[0].strip().lower() != 'chunked':
                raise ValueError('unsupported transfer coding')
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(LINE_LIMIT + 1)
                if len(line) > LINE_LIMIT or not line.endswith(b'\r\n'):
                    raise ValueError('invalid chunk framing')
                size = line[:-2].split(b';', 1)[0]
                if not re.fullmatch(rb'[0-9a-fA-F]+', size):
                    raise ValueError('invalid chunk size')
                size = int(size, 16)
                total += size
                if total > BODY_LIMIT:
                    raise ValueError('body too large')
                if not size:
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(LINE_LIMIT + 1)
                        trailer_bytes += len(trailer)
                        if trailer_bytes > LINE_LIMIT or not trailer.endswith(b'\r\n'):
                            raise ValueError('invalid trailers')
                        if trailer == b'\r\n':
                            return b''.join(chunks)
                        if b':' not in trailer:
                            raise ValueError('invalid trailer field')
                chunks.append(self.exact(size))
                if self.exact(2) != b'\r\n':
                    raise ValueError('invalid chunk ending')

        def dispatch(self):
            self.connection.settimeout(30)
            try:
                if not self.path.startswith('/'):
                    raise ValueError('invalid request path')
                request_body = self.body()
            except (ValueError, TimeoutError, OSError):
                self.send_error(400)
                return
            headers = {
                key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS | {'content-length'}
            }
            # Transfer framing belongs to each HTTP hop. Once decoded, the
            # forwarded length must describe these exact bytes, including UTF-8.
            headers['Content-Length'] = str(len(request_body))
            with closing(HTTPConnection(host, port, timeout=30)) as connection:
                connection.request(self.command, self.path, request_body, headers)
                result = connection.getresponse()
                body = result.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    self.send_error(502)
                    return
                self.send_response(result.status)
                for key, value in result.getheaders():
                    if key.lower() not in HOP_HEADERS | {'content-length'}:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_GET = do_POST = do_PATCH = do_DELETE = dispatch

    return Proxy


if __name__ == '__main__':
    for host, port in (('backend', 8080), ('auth-server', 3000)):
        server = ThreadingHTTPServer(('0.0.0.0', port), handler(host, port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
