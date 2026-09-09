"""Bounded model HTTP framing and absolute socket deadlines without worker pools."""

import json
import time
import httpcore
import httpx

LIMIT = 1024 * 1024


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise httpx.TimeoutException('local model deadline exceeded')
    return value


class DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline

    def read(self, max_bytes, timeout=None):
        result = self.stream.read(max_bytes, timeout=remaining(self.deadline))
        remaining(self.deadline)
        return result

    def write(self, buffer, timeout=None):
        self.stream.write(buffer, timeout=remaining(self.deadline))
        remaining(self.deadline)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = self.stream.start_tls(ssl_context, server_hostname, timeout=remaining(self.deadline))
        try:
            remaining(self.deadline)
        except httpx.TimeoutException:
            stream.close()
            raise
        return DeadlineStream(stream, self.deadline)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, deadline, backend=None):
        self.deadline, self.backend = deadline, backend or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = self.backend.connect_tcp(
            host, port, timeout=remaining(self.deadline), local_address=local_address, socket_options=socket_options
        )
        try:
            remaining(self.deadline)
        except httpx.TimeoutException:
            stream.close()
            raise
        return DeadlineStream(stream, self.deadline)


class CoreBody(httpx.SyncByteStream):
    def __init__(self, response):
        self.response = response

    def __iter__(self):
        try:
            yield from self.response.iter_stream()
        except httpcore.TimeoutException as error:
            raise httpx.TimeoutException('local model deadline exceeded') from error
        except (httpcore.NetworkError, httpcore.ProtocolError) as error:
            raise httpx.NetworkError('local model transport failed') from error

    def close(self):
        self.response.close()


class DeadlineTransport(httpx.BaseTransport):
    """httpcore's public network backend clamps every socket operation."""

    def __init__(self, deadline):
        self.pool = httpcore.ConnectionPool(
            network_backend=DeadlineBackend(deadline), max_connections=1, max_keepalive_connections=1, retries=0
        )

    def handle_request(self, request):
        try:
            result = self.pool.handle_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
            )
        except httpcore.TimeoutException as error:
            raise httpx.TimeoutException('local model deadline exceeded') from error
        except (httpcore.NetworkError, httpcore.ProtocolError) as error:
            raise httpx.NetworkError('local model transport failed') from error
        return httpx.Response(
            result.status, headers=result.headers, stream=CoreBody(result), extensions=result.extensions
        )

    def close(self):
        self.pool.close()


def validate_response(response, path):
    from .local_llm import LLMInputRejected, LLMUnavailable

    if response.status_code == 400 and path == '/api/chat':
        raise LLMInputRejected('selected model rejected input')
    if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
        raise LLMUnavailable('local model authority rejected response')


def decode_json(raw):
    from .local_llm import LLMUnavailable

    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('error'):
            raise ValueError('envelope')
        return data
    except (ValueError, UnicodeDecodeError) as error:
        raise LLMUnavailable('local model response is malformed') from error


def append_bounded(buffer, chunk):
    from .local_llm import LLMUnavailable

    if len(buffer) + len(chunk) > LIMIT:
        raise LLMUnavailable('local model response exceeds envelope limit')
    buffer.extend(chunk)


def request_json(client, method, origin, path, deadline, data=None):
    with client.stream(method, origin + path, json=data, timeout=remaining(deadline)) as response:
        validate_response(response, path)
        raw = bytearray()
        for chunk in response.iter_raw():
            remaining(deadline)
            append_bounded(raw, chunk)
        remaining(deadline)
    return decode_json(raw)


async def arequest_json(client, method, origin, path, data=None):
    raw = bytearray()
    async with client.stream(method, origin + path, json=data) as response:
        validate_response(response, path)
        async for chunk in response.aiter_raw():
            append_bounded(raw, chunk)
    return decode_json(raw)


class Frames:
    def __init__(self):
        self.pending, self.total = bytearray(), 0

    def add(self, chunk):
        from .local_llm import LLMUnavailable

        self.total += len(chunk)
        if self.total > 4 * LIMIT:
            raise LLMUnavailable('local model stream exceeds total limit')
        fragments = chunk.split(b'\n')
        for index, fragment in enumerate(fragments):
            append_bounded(self.pending, fragment)
            if index < len(fragments) - 1:
                yield decode_json(self.pending)
                self.pending.clear()

    def finish(self):
        if self.pending:
            yield decode_json(self.pending)
            self.pending.clear()
