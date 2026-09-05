"""Supervised self-host owner for canonical-memory projection outbox delivery.

Canonical source replacement, leases, retries, acknowledgements and provider
mutations remain in their existing upstream owners. This process contributes
only bounded UID scheduling and process supervision for standard Server OS.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .bootstrap import Role, bootstrap
from .profile import ProfileError

logger = logging.getLogger(__name__)

MAX_UIDS_PER_CYCLE = 400


@dataclass(frozen=True)
class Config:
    poll_seconds: float
    uid_limit: int

    @classmethod
    def from_env(cls) -> 'Config':
        raw_poll = os.environ.get('MEMORY_OUTBOX_POLL_SECONDS', '5').strip()
        raw_limit = os.environ.get('MEMORY_OUTBOX_UID_LIMIT', '50').strip()
        try:
            poll_seconds = float(raw_poll)
        except ValueError as error:
            raise ProfileError('MEMORY_OUTBOX_POLL_SECONDS must be a number') from error
        try:
            uid_limit = int(raw_limit)
        except ValueError as error:
            raise ProfileError('MEMORY_OUTBOX_UID_LIMIT must be an integer') from error
        if not 1 <= poll_seconds <= 3600:
            raise ProfileError('MEMORY_OUTBOX_POLL_SECONDS must be within 1..3600')
        if not 1 <= uid_limit <= MAX_UIDS_PER_CYCLE:
            raise ProfileError(f'MEMORY_OUTBOX_UID_LIMIT must be within 1..{MAX_UIDS_PER_CYCLE}')
        return cls(poll_seconds=poll_seconds, uid_limit=uid_limit)


@dataclass(frozen=True)
class CycleReport:
    user_count: int
    delivered_count: int
    retryable_failure_count: int
    dead_letter_count: int
    ack_failed_count: int
    error_count: int

    @property
    def succeeded(self) -> bool:
        return not (self.retryable_failure_count or self.dead_letter_count or self.ack_failed_count or self.error_count)


class RegistryInventoryUnavailable(RuntimeError):
    """The content-free canonical-memory registry could not be paged safely."""


class RegistryPager:
    """Read bounded UID pages without creating another persistent cursor owner."""

    def __init__(self) -> None:
        self.after_uid = ''

    @staticmethod
    def _uid(snapshot: Any) -> str:
        from utils.memory.memory_system import (
            CANONICAL_MEMORY_MAINTENANCE_REGISTRY_COLLECTION,
            CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
        )

        payload = snapshot.to_dict() if hasattr(snapshot, 'to_dict') else None
        uid = payload.get('uid') if isinstance(payload, dict) else None
        schema_version = payload.get('schema_version') if isinstance(payload, dict) else None
        path = getattr(getattr(snapshot, 'reference', None), 'path', '')
        if (
            not isinstance(uid, str)
            or not uid.strip()
            or '/' in uid
            or schema_version != CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION
            or path != f'{CANONICAL_MEMORY_MAINTENANCE_REGISTRY_COLLECTION}/{uid.strip()}'
        ):
            raise RegistryInventoryUnavailable('canonical memory registry entry is malformed')
        return uid.strip()

    def __call__(self, db_client: Any, limit: int) -> Sequence[str]:
        from utils.memory.memory_system import CANONICAL_MEMORY_MAINTENANCE_REGISTRY_COLLECTION

        try:
            registry = db_client.collection(CANONICAL_MEMORY_MAINTENANCE_REGISTRY_COLLECTION)
            query = registry.where('uid', '>', self.after_uid) if self.after_uid else registry
            page = list(query.order_by('uid').limit(limit).stream())
            if self.after_uid and len(page) < limit:
                page.extend(list(registry.order_by('uid').limit(limit - len(page)).stream()))
            uids = tuple(dict.fromkeys(self._uid(snapshot) for snapshot in page))
        except RegistryInventoryUnavailable:
            raise
        except Exception as error:
            raise RegistryInventoryUnavailable('canonical memory registry query failed') from error
        if uids:
            self.after_uid = uids[-1]
        return uids


_production_inventory = RegistryPager()


def _production_drain(uid: str, *, db_client: Any, run_id: str, now: datetime) -> Mapping[str, Any]:
    from utils.memory.short_term_promotion import _drain_canonical_outbox

    return _drain_canonical_outbox(uid, db_client=db_client, run_id=run_id, now=now)


def run_cycle(
    *,
    db_client: Any,
    config: Config,
    inventory: Callable[[Any, int], Sequence[str]] = _production_inventory,
    drain: Callable[..., Mapping[str, Any]] = _production_drain,
    now: datetime | None = None,
    run_id: str | None = None,
) -> CycleReport:
    """Drain one bounded registry page through the existing outbox authority."""
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cycle_id = run_id or f'self-host-outbox:{uuid.uuid4().hex}'
    uids = tuple(inventory(db_client, config.uid_limit))
    totals = {
        'delivered_count': 0,
        'retryable_failure_count': 0,
        'dead_letter_count': 0,
        'ack_failed_count': 0,
        'error_count': 0,
    }
    for uid in uids:
        summary = drain(uid, db_client=db_client, run_id=cycle_id, now=observed)
        for key in ('delivered_count', 'retryable_failure_count', 'dead_letter_count', 'ack_failed_count'):
            totals[key] += int(summary.get(key) or 0)
        errors = summary.get('errors')
        totals['error_count'] += len(errors) if isinstance(errors, list) else int(bool(errors))
    return CycleReport(user_count=len(uids), **totals)


def run_loop(
    *,
    db_client: Any,
    config: Config,
    stop: threading.Event,
    cycle: Callable[..., CycleReport] = run_cycle,
) -> None:
    while not stop.is_set():
        try:
            report = cycle(db_client=db_client, config=config)
            log = logger.info if report.succeeded else logger.warning
            log(
                'canonical memory outbox cycle users=%d delivered=%d retryable=%d dead_letter=%d ack_failed=%d errors=%d',
                report.user_count,
                report.delivered_count,
                report.retryable_failure_count,
                report.dead_letter_count,
                report.ack_failed_count,
                report.error_count,
            )
        except Exception as error:
            # Do not expose provider bodies, memory content, UIDs or DSNs. A
            # supervised process remains alive so transient inventory/PG faults
            # can recover; --once remains the fail-fast operator probe.
            logger.warning('canonical memory outbox cycle failed error_type=%s', type(error).__name__)
        stop.wait(config.poll_seconds)


def _process_is_running() -> bool:
    from pathlib import Path

    try:
        command = Path('/proc/1/cmdline').read_bytes().replace(b'\x00', b' ')
    except OSError:
        return False
    return b'fork.memory_maintenance_worker' in command


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='validate deployment and provider admission, then exit')
    parser.add_argument('--health', action='store_true', help='check that the supervised worker remains PID 1')
    parser.add_argument('--once', action='store_true', help='run one bounded cycle and fail if delivery reports errors')
    args = parser.parse_args(argv)
    if args.health:
        return 0 if _process_is_running() else 1

    config = Config.from_env()
    bootstrap(Role.MEMORY_MAINTENANCE)
    if args.check:
        print('canonical memory outbox admission OK')
        return 0

    from database._client import db

    if args.once:
        report = run_cycle(db_client=db, config=config)
        print(
            'canonical memory outbox cycle '
            f'users={report.user_count} delivered={report.delivered_count} '
            f'retryable={report.retryable_failure_count} dead_letter={report.dead_letter_count} '
            f'ack_failed={report.ack_failed_count} errors={report.error_count}'
        )
        return 0 if report.succeeded else 1

    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    run_loop(db_client=db, config=config, stop=stop)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(run())


if __name__ == '__main__':
    main()
