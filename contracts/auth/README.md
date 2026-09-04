# Shared product access authentication

Both deployment targets use `auth/shared/jwt-policy.mjs` (typed by its `.d.mts`
declaration). Runtime-neutral JavaScript is directly importable by the existing
Node server and bundled by Wrangler; no additional compilation or dependency
version is required. The shared boundary addresses the wrong/missing issuer and
audience accepted by the Server verifier shipped in
[self-host PR #7](https://github.com/summersmile1984/omi/pull/7), reproduced in the
[2026-09-04 audit](../../dev/unified-main/audit-2026-09-04/01-self-host-action-plan.md).

- A product JWT has `uid == sub`, nonempty `sid` (Better Auth session ID),
  `iss`, `aud`, integer `iat` and `exp`. Its maximum lifetime is 3,600 seconds.
- `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` select explicit HTTP(S) origins;
  both default to the public Auth origin at issuance. The Python verifier
  requires both settings. Array audiences are outside this product contract.
- Both targets issue ES256 keys, rotate after 30 days and publish prior public
  keys for a 2-day grace period. The grace period must cover the token lifetime.
  Existing supported asymmetric keys can verify during their publication window.
- Signature verification is followed by a read of the current user and session.
  Logout, session expiration and account deletion invalidate previously issued
  JWTs immediately. Workers also checks its existing deletion fence, including
  the interval before deletion has removed the user/session rows.
- `/internal/verify` requires `x-internal-assertion-secret` and returns `uid`,
  `authority: better-auth`, and `sessionGeneration: sid`. Server's secret is
  `AUTH_INTERNAL_ADMIN_SECRET`; Workers uses `INTERNAL_ASSERTION_SECRET`.
  Python uses `AUTH_SERVER_INTERNAL_URL` plus the former secret name. Plain
  HTTP on this trusted internal transport requires `AUTH_INTERNAL_ALLOW_HTTP=true`.
- Existing users need no additional user columns or generation document. They
  use their normal session to call `GET /api/auth/token`. Previously issued
  JWTs without the required claims must be refreshed; a signature alone is
  insufficient to infer a revocable session. This is an intentional candidate
  rollout boundary, to be coordinated with CLIENT-1 before release.
- The session credential and product JWT are different credentials. Clients
  retain the session credential for refresh/logout and use JWTs for product API
  and realtime authentication. They read `exp`, not an assumed client lifetime.
- Both HTTP adapters use `auth/shared/cors-policy.mjs` to expose `set-auth-token`
  and `set-auth-jwt` only to configured Web origins. Browser requests can use an
  explicit bearer session without third-party cookies. Preflights permit the
  Authorization and Content-Type headers; unknown origins receive no allow-origin
  header. Server uses `BETTER_AUTH_TRUSTED_ORIGINS`, Workers `ALLOWED_ORIGINS`.
- MCP OAuth keeps its existing `/api/auth` issuer, consent and scoped grant
  validation. The Workers adapter preserves OAuth plugin metadata while product
  token payloads set their explicit shared issuer/audience. An MCP token cannot
  be used as an unrestricted product JWT.

The production options and `definePayload` interface were checked against the
installed Better Auth versions (Node 1.6.26, Workers 1.7.2) and the
[official JWT documentation](https://better-auth.com/docs/plugins/jwt).
`claims.json` is executed by both the real Python cryptographic verifier and
the real Workers Better Auth verifier with local SQLite. It deliberately covers
legacy principals, rejected old token shapes and signed negative claims.

`bash scripts/fork/auth-contracts.sh` runs the Node suite, Workers auth suites,
and backend selected-suite runner. Provision locked Node dependencies first
with `npm ci --ignore-scripts` in `auth-server` and `deploy/cloudflare`, and run
`make setup` for the backend. The existing fork CI workflow installs these
dependencies and runs the same manifest entry. Tests make no service/network calls.

Live evidence for this implementation is kept in
`/tmp/memweft-implementation/auth/`: a newly built isolated Auth+PG stack and
actual local workerd+D1 both passed signup, JWT exchange, refresh and logout
revocation; PG also passed account deletion through its existing smoke command.
The hermetic Workers test exercises deletion fences and the real deletion route.
These are identity-subsystem results, not the full SH-2/CI-1 product loop.
Shared Firebase password implementation, complete migration/rotation acceptance,
and the CLIENT-1 integration gate remain AUTH-1 work until recorded as passed.
