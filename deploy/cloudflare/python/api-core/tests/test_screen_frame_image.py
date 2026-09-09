"""Screenshot colors and admission through the actual bounded-image adapter."""

import asyncio
import hashlib
from io import BytesIO
import struct
from types import SimpleNamespace
import warnings
import zlib

from PIL import Image
import pytest

from test_frame_image_transform import native_images, picture
from screen_frame_image import canonicalize_screen_frame, prepare
from screen_frame_png import PASSES, chunk, opaque_png
from frame_image_metadata import png_chunks
from screen_frames_canonical import ScreenFrameCanonicalizationError, canonicalize_candidate


@pytest.mark.parametrize('format', ['JPEG', 'PNG'])
@pytest.mark.parametrize('orientation', range(1, 9))
def test_screenshot_orientation_metadata_digest_and_original_dimensions(native_images, format, orientation):
    payload = picture(format, orientation)
    expected = canonicalize_candidate(payload)
    actual = asyncio.run(canonicalize_screen_frame(SimpleNamespace(IMAGES=native_images), payload))
    assert (actual.width, actual.height) == (expected.width, expected.height)
    assert actual.sha256_hex == hashlib.sha256(actual.jpeg_bytes).hexdigest()
    assert b'PRIVATE_' not in actual.jpeg_bytes + actual.thumbnail_jpeg_bytes
    with Image.open(BytesIO(expected.jpeg_bytes)) as before, Image.open(BytesIO(actual.jpeg_bytes)) as after:
        assert before.tobytes() == after.tobytes()
        assert not after.getexif()


@pytest.mark.parametrize('mode', ['RGBA', 'LA', 'P'])
def test_screenshot_discards_alpha_without_compositing(native_images, mode):
    image = Image.new('RGBA', (180, 90), (150, 75, 25, 0))
    image.paste((35, 140, 90, 60), (90, 0, 180, 90))
    image = image.convert(mode)
    stream = BytesIO()
    image.save(stream, format='PNG')
    payload = stream.getvalue()
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Palette images with Transparency')
        expected = canonicalize_candidate(payload)
    actual = asyncio.run(canonicalize_screen_frame(SimpleNamespace(IMAGES=native_images), payload))
    assert actual.jpeg_bytes == expected.jpeg_bytes
    assert actual.thumbnail_jpeg_bytes == expected.thumbnail_jpeg_bytes


def test_large_source_is_never_decoded_by_core_and_dimensions_use_upstream_rounding(monkeypatch):
    stream = BytesIO()
    Image.new('RGBA', (8000, 8000), (25, 55, 80, 0)).save(stream, format='PNG')
    payload = stream.getvalue()

    def forbidden(*args, **kwargs):
        raise AssertionError('source pixel planes must be decoded by Images')

    monkeypatch.setattr(Image.Image, 'load', forbidden)
    prepared, orientation, dimensions = prepare(payload)
    assert prepared and orientation is None and dimensions == (1600, 1600)


def test_single_frame_apng_remains_a_still_image(native_images):
    stream = BytesIO()
    Image.new('RGB', (20, 20), (25, 55, 80)).save(stream, format='PNG', save_all=True, duration=100)
    payload = stream.getvalue()
    expected = canonicalize_candidate(payload)
    actual = asyncio.run(canonicalize_screen_frame(SimpleNamespace(IMAGES=native_images), payload))
    assert actual.jpeg_bytes == expected.jpeg_bytes


def test_single_large_idat_has_bounded_decoder_input(monkeypatch):
    import random
    import screen_frame_png

    image = Image.frombytes('RGBA', (300, 300), random.Random(73).randbytes(300 * 300 * 4))
    stream = BytesIO()
    image.save(stream, format='PNG')
    payload = stream.getvalue()
    compressed = b''.join(
        payload[o + 8 : o + 8 + size] for kind, o, size, _end in png_chunks(payload) if kind == b'IDAT'
    )
    payload = payload[:33] + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')
    decoder = zlib.decompressobj()

    class BoundedDecoder:
        def decompress(self, value, limit):
            assert len(value) <= 65536
            return decoder.decompress(value, limit)

        def __getattr__(self, name):
            return getattr(decoder, name)

    monkeypatch.setattr(screen_frame_png.zlib, 'decompressobj', lambda: BoundedDecoder())
    result = opaque_png(payload)
    with Image.open(BytesIO(result)) as actual:
        assert image.convert('RGB').tobytes() == actual.tobytes()


def filtered_png(*, interlace, depth, color):
    # Independent PNG encoder exercises all five predictors and Adam7 without
    # relying on Pillow's encoder options (which do not emit Adam7 PNGs).
    width, height = 19, 17
    component_bytes = depth // 8
    channels = 4 if color == 6 else 2
    stride = channels * component_bytes
    pixels = bytes((i * 37 + i // 17) % 256 for i in range(width * height * stride))
    scanlines = bytearray()
    for x, y, dx, dy in PASSES if interlace else ((0, 0, 1, 1),):
        previous = b''
        for row in range(y, height, dy):
            raw = b''.join(
                pixels[(row * width + col) * stride : (row * width + col + 1) * stride] for col in range(x, width, dx)
            )
            if not raw:
                continue
            previous = previous or bytes(len(raw))
            filter_kind = row % 5
            scanlines.append(filter_kind)
            for index, value in enumerate(raw):
                a = raw[index - stride] if index >= stride else 0
                b = previous[index]
                c = previous[index - stride] if index >= stride else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                paeth = a if pa <= pb and pa <= pc else b if pb <= pc else c
                predictor = (0, a, b, (a + b) // 2, paeth)[filter_kind]
                scanlines.append((value - predictor) % 256)
            previous = raw
    encoded = zlib.compress(scanlines)
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace))
        + b''.join(chunk(b'IDAT', encoded[i : i + 13]) for i in range(0, len(encoded), 13))
        + chunk(b'IEND', b'')
    )


@pytest.mark.parametrize('interlace', [0, 1])
@pytest.mark.parametrize('depth', [8, 16])
@pytest.mark.parametrize('color', [4, 6])
def test_alpha_removal_preserves_filters_and_adam7(interlace, depth, color):
    payload = filtered_png(interlace=interlace, depth=depth, color=color)
    result = opaque_png(payload)
    with Image.open(BytesIO(payload)) as source, Image.open(BytesIO(result)) as opaque:
        # Gray-16 PNG has a different Pillow I;16 -> RGB saturation rule; compare
        # its high byte to the original LA decoder's eight-bit luminance.
        if depth == 16 and color == 4:
            assert bytes(value >> 8 for value in opaque.getdata()) == source.getchannel('R').tobytes()
        else:
            assert source.convert('RGB').tobytes() == opaque.convert('RGB').tobytes()


def test_invalid_animated_and_service_failure_never_reach_upstream_decoder(native_images):
    animated = BytesIO()
    Image.new('RGB', (20, 20), 'red').save(
        animated,
        format='PNG',
        save_all=True,
        append_images=[Image.new('RGB', (20, 20), 'blue')],
        duration=100,
    )
    damaged = bytearray(picture('PNG'))
    damaged[-5] ^= 1
    env = SimpleNamespace(IMAGES=native_images)
    for payload in [b'not an image', animated.getvalue(), bytes(damaged)]:
        with pytest.raises(ScreenFrameCanonicalizationError):
            asyncio.run(canonicalize_screen_frame(env, payload))
    assert native_images.calls == []
    native_images.failure = RuntimeError('Images unavailable')
    with pytest.raises(ScreenFrameCanonicalizationError):
        asyncio.run(canonicalize_screen_frame(env, picture('PNG')))
