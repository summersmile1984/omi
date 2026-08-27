import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

try:
    from workers import asgi
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare API Core", version="0.1.0")
MAX_ASSET_BODY_BYTES = 25_000_000


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
