"""Render a fork-owned ``self_hosted.local`` profile table for the dev harness.

`backend/fork/profile.py` reads a JSON table from
``backend/fork/deployment_profiles.generated.json`` (or whatever
``OMI_DEPLOYMENT_PROFILES_PATH`` points at). The committed table only ships
``omi_cloud`` profiles, which means a dev-mode backend that wants the fork's
``firestore_pg`` facade + ``AUTH_PROVIDER=better_auth`` cannot select a
profile — the upstream ``local_prod`` row says ``store=firestore`` and
``identity_provider=firebase``.

This module writes a fork-owned, runtime-only profile table into the harness
state directory that contains a ``self_hosted.local`` row matching every
assertion ``backend/fork/bootstrap.py`` and ``backend/fork/profile.py`` make
at boot. The harness then points ``OMI_DEPLOYMENT_PROFILES_PATH`` at the
runtime file so the backend uvicorn child resolves to it.

Schema requirements sourced from:
- ``deploy/profiles/self_hosted.yaml`` — the upstream-known-good profile.
- ``backend/fork/bootstrap.py:62-90`` — the data-plane fail-closed asserts.
- ``backend/fork/profile.py:106-108`` — the identity-provider pairing assert.

The produced row is the minimum that satisfies those checks. Capability values
match ``deploy/profiles/self_hosted.yaml`` so the upstream contract is honored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_VERSION = 1

BRAND = "omi-upstream"

TARGET = "self_hosted"

IDENTITY_PROVIDER = "better_auth"

REQUIRED_CAPABILITIES = {
    "allow_direct_model_providers": False,
    "allow_byok": False,
    "allow_google_connectors": False,
    "allow_cloud_connectors": False,
    "marketplace_enabled": False,
    "model_downloads": False,
    "account_activation_fence": False,
    "embedding_dims": 3072,
    "sync_upload_batch_limit": 5,
    "max_request_bytes": 0,
    "push_provider": "disabled",
    # Local dev has no admitted speech bundle; ``backend/fork/bootstrap.py``
    # requires ``stt_providers`` to equal the empty list and ``tts_provider``
    # to equal ``"disabled"`` when ``validate_speech(None)``. The previous
    # ``"kokoro"`` here was wrong: it forced the bootstrap to call a real
    # kokoro TTS path the harness never wires, surfacing as a 5-minute
    # firestore timeout on first request. Keeping ``realtime_relay=operator``
    # so the chat contract tests still pass.
    "tts_provider": "disabled",
    "realtime_relay": "operator",
    "stt_providers": [],
}

REQUIRED_DATA_PLANE = {
    "store": "firestore_pg",
    "object_store": "minio",
    "queue": "redis",
    "vector": "qdrant",
    "cache": "redis",
}

LOCAL_STAGE = {
    "api_base_url": "http://127.0.0.1:8000/",
    "auth_api_base_url": "http://127.0.0.1:3000/",
    "auth_base_url": "http://127.0.0.1:3000",
    "web_app_base_url": "http://127.0.0.1:8080/",
    "mcp_base_url": "http://127.0.0.1:8000/mcp",
    "share_base_url": "http://127.0.0.1:8000/share",
    "objects_base_url": "http://127.0.0.1:9000",
    "auth_callback_scheme": "omi",
}


def render_self_hosted_local_profile() -> dict:
    """Build the in-memory profile table the backend uvicorn will resolve."""
    return {
        "schema_version": SCHEMA_VERSION,
            "brand": BRAND,
            "target": TARGET,
            "profiles": {
                "self_hosted.local": {
                    "name": "self_hosted.local",
                    "target": TARGET,
                    "stage": "local",
                    "identity_provider": IDENTITY_PROVIDER,
                    "managed": False,
                    "legacy": False,
                    "requires_https": False,
                    "uses_firebase_auth_emulator": False,
                    "firebase_project_id": "demo-omi-local",
                    **LOCAL_STAGE,
                    "capabilities": dict(REQUIRED_CAPABILITIES),
                    "data_plane": dict(REQUIRED_DATA_PLANE),
                },
            },
        }


def write_self_hosted_local_profile(target_dir: Path) -> Path:
    """Render and write the runtime profile table; return the path written."""
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "deployment_profiles.generated.json"
    out_path.write_text(
        json.dumps(render_self_hosted_local_profile(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def self_hosted_local_dsn(pg_port: int) -> str:
    """DSN the backend uvicorn will use to reach the harness-managed Postgres.

    Mirrors the psycopg URL format expected by ``firestore_pg.compat.install`` /
    the ``firestore_pg`` engine. The user/password/database match the values
    the harness writes when it starts the Postgres container; see
    ``_start_postgres_container``.
    """
    user = "omi"
    password = "omi_dev_only_local_dev_password"
    database = "omi"
    host = "127.0.0.1"
    return f"postgresql://{user}:{password}@{host}:{pg_port}/{database}"