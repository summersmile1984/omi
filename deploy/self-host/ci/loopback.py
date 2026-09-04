#!/usr/bin/env python3
"""Expose fixture HTTP ports while application containers remain internal-only."""

from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

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

        def dispatch(self):
            size = int(self.headers.get('Content-Length', '0'))
            if not self.path.startswith('/') or not 0 <= size <= 1024 * 1024:
                self.send_error(400)
                return
            headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS}
            with closing(HTTPConnection(host, port, timeout=30)) as connection:
                connection.request(self.command, self.path, self.rfile.read(size), headers)
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
