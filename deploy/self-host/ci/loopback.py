#!/usr/bin/env python3
"""Expose fixture HTTP/WebSocket ports while application containers stay internal."""

from contextlib import closing, suppress
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import threading
import time

BODY_LIMIT = 1024 * 1024
LINE_LIMIT = 8192
TUNNEL_BYTE_LIMIT = 64 * 1024 * 1024
TUNNEL_SECONDS = 900
RESPONSE_BYTE_LIMIT = 64 * 1024 * 1024
RESPONSE_SECONDS = 900

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


def tokens(headers, name):
    return {token.strip().lower() for value in headers.get_all(name, []) for token in value.split(',')}


def tunnel(client, upstream, upstream_socket):
    """Forward the two actual peers, including bytes buffered with their headers."""
    deadline = time.monotonic() + TUNNEL_SECONDS

    def stop():
        for connection in (client.connection, upstream_socket):
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def copy(reader, source, destination):
        total = 0
        try:
            while time.monotonic() < deadline:
                source.settimeout(min(30, max(0.001, deadline - time.monotonic())))
                # Reading from the HTTP parser's file retains a first audio or
                # ready frame received in the same TCP packet as the handshake.
                chunk = reader.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > TUNNEL_BYTE_LIMIT:
                    break
                destination.sendall(chunk)
        except (OSError, ValueError):
            pass
        finally:
            stop()

    outgoing = threading.Thread(target=copy, args=(client.rfile, client.connection, upstream_socket), daemon=True)
    outgoing.start()
    try:
        copy(upstream.fp, upstream_socket, client.connection)
    finally:
        stop()
        outgoing.join(5)
        if outgoing.is_alive():
            raise RuntimeError('fixture WebSocket reader did not stop')


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
                upgrades = self.headers.get_all('Upgrade', [])
                websocket = bool(upgrades)
                if websocket and (
                    self.command != 'GET'
                    or len(upgrades) != 1
                    or upgrades[0].lower() != 'websocket'
                    or 'upgrade' not in tokens(self.headers, 'Connection')
                    or request_body
                    or self.headers.get_all('Transfer-Encoding')
                ):
                    raise ValueError('invalid WebSocket upgrade')
                if not websocket and 'upgrade' in tokens(self.headers, 'Connection'):
                    raise ValueError('missing upgrade protocol')
            except (ValueError, TimeoutError, OSError):
                self.send_error(400)
                return
            headers = {
                key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS | {'content-length'}
            }
            # Transfer framing belongs to each HTTP hop. Once decoded, the
            # forwarded length must describe these exact bytes, including UTF-8.
            headers['Content-Length'] = str(len(request_body))
            if websocket:
                headers.update(Connection='Upgrade', Upgrade='websocket')
            with closing(HTTPConnection(host, port, timeout=30)) as connection:
                connection.request(self.command, self.path, request_body, headers)
                upstream_socket = connection.sock
                result = connection.getresponse()
                if result.status == 101:
                    if (
                        not websocket
                        or connection.sock is None
                        or result.getheader('Upgrade', '').lower() != 'websocket'
                        or 'upgrade' not in tokens(result.headers, 'Connection')
                    ):
                        self.send_error(502)
                        return
                    self.protocol_version = 'HTTP/1.1'
                    self.close_connection = True
                    self.send_response(101)
                    for key, value in result.getheaders():
                        if key.lower() not in HOP_HEADERS | {'content-length'}:
                            self.send_header(key, value)
                    self.send_header('Connection', 'Upgrade')
                    self.send_header('Upgrade', 'websocket')
                    self.end_headers()
                    self.wfile.flush()
                    try:
                        tunnel(self, result, connection.sock)
                    finally:
                        result.close()
                    return
                # Flush SSE events as they arrive. Buffering to EOF both hides
                # progress and times out while the real local model is working.
                # A missing final chunk makes an interrupted stream observable.
                self.protocol_version = 'HTTP/1.1'
                self.close_connection = True
                self.send_response(result.status)
                for key, value in result.getheaders():
                    if key.lower() not in HOP_HEADERS | {'content-length'}:
                        self.send_header(key, value)
                self.send_header('Connection', 'close')
                if result.status in (204, 304):
                    self.end_headers()
                    return
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.flush()
                deadline, total = time.monotonic() + RESPONSE_SECONDS, 0
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('fixture response deadline exceeded')
                        upstream_socket.settimeout(remaining)
                        chunk = result.read1(65536)
                        if not chunk:
                            if result.length not in (None, 0):
                                raise HTTPException('incomplete upstream body')
                            break
                        total += len(chunk)
                        if total > RESPONSE_BYTE_LIMIT:
                            raise ValueError('fixture response byte limit exceeded')
                        self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                        self.wfile.flush()
                except (OSError, HTTPException, ValueError):
                    # Headers have been sent; closing without the final chunk
                    # signals an incomplete body rather than fabricating success.
                    return
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()

        do_GET = do_POST = do_PATCH = do_DELETE = dispatch

    return Proxy


if __name__ == '__main__':
    for host, port in (('backend', 8080), ('auth-server', 3000)):
        server = ThreadingHTTPServer(('0.0.0.0', port), handler(host, port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
