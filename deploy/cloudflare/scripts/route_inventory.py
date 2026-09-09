#!/usr/bin/env python3
"""Own the fork's complete HTTP/WebSocket migration inventory.

The upstream OpenAPI exporter owns the hermetic import boundary. We reuse it
without adding a fork-only ``--surface`` to the upstream command. Review state
belongs to this inventory: a newly registered route stays unclassified until
its real implementation or tracked migration boundary has been reviewed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from fastapi.routing import APIRoute, APIWebSocketRoute

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INVENTORY = REPO_ROOT / 'deploy/cloudflare/manifests/backend-routes.json'
CLASSIFIED_STATES = {'staging-owned', 'legacy-owned', 'blocked'}


class InventoryError(ValueError):
    pass


def route_identity(route: dict[str, Any]) -> tuple[str, str, str]:
    try:
        identity = (route['method'], route['path'], route['protocol'])
    except (KeyError, TypeError) as exc:
        raise InventoryError('route requires method, path and protocol') from exc
    if not all(isinstance(value, str) and value for value in identity):
        raise InventoryError('route requires nonempty method, path and protocol')
    return identity


def registered_routes(routes: Iterable[Any]) -> list[dict[str, str]]:
    """Read actual FastAPI registrations, including routes omitted from OpenAPI."""
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for route in routes:
        if isinstance(route, APIWebSocketRoute):
            methods, protocol = ['WEBSOCKET'], 'websocket'
        elif isinstance(route, APIRoute):
            methods, protocol = sorted(route.methods), 'http'
        else:
            continue  # Starlette's /docs and /openapi.json are not product APIs.
        for method in methods:
            item = {'method': method, 'path': route.path, 'protocol': protocol}
            key = route_identity(item)
            # FastAPI may register a router twice. Those declarations share one
            # externally reachable method/path/protocol slot (first match wins).
            # This inventory owns route coverage, not upstream shadowing policy.
            found.setdefault(key, item)
    return [found[key] for key in sorted(found)]


def discover_backend_routes() -> list[dict[str, str]]:
    scripts_path = str(REPO_ROOT / 'backend/scripts')
    sys.path.insert(0, scripts_path)
    try:
        exporter = importlib.import_module('export_openapi')
        exporter.generate_public_openapi()
        # The exporter has already imported this app with blocked network,
        # synthetic credentials and dependency patches, and checked side effects.
        return registered_routes(sys.modules['main'].app.routes)
    finally:
        sys.path.remove(scripts_path)


def load_inventory(path: Path) -> dict[str, Any]:
    try:
        inventory = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f'cannot read inventory {path}: {exc}') from exc
    if not isinstance(inventory, dict) or inventory.get('version') != 1:
        raise InventoryError('inventory must declare version 1')
    if inventory.get('source') != 'backend/main.py' or not isinstance(inventory.get('routes'), list):
        raise InventoryError('inventory must declare backend/main.py source and routes')
    return inventory


def reconcile_inventory(actual: list[dict[str, str]], previous: dict[str, Any]) -> dict[str, Any]:
    reviewed = {}
    for item in previous['routes']:
        key = route_identity(item)
        if key in reviewed:
            raise InventoryError(f'duplicate inventory entry: {key}')
        reviewed[key] = item
    rows = []
    for route in actual:
        rows.append(
            {
                **reviewed.get(
                    route_identity(route),
                    {'migration_state': 'unclassified', 'owner': 'unclassified', 'target_runtime': 'unclassified'},
                ),
                **route,
            }
        )
    return {'routes': sorted(rows, key=route_identity), 'source': 'backend/main.py', 'version': 1}


def stable_json(inventory: dict[str, Any]) -> str:
    rows = ',\n'.join('    ' + json.dumps(row, sort_keys=True) for row in inventory['routes'])
    return '{\n  "routes": [\n' + rows + '\n  ],\n  "source": "backend/main.py",\n  "version": 1\n}\n'


def check_inventory(actual: list[dict[str, str]], previous: dict[str, Any]) -> dict[str, Any]:
    current = reconcile_inventory(actual, previous)
    actual_keys = {route_identity(item) for item in actual}
    previous_keys = {route_identity(item) for item in previous['routes']}
    added, removed = sorted(actual_keys - previous_keys), sorted(previous_keys - actual_keys)
    if added or removed:
        details = [f'added {method} {path} ({protocol})' for method, path, protocol in added]
        details += [f'removed {method} {path} ({protocol})' for method, path, protocol in removed]
        raise InventoryError('backend route inventory is stale:\n' + '\n'.join(details))
    for item in current['routes']:
        if item.get('migration_state') not in CLASSIFIED_STATES:
            raise InventoryError(f'unclassified backend route: {item["method"]} {item["path"]}')
        for field in ('owner', 'target_runtime'):
            if not isinstance(item.get(field), str) or not item[field] or item[field] == 'unclassified':
                raise InventoryError(f'backend route requires reviewed {field}: {item["method"]} {item["path"]}')
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', type=Path, nargs='?', const=DEFAULT_INVENTORY)
    action.add_argument('--write', type=Path, nargs='?', const=DEFAULT_INVENTORY)
    args = parser.parse_args()
    path = (args.check or args.write).resolve()
    try:
        actual = discover_backend_routes()
        previous = load_inventory(path) if path.exists() else {'version': 1, 'source': 'backend/main.py', 'routes': []}
        if args.check:
            check_inventory(actual, previous)
            print(f'Backend route inventory matches {len(actual)} registered HTTP/WebSocket routes.')
        else:
            current = reconcile_inventory(actual, previous)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stable_json(current), encoding='utf-8')
            pending = sum(row['migration_state'] == 'unclassified' for row in current['routes'])
            print(f'Wrote {len(actual)} routes to {path}; {pending} require explicit classification.')
    except InventoryError as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
