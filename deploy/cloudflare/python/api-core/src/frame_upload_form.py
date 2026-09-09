"""Bound the frame multipart body before Starlette can spool to local files."""

from fastapi import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from screen_frame_views import ScreenshotRoute

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024


class FrameMultipartParser(MultiPartParser):
    spool_max_size = MAX_BODY_BYTES + 1


async def bounded_body(request):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(413, 'frame_upload_too_large')
        yield chunk


class FrameUploadRequest(Request):
    async def _get_form(self, *, max_files=1000, max_fields=1000, max_part_size=1024 * 1024):
        if self._form is not None:
            return self._form
        if self.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'multipart/form-data':
            return await super()._get_form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)
        parser = FrameMultipartParser(
            self.headers, bounded_body(self), max_files=1, max_fields=8, max_part_size=64 * 1024
        )
        try:
            self._form = await parser.parse()
            return self._form
        except BaseException as error:
            # Starlette closes these on its own multipart exceptions, but an
            # interrupted/oversized source can fail outside those callbacks.
            for file in parser._files_to_close_on_error:
                file.close()
            if isinstance(error, MultiPartException):
                raise HTTPException(400, 'invalid_frame_multipart') from error
            raise


class FrameUploadRoute(ScreenshotRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            return await handler(FrameUploadRequest(request.scope, request.receive))

        return handle
