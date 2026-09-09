"""Drop PNG alpha without allocating a source-sized pixel plane.

PNG filters predict each component from that same component in neighbouring
pixels. Removing the alpha bytes from each filtered row therefore preserves
the RGB/gray filters exactly, including Paeth and Adam7 passes. This matches
Pillow's convert('RGB'), which discards alpha rather than compositing it.
"""

import struct
import zlib

from frame_image_metadata import png_chunks

PASSES = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
# Retain only pixel representation. Pillow does not apply PNG color profiles
# when converting to RGB; Images must not color-manage those discarded tags.
PIXEL_CHUNKS = {b'IHDR', b'PLTE', b'IDAT', b'IEND'}


def chunk(kind, value):
    return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(value, zlib.crc32(kind)))


def opaque_png(payload):
    if next(png_chunks(payload), None) != (b'IHDR', 8, 13, 33):
        raise ValueError('invalid PNG header')
    for kind, offset, size, end in png_chunks(payload):
        if zlib.crc32(memoryview(payload)[offset + 4 : end - 4]) != int.from_bytes(payload[end - 4 : end], 'big'):
            raise ValueError('invalid PNG checksum')
        if kind == b'fdAT' or (
            kind == b'acTL' and (size != 8 or int.from_bytes(payload[offset + 8 : offset + 12], 'big') != 1)
        ):
            raise ValueError('animated PNG')
        if kind[:1].isupper() and kind not in PIXEL_CHUNKS:
            raise ValueError('unknown PNG critical chunk')
    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload[16:29])
    if not width or not height or compression or filtering or interlace not in {0, 1}:
        raise ValueError('invalid PNG dimensions or encoding')
    if color not in {4, 6}:
        output = bytearray(payload[:8])
        for kind, offset, _size, end in png_chunks(payload):
            if kind in PIXEL_CHUNKS:
                output.extend(memoryview(payload)[offset:end])
        return bytes(output)
    if depth not in {8, 16}:
        raise ValueError('invalid PNG alpha depth')
    component_bytes = depth // 8
    stride = (4 if color == 6 else 2) * component_bytes
    retained = stride - component_bytes
    decoder, encoder = zlib.decompressobj(), zlib.compressobj()
    output = bytearray(payload[:8])
    output.extend(
        chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, 2 if color == 6 else 0, 0, 0, interlace))
    )

    def rows():
        for x, y, dx, dy in PASSES if interlace else ((0, 0, 1, 1),):
            columns = max(0, (width - x + dx - 1) // dx)
            count = max(0, (height - y + dy - 1) // dy)
            if columns:
                for _ in range(count):
                    yield columns

    dimensions = iter(rows())
    columns = next(dimensions, None)
    pending = bytearray()

    def blocks():
        for kind, offset, size, _end in png_chunks(payload):
            if kind == b'IDAT':
                for start in range(offset + 8, offset + 8 + size, 65536):
                    yield memoryview(payload)[start : min(start + 65536, offset + 8 + size)]

    # unconsumed_tail is a bytes copy. Feeding a source-sized IDAT on every
    # row would repeatedly copy that entire suffix; keep each feed bounded.
    for compressed in blocks():
        while compressed:
            if columns is None:
                if decoder.decompress(compressed, 1) or decoder.unused_data:
                    raise ValueError('excess PNG pixel data')
                compressed = decoder.unconsumed_tail
                continue
            pending.extend(decoder.decompress(compressed, 1 + columns * stride - len(pending)))
            compressed = decoder.unconsumed_tail
            if len(pending) != 1 + columns * stride:
                continue
            if pending[0] > 4:
                raise ValueError('invalid PNG row filter')
            filtered = bytearray(1 + columns * retained)
            filtered[0] = pending[0]
            for component in range(retained):
                filtered[1 + component :: retained] = pending[1 + component :: stride]
            encoded = encoder.compress(filtered)
            if encoded:
                output.extend(chunk(b'IDAT', encoded))
            pending.clear()
            columns = next(dimensions, None)
    if columns is not None or pending or not decoder.eof or decoder.unused_data:
        raise ValueError('incomplete PNG pixels')
    output.extend(chunk(b'IDAT', encoder.flush()))
    output.extend(chunk(b'IEND', b''))
    return bytes(output)
