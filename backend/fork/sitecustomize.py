"""Patch non-ASGI fork processes (queue workers, jobs, scripts).

Python imports `sitecustomize` automatically when it is on the path, so adding
`backend/fork` to PYTHONPATH gives a worker the same patches the API entry point
applies -- without a fork edit to any upstream script.

    PYTHONPATH=backend/fork python -m utils.cloud_tasks_redis
"""

from __future__ import annotations

import logging
import os

if os.getenv("OMI_FORK_DISABLE_SITECUSTOMIZE", "") != "1":
    try:
        from fork.main import bootstrap

        bootstrap()
    except Exception:  # noqa: BLE001 - must not mask the real startup error
        logging.getLogger(__name__).exception("fork sitecustomize bootstrap failed")
        raise
