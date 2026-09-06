"""Remove image container metadata without decoding the pixel plane."""


def jpeg_without_metadata(payload: bytes) -> bytes:
    if not payload.startswith(b'\xff\xd8'):
        raise ValueError('invalid JPEG header')
    output = bytearray(payload[:2])
    offset = 2
    while offset < len(payload):
        start = offset
        if payload[offset] != 255:
            raise ValueError('invalid JPEG marker')
        while offset < len(payload) and payload[offset] == 255:
            offset += 1
        if offset >= len(payload):
            raise ValueError('truncated JPEG marker')
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            return bytes(output) + b'\xff\xd9'
        if marker == 1 or 0xD0 <= marker <= 0xD7:
            output.extend(payload[start:offset])
            continue
        if marker in {0, 0xD8}:
            raise ValueError('invalid JPEG segment')
        size = int.from_bytes(payload[offset : offset + 2], 'big')
        end = offset + size
        if size < 2 or end > len(payload):
            raise ValueError('truncated JPEG segment')
        # APP14 Adobe describes the pixel color transform, not user metadata.
        adobe = marker == 0xEE and size == 14 and payload[offset + 2 : offset + 7] == b'Adobe'
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE) or adobe:
            output.extend(payload[start:end])
        offset = end
        if marker == 0xDA:
            # Entropy-coded scans escape FF as FF00; restart markers belong
            # to the scan. Parse the next real marker, including later scans.
            scan = offset
            while True:
                boundary = payload.find(b'\xff', offset)
                if boundary < 0:
                    raise ValueError('unterminated JPEG scan')
                cursor = boundary + 1
                while cursor < len(payload) and payload[cursor] == 255:
                    cursor += 1
                if cursor >= len(payload):
                    raise ValueError('truncated JPEG scan marker')
                if payload[cursor] == 0 or 0xD0 <= payload[cursor] <= 0xD7:
                    offset = cursor + 1
                    continue
                output.extend(payload[scan:boundary])
                offset = boundary
                break
    raise ValueError('JPEG end missing')


def png_chunks(payload: bytes):
    signature = b'\x89PNG\r\n\x1a\n'
    if not payload.startswith(signature):
        raise ValueError('invalid PNG header')
    offset = 8
    while offset + 12 <= len(payload):
        size = int.from_bytes(payload[offset : offset + 4], 'big')
        kind = payload[offset + 4 : offset + 8]
        end = offset + size + 12
        if end > len(payload):
            raise ValueError('truncated PNG chunk')
        yield kind, offset, size, end
        offset = end
        if kind == b'IEND':
            return
    raise ValueError('PNG end missing')


def png_without_metadata(payload: bytes) -> bytes:
    output = bytearray(payload[:8])
    for kind, offset, _size, end in png_chunks(payload):
        if kind not in {b'eXIf', b'tEXt', b'zTXt', b'iTXt', b'iCCP'}:
            output.extend(payload[offset:end])
    return bytes(output)


def webp_chunks(payload: bytes):
    if payload[:4] != b'RIFF' or payload[8:12] != b'WEBP':
        raise ValueError('invalid WebP header')
    length = int.from_bytes(payload[4:8], 'little') + 8
    if length > len(payload):
        raise ValueError('truncated WebP container')
    offset = 12
    while offset + 8 <= length:
        kind = payload[offset : offset + 4]
        size = int.from_bytes(payload[offset + 4 : offset + 8], 'little')
        end = offset + 8 + size + (size & 1)
        if end > length:
            raise ValueError('truncated WebP chunk')
        yield kind, offset, size, end
        offset = end
    if offset != length:
        raise ValueError('incomplete WebP chunk')


def webp_without_metadata(payload: bytes) -> bytes:
    chunks = bytearray()
    for kind, offset, size, end in webp_chunks(payload):
        if kind not in {b'EXIF', b'XMP ', b'ICCP'}:
            chunk = bytearray(payload[offset:end])
            if kind == b'VP8X':
                if size != 10:
                    raise ValueError('invalid WebP extended header')
                chunk[8] &= ~(0x20 | 0x08 | 0x04)
            chunks.extend(chunk)
    return b'RIFF' + (len(chunks) + 4).to_bytes(4, 'little') + b'WEBP' + bytes(chunks)


def without_metadata(payload: bytes, content_type: str) -> bytes:
    return {
        'image/jpeg': jpeg_without_metadata,
        'image/png': png_without_metadata,
        'image/webp': webp_without_metadata,
    }[content_type](payload)
