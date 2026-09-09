"""Brand download metadata after the upstream export owner has succeeded."""

import re

from .profile import ProfileError, _load_table, current


class ExportBrandMiddleware:
    def __init__(self, app, *, path, filename):
        self.app, self.path = app, path
        self.disposition = f'attachment; filename="{filename}"'.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'GET' or scope.get('path') != self.path:
            return await self.app(scope, receive, send)

        async def branded_send(message):
            if message['type'] == 'http.response.start' and message['status'] == 200:
                message = dict(message)
                headers = [
                    (key, value) for key, value in message.get('headers', []) if key.lower() != b'content-disposition'
                ]
                message['headers'] = [*headers, (b'content-disposition', self.disposition)]
            await send(message)

        await self.app(scope, receive, branded_send)


def install(app):
    if current()['target'] != 'self_hosted':
        return
    # The generated image identity is already admitted by profile.current.
    # Neither request headers nor an independent environment override own it.
    brand = _load_table().get('brand')
    if not isinstance(brand, str) or not re.fullmatch(r'[a-z0-9-]+', brand):
        raise ProfileError('export requires a valid generated brand identity')
    routes = [
        route
        for route in app.routes
        if getattr(getattr(route, 'endpoint', None), '__module__', '') == 'routers.users'
        and getattr(getattr(route, 'endpoint', None), '__name__', '') == 'export_all_user_data'
        and 'GET' in (getattr(route, 'methods', None) or ())
    ]
    if len(routes) != 1:
        raise ProfileError('export route owner missing or ambiguous')
    app.add_middleware(ExportBrandMiddleware, path=routes[0].path, filename=f'{brand}-export.json')
