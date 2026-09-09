# Recording WebSocket identity contract

Source: unchanged `backend/routers/transcribe.py` browser `/v4/web/listen`
handler and `web/app/src/lib/transcriptionSocket.ts` at the base
`d238a85af9d999992d9f0352db682cd9f11fc951`. The Web caller upgrades without an
Authorization header, then sends its first JSON message:

```json
{"type":"auth","token":"<product JWT>","device_id_hash":"<optional device hash>"}
```

A successful `auth_response` admits audio. The CLIENT-1 facade supplies the same
AUTH-1 product JWT used by API requests; no realtime-ticket exchange exists.
Cloudflare adds its provider `ready` frame and preserves the current transcript
wire format. Cloudflare rejects invalid/missing tokens, binary or malformed
first messages, repeated auth, and authentication timeouts with an unsuccessful
`auth_response` and close 4001; clients must not infer identical close codes from
the Python backend (which uses 1008). No credentials are forwarded to ASR.

Native macOS `TranscriptionService.swift` and Windows `omiListen.ts` authenticate
`/v4/listen` and `/v2/voice-message/transcribe-stream` with the Bearer upgrade
header. Those shipped callers remain supported. Reconnects re-enter AUTH-1's
current session/user check; a signed JWT from a revoked session is insufficient.
Session/account deletion semantics are owned by `contracts/auth/README.md`.

Cloudflare's shared `session-authority.ts` is the single HTTP adapter to Auth.
`realtime-admission.ts` owns per-uid session admission for native and browser
connections. A signed internal bootstrap lets Edge open one unauthenticated
Durable Object connection, without becoming a client token. Auth verification,
admission, optional migration fence and fair-use policy precede provider start.
The migration fence's API Core and Auth service bindings must be deployed before
Realtime; fresh brand profiles leave the fence off.

`deploy/cloudflare/tests/realtime.test.ts` executes these production handlers
through controlled Auth/Durable Object/provider seams in the existing fork
route CI lane. `tests/edge.test.ts` retains native routing and confirms the retired
web-ticket endpoint no longer mints credentials. Signature/claim and session
revocation tests remain in the shared AUTH-1 contract lane; the WS unit fixture
does not claim to reimplement cryptographic validation.

`POST /v2/realtime/session` is a different, real upstream live-model credential
route. It remains registered and returns the explicit disabled-capability
error envelope for OpenAI/Gemini on the pure CF target; an STT ticket is not a
valid replacement. Its missing native live-model owner remains CF-4 in
`dev/unified-main/09-cloudflare-route-migrations.md#cf-4-live-model-session`.
CLIENT-1 gates both the visible direct-provider control and token-request entry
when `allow_direct_model_providers` is false.
