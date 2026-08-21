#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Local cutover-only OpenAI Realtime wire fixture.

The fixture proves that the production relay moves an authenticated client
event in both directions. It is never part of the production Compose profile.
"""

from __future__ import annotations

import json
import os
from typing import Any

from websockets.sync.server import serve


def handle(connection: Any) -> None:
    expected = f"Bearer {os.environ['REALTIME_FIXTURE_API_KEY']}"
    if connection.request.headers.get('Authorization') != expected:
        connection.close(code=1008, reason='fixture_auth_failed')
        return
    for raw in connection:
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            connection.close(code=1003, reason='fixture_json_required')
            return
        if not isinstance(event, dict) or event.get('type') != 'session.update':
            connection.close(code=1008, reason='fixture_openai_event_required')
            return
        connection.send(
            json.dumps(
                {
                    'type': 'session.updated',
                    'acceptance_marker': event.get('acceptance_marker'),
                },
                separators=(',', ':'),
            )
        )


def main() -> None:
    with serve(handle, '0.0.0.0', 8765) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
