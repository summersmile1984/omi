from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from workers import asgi

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare API Core", version="0.1.0")


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


Default = asgi.entrypoint(app)
