#!/usr/bin/env python3
"""Fork-owned local dev harness entry that replaces host-binary services with Testcontainers.

LIFECYCLE: permanent

Replaces the upstream ``dev_harness.cli``'s host-binary ``redis-server``,
Firebase emulators, typesense Docker run, etc. with Testcontainers-managed
containers, and wires the fork's Better Auth TS service image + MinIO into
the local-dev stack. Behaves as a drop-in ``python -m dev_harness`` shim
that the fork's ``make -f Makefile.fork local-tc-up`` invokes; the upstream
``make dev-up`` is unchanged and still does the host-binary path.

Why this lives here:
- Upstream ``scripts/dev-harness/dev_harness/`` is upstream-owned; we
  intentionally do NOT modify any of its files. We add one new fork-owned
  module inside the package directory (``fork_local.py``); adding files
  inside an upstream-owned directory does not produce upstream-touch
  violations as long as upstream does not add a same-named file.
- The fork's prior attempts (commits 8f8d425233 / 851bbdf530 / 3b98b831e7)
  inlined ~800 lines of Testcontainers orchestration into upstream
  ``scripts/dev-harness/dev_harness/cli.py`` and ``config.py``, which
  produced 3 not-allowlisted upstream-file edits (N6/N7/N8 in the 13-violation
  baseline that PR-A closed). This module keeps that orchestration outside
  the upstream files by monkey-patching them at runtime.

What this module does:
1. Loads the upstream ``dev_harness.cli`` and ``dev_harness.config`` modules
   via importlib.
2. Replaces ``dev_harness.cli._start_infrastructure`` with a Testcontainers
   variant that brings up:
   - Postgres (firestore_pg backend via fork-owned ``backend/firestore_pg/``)
   - MinIO (S3-compatible storage for the fork's STORAGE_BACKEND=minio path)
   - Firebase Auth emulator (omi-emulators:local, auth-only — firestore is
     served in-process by the fork's ``fake_firestore`` shim)
   - Better Auth TS service (omi-auth-server:self-host-live-current image
     runs the production ``src/migrate.js`` then exposes /api/auth/jwks)
   - Redis (testcontainers redis:7-alpine replacing the host redis-server)
   - Typesense still uses the upstream path (Docker container with pinned
     image; no host binary)
3. Replaces ``dev_harness.config._harness_service_extra`` with a fork
   variant that injects ``FIRESTORE_PG_DSN``, ``AUTH_JWKS_URL``, ``AUTH_PROVIDER``,
   ``STORAGE_BACKEND=minio``, MinIO endpoint + access keys, ``VECTOR_STORE_PROVIDER=qdrant``,
   and Better Auth env vars.
4. Delegates everything else (CLI parser, state manifest, supervised process
   manager, typesense runtime, app service startup, health checks, status,
   down, reset) to upstream. Calls ``dev_harness.cli.main(argv)`` after
   monkey-patching.

Usage:
    PYTHONPATH=scripts/dev-harness python3 -m dev_harness.fork_local up
    PYTHONPATH=scripts/dev-harness python3 -m dev_harness.fork_local status
    PYTHONPATH=scripts/dev-harness python3 -m dev_harness.fork_local down

Or via the wrapper:
    bash scripts/dev-harness/fork_local.sh up
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants (mirror the prior fork's hard-coded choices; see commit 8f8d425233
# and 3b98b831e7 for the originals that lived in upstream cli.py before
# PR-A closed them).
# ---------------------------------------------------------------------------

REDIS_IMAGE = "redis:7-alpine"
POSTGRES_IMAGE = "postgres:16-alpine"
POSTGRES_USER = "omi"
POSTGRES_PASSWORD = "omi_dev_only_local_dev_password"
POSTGRES_DB = "omi"

BETTER_AUTH_IMAGE = "omi-auth-server:self-host-live-current"
BETTER_AUTH_PORT = 3000

MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z"
MINIO_PORT = 9000
MINIO_CONSOLE_PORT = 9001
MINIO_ACCESS_KEY = "synthetic-access"
MINIO_SECRET_KEY = "synthetic-secret"
MINIO_BUCKETS = [
    "speech-profiles",
    "postprocessing",
    "omi-private-cloud-sync",
    "sync-temporal",
    "memories-recordings",
    "app-thumbnails",
    "chat-files",
    "desktop-updates",
]

FIREBASE_IMAGE = "omi-emulators:local"
FIREBASE_INTERNAL_AUTH_PORT = 9099

# Backwards-compatible alias used by _start_*_container(record.name).
OWNERSHIP_PREFIX = "omi-dev-harness"


# ---------------------------------------------------------------------------
# Upstream module loading
# ---------------------------------------------------------------------------

_PKG_DIR = Path(__file__).resolve().parent


def _load(name: str):
    """Import a sibling module from upstream's ``dev_harness`` package."""
    spec = importlib.util.spec_from_file_location(name, _PKG_DIR / f"{name.split('.')[-1]}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Loaded lazily so importing this module without the upstream sources does
# not crash. main() invokes these at runtime.
_upstream_cli = None
_upstream_config = None
_upstream_self_hosted_profile = None

# Captured in _bootstrap_upstream() before main() patches the upstream
# ``_harness_service_extra``. ``_fork_harness_service_extra`` calls this
# captured reference (not the patched one) so it can extend the upstream
# env dict without recursing into itself.
_original_harness_service_extra = None


def _bootstrap_upstream() -> None:
    """Load upstream dev_harness.cli / config / self_hosted_profile."""
    global _upstream_cli, _upstream_config, _upstream_self_hosted_profile, _original_harness_service_extra
    if _upstream_cli is not None:
        return
    # ``dev_harness`` is the package; ensure scripts/dev-harness is on sys.path.
    pkg_root = str(_PKG_DIR.parent)
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    import dev_harness.cli as cli_mod
    import dev_harness.config as config_mod
    import dev_harness.self_hosted_profile as profile_mod
    _upstream_cli = cli_mod
    _upstream_config = config_mod
    _upstream_self_hosted_profile = profile_mod
    # Capture the ORIGINAL upstream ``_harness_service_extra`` BEFORE main()
    # monkey-patches it. The fork wrapper needs to call the unpatched version
    # to avoid infinite recursion (patch-into-patched).
    _original_harness_service_extra = _upstream_config._harness_service_extra


# ---------------------------------------------------------------------------
# Testcontainers orchestration
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_postgres_container(cfg, harness_network) -> None:
    from testcontainers.postgres import PostgresContainer

    _require_port_available_or_owned(cfg, "postgres", int(os.environ.get("OMI_HARNESS_PG_PORT", "5444")))
    pg_port = int(os.environ.get("OMI_HARNESS_PG_PORT", "5444"))
    # Container name must include the port so multiple instances on different
    # ports don't collide on the hardcoded Docker container name. The fork's
    # OWNERSHIP_PREFIX collision bug predates testcontainers; the upstream
    # _marker() omits port from the marker string.
    container_name = _marker(cfg, "postgres").replace(":", "-") + f"-pg-{pg_port}"
    container = (
        PostgresContainer(POSTGRES_IMAGE, username=POSTGRES_USER, password=POSTGRES_PASSWORD, dbname=POSTGRES_DB)
        .with_name(container_name)
        .with_bind_ports(5432, pg_port)
        .with_network(harness_network)
        .with_network_aliases("pg")
    )
    container.start()
    host = container.get_container_host_ip()
    pg_port = int(os.environ.get("OMI_HARNESS_PG_PORT", "5444"))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _port_open(host, pg_port):
            break
        time.sleep(0.25)
    else:
        container.stop()
        raise RuntimeError(f"postgres container did not bind 127.0.0.1:{pg_port} within 30s")
    log_path = cfg.layout.logs_dir / "postgres.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"[fork-local] started testcontainers postgres ({POSTGRES_IMAGE}) -> "
            f"127.0.0.1:{pg_port} user={POSTGRES_USER} db={POSTGRES_DB}\n"
        )
    records = [r for r in _upstream_cli._process_records(cfg) if r.get("service") != "postgres"]
    records.append(
        {
            "service": "postgres",
            "kind": "container",
            "pid": -1,
            "port": pg_port,
            "endpoint": f"{host}:{pg_port}",
            "container_name": container_name,
            "container_id": container.get_wrapped_container().id,
            "image": POSTGRES_IMAGE,
            "log": str(log_path),
            "ownership_marker": _marker(cfg, "postgres"),
            "started_at": _upstream_cli._now(),
        }
    )
    _upstream_cli._save_manifests(cfg, records)
    (cfg.layout.services_dir / "pg_port.txt").write_text(f"{pg_port}\n", encoding="utf-8")
    print(f"postgres: started (testcontainers {POSTGRES_IMAGE}) -> 127.0.0.1:{pg_port}")


def _start_minio_container(cfg) -> int:
    from testcontainers.core.container import DockerContainer

    _require_port_available_or_owned(cfg, "minio", int(os.environ.get("OMI_HARNESS_MINIO_PORT", str(MINIO_PORT))))
    data_dir = cfg.layout.services_dir / "minio-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    minio_port = int(os.environ.get("OMI_HARNESS_MINIO_PORT", str(MINIO_PORT)))
    minio_console_port = int(os.environ.get("OMI_HARNESS_MINIO_CONSOLE_PORT", str(MINIO_CONSOLE_PORT)))
    # Same port-in-name pattern as Postgres to avoid collisions across
    # multiple test runs on different ports in the same docker daemon.
    container_name = _marker(cfg, "minio").replace(":", "-") + f"-minio-{minio_port}"
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_name(container_name)
        .with_bind_ports(MINIO_PORT, minio_port)
        .with_bind_ports(MINIO_CONSOLE_PORT, minio_console_port)
        .with_envs(MINIO_ROOT_USER=MINIO_ACCESS_KEY, MINIO_ROOT_PASSWORD=MINIO_SECRET_KEY)
        .with_volume_mapping(str(data_dir), "/data")
    )
    container.start()
    host = container.get_container_host_ip()
    _port_open(host, minio_port)
    # Pre-create buckets
    from testcontainers.core.docker_client import DockerClient
    api = DockerClient().client
    bucket_create_cmd = " && ".join(
        [f"/usr/bin/mc alias local http://127.0.0.1:{MINIO_PORT} {MINIO_ACCESS_KEY} {MINIO_SECRET_KEY}"]
        + [f"/usr/bin/mc mb --ignore-existing local/{bucket}" for bucket in MINIO_BUCKETS]
    )
    api.containers.run(
        MINIO_IMAGE,
        command=["sh", "-c", bucket_create_cmd],
        name=_marker(cfg, "minio-bootstrap").replace(":", "-") + "-buckets",
        environment={"MC_CONFIG_DIR": "/tmp/.mc"},
        network=f"container:{container_name}",
        remove=True,
        detach=True,
    ).wait()
    log_path = cfg.layout.logs_dir / "minio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[fork-local] started {MINIO_IMAGE} -> http://{host}:{minio_port} buckets={MINIO_BUCKETS}\n")
    records = [r for r in _upstream_cli._process_records(cfg) if r.get("service") != "minio"]
    records.append(
        {
            "service": "minio",
            "kind": "container",
            "pid": -1,
            "port": minio_port,
            "endpoint": f"{host}:{minio_port}",
            "container_name": container_name,
            "container_id": container.get_wrapped_container().id,
            "image": MINIO_IMAGE,
            "log": str(log_path),
            "ownership_marker": _marker(cfg, "minio"),
            "started_at": _upstream_cli._now(),
        }
    )
    _upstream_cli._save_manifests(cfg, records)
    (cfg.layout.services_dir / "minio_port.txt").write_text(f"{minio_port}\n", encoding="utf-8")
    print(f"minio: started ({MINIO_IMAGE}) -> http://{host}:{minio_port} (console :{minio_console_port})")
    return minio_port


def _start_auth_container(cfg) -> None:
    """Firebase Auth emulator (auth-only; firestore is in-process fake)."""
    from testcontainers.core.container import DockerContainer

    _require_port_available_or_owned(cfg, "auth", cfg.auth_port)
    container_name = _marker(cfg, "auth").replace(":", "-") + "-emulators"
    container = (
        DockerContainer(FIREBASE_IMAGE)
        .with_name(container_name)
        .with_bind_ports(FIREBASE_INTERNAL_AUTH_PORT, cfg.auth_port)
    )
    container.start()
    host = container.get_container_host_ip()
    auth_host_port = container.get_exposed_port(FIREBASE_INTERNAL_AUTH_PORT)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if _port_open(host, auth_host_port):
            break
        time.sleep(0.5)
    else:
        container.stop()
        raise RuntimeError(f"auth container did not bind on {host}:{auth_host_port} within 60s")
    log_path = cfg.layout.logs_dir / "firebase-emulators.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"[fork-local] started testcontainers {FIREBASE_IMAGE} (auth-only) -> "
            f"{host}:{auth_host_port}; firestore served by in-process fake_firestore\n"
        )
    records = [r for r in _upstream_cli._process_records(cfg) if r.get("service") != "auth"]
    records.append(
        {
            "service": "auth",
            "kind": "container",
            "pid": -1,
            "port": cfg.auth_port,
            "endpoint": f"{host}:{auth_host_port}",
            "container_name": container_name,
            "container_id": container.get_wrapped_container().id,
            "image": FIREBASE_IMAGE,
            "log": str(log_path),
            "ownership_marker": _marker(cfg, "auth"),
            "started_at": _upstream_cli._now(),
        }
    )
    records = [r for r in records if r.get("service") != "firestore"]
    _upstream_cli._save_manifests(cfg, records)
    sentinel = cfg.layout.logs_dir / "firestore-shim-ready"
    sentinel.write_text(f"fake_firestore active at {_upstream_cli._now()}\n", encoding="utf-8")
    print(f"auth: started (testcontainers {FIREBASE_IMAGE}) -> {cfg.auth_host}:{cfg.auth_port}")


def _better_auth_envs() -> dict:
    return {
        "DATABASE_URL": os.environ.get(
            "BETTER_AUTH_DATABASE_URL",
            f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@pg:5432/{POSTGRES_DB}",
        ),
        "BETTER_AUTH_SECRET": os.environ.get(
            "BETTER_AUTH_SECRET", "local-dev-better-auth-secret-not-real-32-bytes-min"
        ),
        "BETTER_AUTH_URL": os.environ.get(
            "BETTER_AUTH_URL", f"http://127.0.0.1:{BETTER_AUTH_PORT}"
        ),
        "AUTH_INTERNAL_ADMIN_SECRET": os.environ.get(
            "AUTH_INTERNAL_ADMIN_SECRET", "local-dev-internal-admin-secret-not-real"
        ),
        "BETTER_AUTH_TRUSTED_ORIGINS": os.environ.get(
            "BETTER_AUTH_TRUSTED_ORIGINS",
            "http://127.0.0.1:3000,http://127.0.0.1:8000,http://localhost,http://127.0.0.1",
        ),
        "BETTER_AUTH_IP_HEADERS": os.environ.get("BETTER_AUTH_IP_HEADERS", "x-forwarded-for,x-real-ip"),
        "NODE_ENV": "development",
    }


def _run_better_auth_migration(cfg, harness_network, envs) -> None:
    from testcontainers.core.docker_client import DockerClient

    migrate_name = _marker(cfg, "better-auth-migrate").replace(":", "-") + "-migrate"
    log_path = cfg.layout.logs_dir / "better-auth-migrate.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    client = DockerClient().client
    container = client.containers.run(
        BETTER_AUTH_IMAGE,
        command=["node", "src/migrate.js"],
        name=migrate_name,
        environment=envs,
        network=harness_network.name,
        remove=True,
        stdout=True,
        stderr=True,
        detach=True,
    )
    log_file = log_path.open("ab")
    for line in container.logs(stream=True, follow=True):
        log_file.write(line)
        log_file.flush()
    container.wait()
    log_file.close()
    rc = container.attrs.get("State", {}).get("ExitCode", 1)
    if rc != 0:
        raise RuntimeError(f"better-auth migration container exited with code {rc}; see {log_path}")
    print(f"better-auth: schema migration OK (log={log_path})")


def _start_better_auth_container(cfg, harness_network) -> None:
    """Bring up the Better Auth TS service container exposing JWKS."""
    from testcontainers.core.docker_client import DockerClient

    _require_port_available_or_owned(cfg, "better-auth", BETTER_AUTH_PORT)
    ba_envs = _better_auth_envs()
    print("better-auth: running schema migration...")
    _run_better_auth_migration(cfg, harness_network, ba_envs)
    container_name = _marker(cfg, "better-auth").replace(":", "-") + "-auth"
    ba_log_path = cfg.layout.logs_dir / "better-auth.log"
    ba_log_path.parent.mkdir(parents=True, exist_ok=True)
    ba_log_file = ba_log_path.open("ab")
    client = DockerClient().client
    container = client.containers.run(
        BETTER_AUTH_IMAGE,
        name=container_name,
        environment=ba_envs,
        network=harness_network.name,
        ports={f"{BETTER_AUTH_PORT}/tcp": ("127.0.0.1", BETTER_AUTH_PORT)},
        stdout=True,
        stderr=True,
        detach=True,
    )

    import threading

    def _stream_logs():
        try:
            for line in container.logs(stream=True, follow=True):
                ba_log_file.write(line)
                ba_log_file.flush()
        except Exception:
            pass

    threading.Thread(target=_stream_logs, daemon=True).start()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        ok, _ = _upstream_cli._http_ok(f"http://127.0.0.1:{BETTER_AUTH_PORT}/api/auth/jwks")
        if ok:
            break
        time.sleep(0.5)
    else:
        try:
            container.remove(force=True)
        finally:
            ba_log_file.close()
        raise RuntimeError(f"better-auth container did not expose JWKS on 127.0.0.1:{BETTER_AUTH_PORT} within 30s")
    records = [r for r in _upstream_cli._process_records(cfg) if r.get("service") != "better-auth"]
    records.append(
        {
            "service": "better-auth",
            "kind": "container",
            "pid": -1,
            "port": BETTER_AUTH_PORT,
            "endpoint": f"127.0.0.1:{BETTER_AUTH_PORT}",
            "container_name": container_name,
            "container_id": container.id,
            "image": BETTER_AUTH_IMAGE,
            "log": str(ba_log_path),
            "ownership_marker": _marker(cfg, "better-auth"),
            "started_at": _upstream_cli._now(),
            "_container": container,
        }
    )
    _upstream_cli._save_manifests(cfg, records)
    print(f"better-auth: started ({BETTER_AUTH_IMAGE}) -> http://127.0.0.1:{BETTER_AUTH_PORT}/api/auth/jwks")


def _start_redis_container(cfg, data_dir) -> None:
    """Throwaway Redis 7 Alpine container (replaces host redis-server)."""
    from testcontainers.redis import RedisContainer

    _require_port_available_or_owned(cfg, "redis", cfg.redis_port)
    container_name = _marker(cfg, "redis").replace(":", "-") + "-redis"
    container = RedisContainer(REDIS_IMAGE).with_name(container_name).with_bind_ports(6379, cfg.redis_port)
    container.start()
    host = container.get_container_host_ip()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _port_open(host, cfg.redis_port):
            break
        time.sleep(0.1)
    else:
        container.stop()
        raise RuntimeError(f"redis container did not bind 127.0.0.1:{cfg.redis_port} within 15s")
    log_path = cfg.layout.logs_dir / "redis.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"[fork-local] started testcontainers redis ({REDIS_IMAGE}) -> 127.0.0.1:{cfg.redis_port}\n"
        )
    records = [r for r in _upstream_cli._process_records(cfg) if r.get("service") != "redis"]
    records.append(
        {
            "service": "redis",
            "kind": "container",
            "pid": -1,
            "port": cfg.redis_port,
            "endpoint": f"127.0.0.1:{cfg.redis_port}",
            "container_name": container_name,
            "container_id": container.get_wrapped_container().id,
            "image": REDIS_IMAGE,
            "log": str(log_path),
            "ownership_marker": _marker(cfg, "redis"),
            "started_at": _upstream_cli._now(),
        }
    )
    _upstream_cli._save_manifests(cfg, records)
    print(f"redis: started (testcontainers {REDIS_IMAGE}) -> 127.0.0.1:{cfg.redis_port}")


# ---------------------------------------------------------------------------
# Monkey-patches applied to upstream's modules
# ---------------------------------------------------------------------------


def _require_port_available_or_owned(cfg, service: str, port: int) -> None:
    """Proxy to upstream's helper."""
    _upstream_cli._require_port_available_or_owned(cfg, service, port)


def _marker(cfg, service: str) -> str:
    return _upstream_cli._marker(cfg, service)


def _preclean_stale_containers() -> None:
    """Remove stale ``omi-dev-harness-*`` containers left by prior runs.

    Each ``local-tc-up`` invocation creates six short-lived containers
    (postgres / minio / auth / better-auth / redis / better-auth-migrate).
    A failed previous run leaves them running, occupying host ports and
    blocking the next run with 409 Conflict on container start. Probe the
    docker daemon for any omi-dev-harness container and ``docker rm -f`` it
    before we try to start fresh containers.
    """
    try:
        import subprocess as _sp
        result = _sp.run(
            ["docker", "ps", "-a", "--filter", f"name={OWNERSHIP_PREFIX}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
        if names:
            print(f"fork-local: removing {len(names)} stale container(s) from prior run(s)")
            _sp.run(
                ["docker", "rm", "-f", *names],
                capture_output=True, timeout=30, check=False,
            )
    except (OSError, _sp.TimeoutExpiredException):
        # Docker daemon missing or slow; the prerequisite_report already
        # surfaced that. Silently skip — the next testcontainers call will
        # fail loudly on the actual create.
        pass


def _fork_start_infrastructure(cfg) -> None:
    """Replacement for upstream ``_start_infrastructure``.

    Brings up Postgres, MinIO, Firebase Auth, Better Auth, Redis, Typesense
    via Testcontainers + Docker SDK. Writes pg_port.txt and minio_port.txt
    that ``_harness_service_extra`` reads to wire env into the backend child.
    """
    _preclean_stale_containers()
    from testcontainers.core.network import Network

    cfg.layout.logs_dir.mkdir(parents=True, exist_ok=True)
    profile_path = _upstream_self_hosted_profile.write_self_hosted_local_profile(cfg.layout.services_dir)
    harness_network = Network().create()
    _start_postgres_container(cfg, harness_network)
    _start_minio_container(cfg)
    _start_auth_container(cfg)
    _start_better_auth_container(cfg, harness_network)
    redis_dir = cfg.layout.services_dir / "redis"
    redis_dir.mkdir(parents=True, exist_ok=True)
    _start_redis_container(cfg, redis_dir)
    print(f"typesense runtime: {_upstream_cli.typesense_runtime()}")
    _upstream_cli._remove_stale_typesense_container(cfg)
    _upstream_cli._start_process(
        cfg,
        "typesense",
        _upstream_cli._typesense_command(cfg),
        cwd=cfg.repo_root,
        log_name="typesense.log",
        port=cfg.typesense_port,
    )
    print(f"self_hosted.local profile written at {profile_path}")


def _fork_harness_service_extra(cfg) -> dict:
    """Replacement for upstream ``_harness_service_extra``.

    Adds fork-only env vars (FIRESTORE_PG_DSN, AUTH_JWKS_URL, STORAGE_BACKEND=minio,
    MinIO credentials, VECTOR_STORE_PROVIDER=qdrant, Better Auth env) on top of
    the upstream-supplied base dict.
    """
    # ``_upstream_config._harness_service_extra`` is replaced by ``main()`` so we
    # capture the ORIGINAL upstream function before patching to avoid recursion.
    base = _original_harness_service_extra(cfg)
    pg_port_file = cfg.layout.services_dir / "pg_port.txt"
    pg_port = int(pg_port_file.read_text(encoding="utf-8").strip()) if pg_port_file.is_file() else 5443
    minio_port_file = cfg.layout.services_dir / "minio_port.txt"
    minio_port = int(minio_port_file.read_text(encoding="utf-8").strip()) if minio_port_file.is_file() else 9000
    fork_extra = {
        "OMI_DEPLOYMENT_TARGET": "self_hosted",
        "OMI_DEPLOYMENT_PROFILE": "self_hosted.local",
        "OMI_DEPLOYMENT_PROFILES_PATH": str(cfg.layout.services_dir / "deployment_profiles.generated.json"),
        "OMI_BRAND": "omi-upstream",
        "FIRESTORE_PG_DSN": _upstream_self_hosted_profile.self_hosted_local_dsn(pg_port),
        "AUTH_PROVIDER": "better_auth",
        "AUTH_JWKS_URL": f"http://127.0.0.1:{BETTER_AUTH_PORT}/api/auth/jwks",
        "STORAGE_BACKEND": "minio",
        "QUEUE_BACKEND": "redis",
        "VECTOR_STORE_PROVIDER": "qdrant",
        # Pin STT to the in-tree parakeet stub so the backend's
        # ``validate_streaming_stt_env`` (in ``utils/stt/streaming.py``)
        # does not demand real SONIOX / DEEPGRAM credentials. This is
        # the same override the prior fork used under ``provider_mode=offline``;
        # we set it unconditionally under fork-local because the harness
        # never reaches a real STT provider in this entry.
        "STT_SERVICE_MODELS": "parakeet",
        "MINIO_ENDPOINT": f"http://127.0.0.1:{minio_port}",
        "MINIO_PUBLIC_ENDPOINT": f"http://127.0.0.1:{minio_port}",
        "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
        "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
        "MINIO_REGION": "us-east-1",
    }
    base.update(fork_extra)
    return base


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _fork_prerequisite_report(cfg) -> tuple[list[str], list[str]]:
    """Replacement for upstream ``prerequisite_report`` under fork-local.

    Fork-local serves redis / firebase-auth / typesense / pg / minio via
    Testcontainers, so the upstream host-binary checks (redis-server,
    firebase-tools, java, docker) are replaced with the actual container
    prerequisites: docker daemon reachable + testcontainers-python installed.
    Other upstream checks (firebase.json + firestore.rules presence,
    python-dotenv, Python imports) remain unchanged.
    """
    missing: list[str] = []
    warnings: list[str] = []
    # Docker daemon must be reachable; testcontainers drives it.
    if not _upstream_cli._which("docker"):
        missing.append("docker (testcontainers requires the docker CLI on PATH)")
    else:
        try:
            import subprocess as _sp
            r = _sp.run(["docker", "info"], capture_output=True, timeout=5, check=False)
            if r.returncode:
                missing.append("docker daemon (testcontainers requires a running daemon)")
        except (OSError, _sp.TimeoutExpiredException):
            missing.append("docker daemon (testcontainers could not reach the daemon)")
    # python-dotenv is required by upstream config.py at import time.
    try:
        import dotenv  # noqa: F401
    except ImportError:
        missing.append("python-dotenv (uv pip install --python backend/.venv/bin/python python-dotenv)")
    # Firebase emulator image must be built locally or pulled. The fork's
    # deploy/self-host/Dockerfile.llm is the canonical build target; we just
    # probe whether the image is present.
    try:
        import subprocess as _sp2
        r = _sp2.run(
            ["docker", "image", "inspect", FIREBASE_IMAGE],
            capture_output=True, timeout=5, check=False,
        )
        if r.returncode:
            warnings.append(
                f"docker image {FIREBASE_IMAGE} not present locally; "
                f"build deploy/self-host/Dockerfile.llm or pull before first run"
            )
    except (OSError, _sp2.TimeoutExpiredException):
        pass
    # Better Auth image must also be present.
    try:
        import subprocess as _sp3
        r = _sp3.run(
            ["docker", "image", "inspect", BETTER_AUTH_IMAGE],
            capture_output=True, timeout=5, check=False,
        )
        if r.returncode:
            warnings.append(
                f"docker image {BETTER_AUTH_IMAGE} not present locally; "
                f"build from auth-server/ or deploy/self-host/Dockerfile.llm"
            )
    except (OSError, _sp3.TimeoutExpiredException):
        pass
    # Upstream file presence checks (unchanged).
    if not (cfg.repo_root / "firebase.json").is_file():
        missing.append("firebase.json at repo root")
    if not (cfg.repo_root / "firestore.rules").is_file():
        missing.append("firestore.rules at repo root")
    if not (cfg.repo_root / "firestore.indexes.json").is_file():
        missing.append("firestore.indexes.json at repo root")
    if not (cfg.repo_root / "backend" / "main.py").is_file():
        missing.append("backend/main.py")
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("Python package uvicorn (install backend requirements)")
    return missing, warnings


def main(argv: list[str] | None = None) -> int:
    _bootstrap_upstream()
    # Monkey-patch the upstream functions. ``_start_infrastructure`` is
    # replaced with our Testcontainers variant; ``_harness_service_extra`` is
    # wrapped to extend the upstream-supplied dict with fork-only env vars;
    # ``prerequisite_report`` is replaced with one that looks for the
    # container prerequisites (docker daemon + python-dotenv + images) instead
    # of the host-binary prereqs that no longer apply under this entry.
    _upstream_cli._start_infrastructure = _fork_start_infrastructure
    _upstream_config._harness_service_extra = _fork_harness_service_extra
    _upstream_cli.prerequisite_report = _fork_prerequisite_report
    return _upstream_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())