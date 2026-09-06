"""Container/privacy contracts and the Images service seam; hosted codec proof is separate."""

import asyncio
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageOps, WebPImagePlugin
import pytest

from test_frame_request_metadata import target
import frame_image_transform as transform
from frame_image_metadata import jpeg_without_metadata, without_metadata


class Images:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.after_output = None

    async def info(self, payload):
        with Image.open(BytesIO(payload)) as image:
            return SimpleNamespace(format=Image.MIME[image.format], width=image.width, height=image.height)

    def input(self, payload):
        call = {'payload': payload, 'transforms': []}
        self.calls.append(call)
        owner = self

        class Transformer:
            def transform(self, operation):
                call['transforms'].append(operation)
                return self

            async def output(self, options):
                call['output'] = options
                if owner.failure:
                    raise owner.failure
                with Image.open(BytesIO(payload)) as source:
                    image = source.copy()
                for operation in call['transforms']:
                    if 'flip' in operation:
                        image = ImageOps.mirror(image) if operation['flip'] == 'h' else ImageOps.flip(image)
                    if 'rotate' in operation:
                        image = image.rotate(-operation['rotate'], expand=True)
                    if 'width' in operation:
                        image = image.resize((operation['width'], operation['height']), Image.Resampling.LANCZOS)
                        if 'A' in image.getbands():
                            background = Image.new('RGB', image.size, operation['background'])
                            background.paste(image, mask=image.getchannel('A'))
                            image = background
                        elif image.mode not in {'RGB', 'L'}:
                            image = image.convert('RGB')
                exif = Image.Exif()
                exif[315] = 'PRIVATE_PROVIDER_METADATA'
                encoded = BytesIO()
                image.save(
                    encoded,
                    format={'image/jpeg': 'JPEG', 'image/png': 'PNG'}[options['format']],
                    quality=options.get('quality', 85),
                    optimize=True,
                    exif=exif,
                )
                if owner.after_output:
                    await owner.after_output()

                async def data():
                    return encoded.getvalue()

                return SimpleNamespace(response=lambda: SimpleNamespace(bytes=data))

        return Transformer()


@pytest.fixture
def native_images(monkeypatch):
    monkeypatch.setattr(transform, 'WorkerResponse', lambda payload: SimpleNamespace(body=payload))
    monkeypatch.setattr(transform, 'to_js', lambda value, **kwargs: value)
    monkeypatch.setattr(transform, 'Object', SimpleNamespace(fromEntries=None))
    return Images()


def picture(format, orientation=1):
    image = Image.new('RGB', (120, 80), (15, 100, 20))
    image.paste((240, 15, 10), (0, 0, 60, 40))
    image.paste((10, 15, 240), (60, 40, 120, 80))
    exif = Image.Exif()
    exif[274], exif[315], exif[33432] = orientation, 'PRIVATE_AUTHOR', 'PRIVATE_COPYRIGHT'
    output = BytesIO()
    image.save(output, format=format, exif=exif)
    return output.getvalue()


@pytest.mark.parametrize('format', ['JPEG', 'PNG', 'WEBP'])
@pytest.mark.parametrize('orientation', range(1, 9))
def test_native_transform_preserves_upstream_orientation_and_removes_metadata(native_images, format, orientation):
    payload = picture(format, orientation)
    result = asyncio.run(transform.canonicalize_frame_image(SimpleNamespace(IMAGES=native_images), payload))
    with Image.open(BytesIO(payload)) as original, Image.open(BytesIO(result)) as actual:
        expected = ImageOps.exif_transpose(original).convert('RGB')
        assert actual.size == expected.size
        assert not actual.getexif() and b'PRIVATE_' not in result
        for x, y in [
            (15, 15),
            (actual.width - 15, 15),
            (15, actual.height - 15),
            (actual.width - 15, actual.height - 15),
        ]:
            assert max(abs(a - b) for a, b in zip(actual.getpixel((x, y)), expected.getpixel((x, y)))) < 15
    with Image.open(BytesIO(native_images.calls[0]['payload'])) as admitted:
        assert not admitted.getexif()


def test_prepare_25_megapixel_png_never_loads_the_pixel_plane(monkeypatch):
    output = BytesIO()
    Image.new('RGBA', (5000, 5000), (15, 100, 20, 128)).save(output, format='PNG')

    def forbidden(*args, **kwargs):
        raise AssertionError('full pixel decoding must belong to Images')

    monkeypatch.setattr(Image.Image, 'load', forbidden)
    prepared, orientation, dimensions = transform.prepare_input(output.getvalue())
    assert prepared and orientation is None and dimensions == (1581, 1581)


def test_webp_preparation_never_creates_pillow_pixel_canvases(monkeypatch):
    payload = picture('WEBP', 6)

    def forbidden(*args, **kwargs):
        raise AssertionError('WebP header parsing must not allocate decoder canvases')

    monkeypatch.setattr(WebPImagePlugin.WebPImageFile, '_open', forbidden)
    prepared, orientation, dimensions = transform.prepare_input(payload)
    assert prepared and orientation == 6 and dimensions is None


def test_progressive_jpeg_metadata_between_scans_or_before_end_is_removed():
    output = BytesIO()
    Image.new('RGB', (30, 20), (123, 20, 210)).save(output, format='JPEG', progressive=True)
    original = output.getvalue()
    comment = b'PRIVATE_TRAILING_COMMENT'
    annotated = original[:-2] + b'\xff\xfe' + (len(comment) + 2).to_bytes(2, 'big') + comment + original[-2:]
    result = jpeg_without_metadata(annotated)
    with Image.open(BytesIO(original)) as before, Image.open(BytesIO(result)) as after:
        assert before.tobytes() == after.tobytes()
    assert comment not in result


@pytest.mark.parametrize('format,mime', [('JPEG', 'image/jpeg'), ('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_truncated_container_is_rejected(format, mime):
    with pytest.raises(ValueError):
        without_metadata(picture(format)[:-15], mime)


def test_unavailable_native_transform_has_no_local_codec_fallback(native_images):
    payload = picture('PNG')
    native_images.failure = RuntimeError('provider unavailable')
    for env in [SimpleNamespace(), SimpleNamespace(IMAGES=native_images)]:
        with pytest.raises(Exception) as error:
            asyncio.run(transform.canonicalize_frame_image(env, payload))
        assert error.value.status_code == 503
