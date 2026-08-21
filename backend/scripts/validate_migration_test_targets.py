#!/usr/bin/env python3
"""Fail closed unless migration-test targets are local or explicitly disposable."""

from __future__ import annotations

import argparse
import ipaddress
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

DISPOSABLE_ACK = 'I_ACKNOWLEDGE_THIS_IS_DISPOSABLE'
LOCAL_HOST_ALIASES = frozenset({'localhost', 'host.docker.internal'})
# Keep this allowlist deliberately narrow. libpq accepts connection keywords in
# a URI query and those keywords take precedence over the visible authority.
# Unknown/current targeting or configuration-file parameters therefore fail
# closed; these audited TLS/client options cannot redirect the destination.
SAFE_DATABASE_QUERY_PARAMETERS = frozenset(
    {
        'application_name',
        'channel_binding',
        'connect_timeout',
        'fallback_application_name',
        'keepalives',
        'keepalives_count',
        'keepalives_idle',
        'keepalives_interval',
        'sslcert',
        'sslcrl',
        'sslcrldir',
        'sslkey',
        'sslmode',
        'sslrootcert',
        'sslsni',
        'tcp_user_timeout',
    }
)


class UnsafeMigrationTarget(ValueError):
    """A destructive migration test could address a non-disposable target."""


@dataclass(frozen=True)
class Target:
    label: str
    host: str
    port: int | None


def _canonical_host(host: str) -> str:
    return host.lower().rstrip('.')


def _is_local_host(host: str) -> bool:
    canonical = _canonical_host(host)
    if canonical in LOCAL_HOST_ALIASES:
        return True
    try:
        return ipaddress.ip_address(canonical).is_loopback
    except ValueError:
        return False


def parse_database_url(value: str, *, label: str) -> Target:
    parsed = urlsplit(value)
    if parsed.scheme.split('+', 1)[0] not in {'postgres', 'postgresql'}:
        raise UnsafeMigrationTarget(f'{label} must use a PostgreSQL URL')
    host = _canonical_host(parsed.hostname or '')
    if not host or ',' in host:
        raise UnsafeMigrationTarget(f'{label} must contain exactly one explicit hostname')
    if not parsed.path or parsed.path == '/':
        raise UnsafeMigrationTarget(f'{label} must name an explicit database')
    if parsed.fragment:
        raise UnsafeMigrationTarget(f'{label} must not contain a URL fragment')
    try:
        query_parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
    except ValueError as exc:
        raise UnsafeMigrationTarget(f'{label} has invalid query parameters') from exc
    unsafe_query_parameters = sorted({key.lower() for key, _value in query_parameters} - SAFE_DATABASE_QUERY_PARAMETERS)
    if unsafe_query_parameters:
        raise UnsafeMigrationTarget(
            f'{label} contains unsupported libpq query parameters that may override its target/configuration: '
            + ', '.join(unsafe_query_parameters)
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeMigrationTarget(f'{label} has an invalid port') from exc
    return Target(label, host, port)


def parse_host_authority(value: str, *, label: str) -> Target:
    parsed = urlsplit(f'//{value.strip()}')
    host = _canonical_host(parsed.hostname or '')
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeMigrationTarget(f'{label} must be a host[:port] authority')
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeMigrationTarget(f'{label} has an invalid port') from exc
    return Target(label, host, port)


def require_disposable(target: Target, *, acknowledged: bool) -> None:
    if _is_local_host(target.host) or acknowledged:
        return
    raise UnsafeMigrationTarget(
        f'{target.label} host {target.host!r} is not loopback/local; set '
        f'ALLOW_REMOTE_MIGRATION_TEST_TARGET={DISPOSABLE_ACK} only for an explicitly disposable target'
    )


def validate_external_targets(env: Mapping[str, str] = os.environ) -> tuple[Target, ...]:
    acknowledged = env.get('ALLOW_REMOTE_MIGRATION_TEST_TARGET') == DISPOSABLE_ACK
    targets = (
        parse_database_url(env.get('FIRESTORE_PG_DSN', ''), label='FIRESTORE_PG_DSN'),
        parse_database_url(env.get('AUTH_MIGRATION_DATABASE_URL', ''), label='AUTH_MIGRATION_DATABASE_URL'),
        parse_host_authority(env.get('FIRESTORE_EMULATOR_HOST', ''), label='FIRESTORE_EMULATOR_HOST'),
    )
    for target in targets:
        require_disposable(target, acknowledged=acknowledged)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        targets = validate_external_targets()
    except UnsafeMigrationTarget as exc:
        parser.error(str(exc))
    print('migration test targets admitted: ' + ', '.join(f'{target.label}={target.host}' for target in targets))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
