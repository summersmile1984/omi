"""Expose retryable authentication closes on the actual WebSocket wire."""

from fastapi import FastAPI, WebSocket, WebSocketException
from starlette.websockets import WebSocketState


def install(app: FastAPI) -> None:
    previous = app.exception_handlers.get(WebSocketException)

    async def retryable_close(websocket: WebSocket, error: WebSocketException) -> None:
        # ASGI maps a close before accept to HTTP 403, hiding the retryable code
        # from real clients. No application data is sent on this short connection.
        if error.code == 1013 and websocket.application_state == WebSocketState.CONNECTING:
            await websocket.accept()
        if previous is not None:
            await previous(websocket, error)
        else:
            await websocket.close(code=error.code, reason=error.reason)

    app.add_exception_handler(WebSocketException, retryable_close)
