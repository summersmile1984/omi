"""Decode source screenshots in Images; encode approved-size pixels with upstream rules."""

import asyncio
from io import BytesIO

from PIL import Image, PngImagePlugin

import frame_image_transform as native
from frame_image_metadata import jpeg_without_metadata, png_chunks
from screen_frame_png import opaque_png
from screen_frames_canonical import (
    CANONICAL_LONG_EDGE_PX,
    ScreenFrameCanonicalizationError,
    canonicalize_candidate,
)


def prepare(payload):
    # Reading JPEG/PNG headers does not load their pixel plane. All decoding of
    # source-sized images belongs to the native Images binding.
    with Image.open(BytesIO(payload)) as source:
        if source.format not in {'JPEG', 'PNG'}:
            raise ValueError('unsupported screenshot encoding')
        if getattr(source, 'is_animated', False) or getattr(source, 'n_frames', 1) > 1:
            raise ValueError('animated screenshot')
        width, height = source.size
        if source.format == 'PNG':
            with BytesIO(payload) as stream:
                metadata = PngImagePlugin.PngStream(stream)
                for kind, offset, size, _end in png_chunks(payload):
                    if kind in {b'eXIf', b'tEXt', b'zTXt', b'iTXt'}:
                        stream.seek(offset + 8)
                        metadata.call(kind, offset + 8, size)
                source.info.update(metadata.im_info)
        orientation = Image.Image.getexif(source).get(274)
        prepared = opaque_png(payload) if source.format == 'PNG' else jpeg_without_metadata(payload)
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    scale = min(1, CANONICAL_LONG_EDGE_PX / max(width, height))
    return prepared, orientation, (max(1, round(width * scale)), max(1, round(height * scale)))


async def canonicalize_screen_frame(env, payload):
    try:
        prepared, orientation, dimensions = prepare(payload)
        images = getattr(env, 'IMAGES', None)
        if images is None or native.WorkerResponse is None:
            raise ValueError('screenshot Images binding unavailable')
        transformer = images.input(native.WorkerResponse(prepared).body)
        for operation in native.ORIENTATION_TRANSFORMS.get(orientation, ()):
            transformer = transformer.transform(native.to_js(operation, dict_converter=native.Object.fromEntries))
        transformer = transformer.transform(
            native.to_js(
                {'width': dimensions[0], 'height': dimensions[1], 'fit': 'squeeze'},
                dict_converter=native.Object.fromEntries,
            )
        )
        output = await asyncio.wait_for(
            transformer.output(
                native.to_js({'format': 'image/png', 'anim': False}, dict_converter=native.Object.fromEntries)
            ),
            timeout=30,
        )
        bounded = await asyncio.wait_for(output.response().bytes(), timeout=15)
        with Image.open(BytesIO(bounded)) as image:
            if image.format != 'PNG' or image.size != dimensions or image.mode not in {'RGB', 'L', 'RGBA', 'LA', 'P'}:
                raise ValueError('invalid bounded screenshot')
            # Do not let service-supplied metadata rotate the already oriented
            # image a second time when the upstream canonicalizer reads it.
        return canonicalize_candidate(opaque_png(bounded))
    except Exception as error:
        raise ScreenFrameCanonicalizationError('decode_failed') from error
