"""Expose disabled speech/push before accepting audio, tokens, or delivery requests."""

from starlette.responses import JSONResponse
from .capabilities import Capability, CapabilityDisabled

# Actual upstream route owners are resolved at install; a rename is fatal.
OWNERS = {
    ('routers.tts', 'tts_synthesize'): Capability.TTS,
    ('routers.desktop_tts_updates', 'tts_synthesize'): Capability.TTS,
    ('routers.transcribe', 'listen_handler'): Capability.STT,
    ('routers.transcribe', 'web_listen_handler'): Capability.STT,
    ('routers.chat', 'create_voice_message_stream'): Capability.STT,
    ('routers.chat', 'transcribe_voice_message'): Capability.STT,
    ('routers.chat', 'transcribe_voice_message_stream'): Capability.STT,
    ('routers.notifications', 'save_token'): Capability.PUSH,
    ('routers.notifications', 'send_notification_to_user'): Capability.PUSH,
    ('routers.notifications', 'send_app_notification_to_user'): Capability.PUSH,
}


class DisabledCapabilityMiddleware:
    def __init__(self, app, routes):
        self.app, self.routes = app, routes

    async def __call__(self, scope, receive, send):
        capability = self.routes.get((scope['type'], scope.get('path', '').rstrip('/')))
        if capability and (scope['type'] == 'websocket' or scope.get('method') == 'POST'):
            failure = CapabilityDisabled(capability)
            if scope['type'] == 'websocket':
                # A completed handshake delivers an explicit close code even to
                # clients without the ASGI websocket-denial-response extension.
                await receive()
                await send({'type': 'websocket.accept'})
                await send({'type': 'websocket.close', 'code': 1008, 'reason': capability.value + '_disabled'})
            else:
                await JSONResponse(status_code=503, content=failure.detail())(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install(app):
    routes, found = {}, set()
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        owner = (getattr(endpoint, '__module__', ''), getattr(endpoint, '__name__', ''))
        if owner in OWNERS:
            kind = 'http' if getattr(route, 'methods', None) else 'websocket'
            routes[(kind, route.path.rstrip('/'))] = OWNERS[owner]
            found.add(owner)
    if found != set(OWNERS):
        raise RuntimeError('disabled capability route owner missing: ' + repr(set(OWNERS) - found))
    app.add_middleware(DisabledCapabilityMiddleware, routes=routes)
