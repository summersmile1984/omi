"""Cloudflare Images owns full-resolution decoding; Core owns the output contract."""

import asyncio
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from frame_image_metadata import jpeg_without_metadata, png_chunks, webp_chunks, without_metadata
from frame_request_image import (
    _MAX_EGRESS_DIMENSION,
    _MAX_EGRESS_PIXELS,
    _MAX_IMAGE_PIXELS,
    _validated_image_content_type,
)

try:
    from js import Object
    from pyodide.ffi import to_js
    from workers import Response as WorkerResponse
except ModuleNotFoundError as error:
    if error.name not in {'js', 'pyodide'}:
        raise
    # CPython tests inject only the FFI conversions; production requires the
    # Workers SDK and the Images binding, and has no local-codec fallback.
    Object = to_js = WorkerResponse = None


ORIENTATION_TRANSFORMS = {
    2: ({'flip': 'h'},),
    3: ({'rotate': 180},),
    4: ({'flip': 'v'},),
    5: ({'flip': 'h'}, {'rotate': 270}),
    6: ({'rotate': 90},),
    7: ({'flip': 'h'}, {'rotate': 90}),
    8: ({'rotate': 270},),
}


def prepare_input(payload: bytes):
    if payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        # Pillow opens WebP with WebPAnimDecoder, which allocates two full
        # RGBA canvases even before load(). Read only RIFF metadata here;
        # Images.info owns bitstream validity and dimensions for this format.
        try:
            metadata = Image.Image()
            for kind, offset, size, _end in webp_chunks(payload):
                if kind in {b'EXIF', b'XMP '}:
                    metadata.info['exif' if kind == b'EXIF' else 'xmp'] = payload[offset + 8 : offset + 8 + size]
            orientation = metadata.getexif().get(274)
            return without_metadata(payload, 'image/webp'), orientation, None
        except (OSError, ValueError) as error:
            raise HTTPException(415, 'frame_upload_invalid_image') from error
    content_type = _validated_image_content_type(payload)
    try:
        with Image.open(BytesIO(payload)) as source:
            width, height = source.size
            if content_type == 'image/png':
                # PNG getexif() calls load() to discover metadata after IDAT.
                # Reuse Pillow's bounded metadata parsers while skipping the
                # compressed pixel chunks; never allocate the pixel plane.
                with BytesIO(payload) as stream:
                    metadata = PngImagePlugin.PngStream(stream)
                    for kind, offset, size, _end in png_chunks(payload):
                        if kind in {b'eXIf', b'tEXt', b'zTXt', b'iTXt'}:
                            stream.seek(offset + 8)
                            metadata.call(kind, offset + 8, size)
                    source.info.update(metadata.im_info)
            orientation = Image.Image.getexif(source).get(274)
        prepared = without_metadata(payload, content_type)
    except (OSError, ValueError) as error:
        raise HTTPException(415, 'frame_upload_invalid_image') from error
    return prepared, orientation, output_dimensions(width, height, orientation)


def output_dimensions(width, height, orientation):
    if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
        raise HTTPException(413, 'frame_upload_dimensions_too_large')
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    scale = min(1.0, _MAX_EGRESS_DIMENSION / max(width, height), (_MAX_EGRESS_PIXELS / (width * height)) ** 0.5)
    return max(1, int(width * scale)), max(1, int(height * scale))


async def canonicalize_frame_image(env, payload: bytes) -> bytes:
    prepared, orientation, dimensions = prepare_input(payload)
    images = getattr(env, 'IMAGES', None)
    if images is None or WorkerResponse is None:
        raise HTTPException(503, 'frame_image_transform_unavailable')
    try:
        if dimensions is None:
            info = await asyncio.wait_for(images.info(WorkerResponse(prepared).body), timeout=15)
            if info.format != 'image/webp':
                raise HTTPException(415, 'frame_upload_invalid_image')
            dimensions = output_dimensions(int(info.width), int(info.height), orientation)
        transformer = images.input(WorkerResponse(prepared).body)
        for operation in ORIENTATION_TRANSFORMS.get(orientation, ()):
            transformer = transformer.transform(to_js(operation, dict_converter=Object.fromEntries))
        transformer = transformer.transform(
            to_js(
                {'width': dimensions[0], 'height': dimensions[1], 'fit': 'squeeze', 'background': 'white'},
                dict_converter=Object.fromEntries,
            )
        )
        output = await asyncio.wait_for(
            transformer.output(
                to_js({'format': 'image/jpeg', 'quality': 85, 'anim': False}, dict_converter=Object.fromEntries)
            ),
            timeout=30,
        )
        result = await asyncio.wait_for(output.response().bytes(), timeout=15)
    except HTTPException:
        raise
    except Exception as error:
        if getattr(error, 'code', None) == 9412:
            raise HTTPException(415, 'frame_upload_invalid_image') from error
        raise HTTPException(503, 'frame_image_transform_unavailable') from error
    try:
        result = jpeg_without_metadata(result)
        with Image.open(BytesIO(result)) as image:
            if image.format != 'JPEG' or image.size != dimensions or image.mode not in {'RGB', 'L'}:
                raise ValueError('invalid canonical output')
            image.verify()
    except (OSError, ValueError) as error:
        raise HTTPException(503, 'frame_image_transform_invalid_output') from error
    return result
