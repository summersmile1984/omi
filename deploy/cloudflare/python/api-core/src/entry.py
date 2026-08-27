import re
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

try:
    from workers import asgi, fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]
    worker_fetch = None  # type: ignore[assignment]

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare API Core", version="0.1.0")
MAX_ASSET_BODY_BYTES = 25_000_000
FIRMWARE_TAG_PATTERN = re.compile(
    r"^(?:Omi_CV1|Omi_DK2|OmiGlass|OpenGlass|Friend)_v[0-9]+(?:\.[0-9]+){1,2}$", re.IGNORECASE
)
DEVICE_PREFIXES = {
    "Omi DevKit 2": "Omi_DK2",
    "Friend DevKit 1": "Friend",
    "Friend": "Friend",
    "OpenGlass": "OpenGlass",
    "Omi CV 1": "Omi_CV1",
    "OMI Glass": "OmiGlass",
    "OmiGlass": "OmiGlass",
    "nrf5340": "Omi_CV1",
}


def auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-core", "version": "cf-02"}


@app.get("/v1/cf/probe")
async def probe(request: Request):
    context = auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    await env.APP_DB.prepare(
        "INSERT INTO cf_worker_probe (uid, last_seen_at) VALUES (?, unixepoch()) "
        "ON CONFLICT(uid) DO UPDATE SET last_seen_at = excluded.last_seen_at"
    ).bind(uid).run()
    row = await env.APP_DB.prepare("SELECT uid, last_seen_at FROM cf_worker_probe WHERE uid = ?").bind(uid).first()
    return {"status": "ok", "service": "api-core", "auth": context, "probe": row}


def _firmware_metadata(markdown: str) -> dict[str, object]:
    match = re.search(r"<!-- KEY_VALUE_START\s*(.*?)\s*KEY_VALUE_END -->", markdown or "", re.DOTALL)
    if not match:
        return {}
    result: dict[str, object] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key == "ota_update_steps":
            result[key] = [part.strip() for part in value.split(",") if part.strip()]
        elif key == "changelog":
            result[key] = [part.strip() for part in value.split("|") if part.strip()]
        else:
            result[key] = value
    return result


def _firmware_response(prefix: str, release: dict[str, object]) -> dict[str, object]:
    metadata = _firmware_metadata(str(release.get("body") or ""))
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    suffix = ".bin" if prefix == "OmiGlass" else ".zip"
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].endswith(suffix)
            and (suffix == ".bin" or "ota" in item["name"].lower())
        ),
        None,
    )
    if not isinstance(asset, dict) or not asset.get("browser_download_url"):
        raise ValueError("firmware asset missing")
    return {
        "version": metadata.get("release_firmware_version"),
        "min_version": metadata.get("minimum_firmware_required"),
        "min_app_version": metadata.get("minimum_app_version"),
        "min_app_version_code": metadata.get("minimum_app_version_code"),
        "zip_url": asset["browser_download_url"],
        "draft": False,
        "ota_update_steps": metadata.get("ota_update_steps", []),
        "is_legacy_secure_dfu": str(metadata.get("is_legacy_secure_dfu", "True")).lower() == "true",
        "changelog": metadata.get("changelog", ""),
    }


def _parse_firmware_version(version: object) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not version.strip():
        return None
    normalized = version.strip().lower()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    parts = normalized.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    parsed = tuple(int(part) for part in parts)
    return parsed + (0,) * (3 - len(parsed))


def _firmware_candidates(releases: list[dict[str, object]], prefix: str) -> list[dict[str, object]]:
    return [
        release
        for release in releases
        if release.get("published_at")
        and not release.get("draft")
        and not release.get("prerelease")
        and isinstance(release.get("tag_name"), str)
        and FIRMWARE_TAG_PATTERN.fullmatch(str(release["tag_name"]))
        and str(release["tag_name"]).lower().startswith(prefix.lower() + "_v")
        and _parse_firmware_version(
            _firmware_metadata(str(release.get("body") or "")).get("release_firmware_version")
        )
    ]


async def _github_releases(env: object) -> list[dict[str, object]] | None:
    url = getattr(env, "FIRMWARE_RELEASES_URL", "https://api.github.com/repos/BasedHardware/omi/releases")
    headers = {
        "accept": "application/vnd.github+json",
        "user-agent": "omi-cloudflare-worker/0.1",
        "x-github-api-version": "2022-11-28",
    }
    token = getattr(env, "GITHUB_TOKEN", None)
    if token:
        headers["authorization"] = f"Bearer {token}"
    url = f"{url}{'&' if '?' in url else '?'}per_page=100"
    if worker_fetch is None:
        return None
    try:
        response = await worker_fetch(url, method="GET", headers=headers)
    except (OSError, TypeError, ValueError):
        return None
    if int(response.status) != 200:
        return None
    try:
        releases = await response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(releases, list):
        return None
    return [release for release in releases if isinstance(release, dict)]


def _firmware_upstream_error() -> JSONResponse:
    return JSONResponse({"error": "firmware upstream unavailable"}, status_code=502)


@app.get("/v2/firmware/stable")
async def firmware_stable(device_model: str, request: Request):
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    candidates = _firmware_candidates(releases, prefix)
    candidates.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not candidates:
        return JSONResponse({"error": "no stable firmware found"}, status_code=404)
    try:
        return _firmware_response(prefix, candidates[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


@app.get("/v2/firmware/latest")
async def firmware_latest(
    device_model: str,
    firmware_revision: str,
    hardware_revision: str,
    manufacturer_name: str,
    request: Request,
):
    del hardware_revision, manufacturer_name
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    current = _parse_firmware_version(firmware_revision)
    if current is None:
        return JSONResponse({"error": "could not determine current firmware version"}, status_code=400)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    candidates = []
    for release in _firmware_candidates(releases, prefix):
        metadata = _firmware_metadata(str(release.get("body") or ""))
        release_version = _parse_firmware_version(metadata.get("release_firmware_version"))
        if release_version is None or release_version <= current:
            continue
        minimum = _parse_firmware_version(metadata.get("minimum_firmware_required"))
        if minimum is not None and current < minimum:
            continue
        candidates.append(release)
    candidates.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not candidates:
        return JSONResponse({"error": "no suitable firmware update found"}, status_code=404)
    try:
        return _firmware_response(prefix, candidates[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


@app.get("/v2/firmware/version")
async def firmware_version(device_model: str, version: str, request: Request):
    prefix = DEVICE_PREFIXES.get(device_model)
    if not prefix:
        return JSONResponse({"error": "device not found"}, status_code=404)
    target = _parse_firmware_version(version)
    if target is None:
        return JSONResponse({"error": "could not parse requested firmware version"}, status_code=400)
    releases = await _github_releases(request.scope["env"])
    if releases is None:
        return _firmware_upstream_error()
    matches = [
        release
        for release in _firmware_candidates(releases, prefix)
        if _parse_firmware_version(
            _firmware_metadata(str(release.get("body") or "")).get("release_firmware_version")
        )
        == target
    ]
    matches.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    if not matches:
        return JSONResponse({"error": "requested firmware version not found"}, status_code=404)
    try:
        return _firmware_response(prefix, matches[0])
    except ValueError:
        return JSONResponse({"error": "firmware asset missing"}, status_code=502)


def _asset_key(uid: str, requested_key: str) -> str | None:
    key = requested_key.strip("/")
    if not key or len(key) > 512 or "\x00" in key:
        return None
    if any(part in {".", ".."} for part in key.split("/")):
        return None
    return f"{uid}/{key}"


def _asset_context(request: Request) -> tuple[dict[str, object] | None, object | None]:
    context = auth_context(request)
    if not context:
        return None, None
    env = request.scope["env"]
    if not getattr(env, "ASSETS", None):
        return context, None
    return context, env


@app.put("/v1/cf/assets/{requested_key:path}")
async def put_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_ASSET_BODY_BYTES:
        return JSONResponse({"error": "asset body too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_ASSET_BODY_BYTES:
        return JSONResponse({"error": "asset body too large"}, status_code=413)
    content_type = request.headers.get("content-type", "application/octet-stream")[:200]
    stored = await env.ASSETS.put(key, body, httpMetadata={"contentType": content_type})
    etag = str(getattr(stored, "httpEtag", getattr(stored, "etag", "")))
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_asset_objects (uid, object_key, content_type, size, etag, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, object_key) DO UPDATE SET content_type = excluded.content_type, "
        "size = excluded.size, etag = excluded.etag, updated_at = excluded.updated_at"
    ).bind(str(context["uid"]), key, content_type, len(body), etag, now, now).run()
    return {"status": "ok", "key": requested_key.strip("/"), "size": len(body), "etag": etag}


@app.get("/v1/cf/assets/{requested_key:path}")
async def get_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    row = await env.APP_DB.prepare(
        "SELECT content_type, etag FROM cf_asset_objects WHERE uid = ? AND object_key = ?"
    ).bind(str(context["uid"]), key).first()
    if not row:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    stored = await env.ASSETS.get(key)
    if not stored:
        return JSONResponse({"error": "asset not found"}, status_code=404)
    content = bytes(await stored.arrayBuffer())
    return Response(
        content=content,
        media_type=str(row["content_type"]),
        headers={"etag": str(row["etag"]), "content-length": str(len(content))},
    )


@app.delete("/v1/cf/assets/{requested_key:path}")
async def delete_asset(requested_key: str, request: Request):
    context, env = _asset_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if env is None:
        return JSONResponse({"error": "asset storage is not configured"}, status_code=503)
    key = _asset_key(str(context["uid"]), requested_key)
    if not key:
        return JSONResponse({"error": "invalid asset key"}, status_code=400)
    await env.ASSETS.delete(key)
    await env.APP_DB.prepare("DELETE FROM cf_asset_objects WHERE uid = ? AND object_key = ?").bind(
        str(context["uid"]), key
    ).run()
    return {"status": "deleted", "key": requested_key.strip("/")}


Default = asgi.entrypoint(app) if asgi else None
