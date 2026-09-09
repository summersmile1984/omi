# Self-host identity deletion consumer

The PostgreSQL Auth service in `auth-server/src/index.js` owns the internal wire
contract. This document describes the self-host backend consumer; Cloudflare's
internal deletion adapter has a separate current wire shape.

Both live-session verification and deletion use `AUTH_SERVER_INTERNAL_URL` and
`AUTH_INTERNAL_ADMIN_SECRET`, validated by `utils.auth_shim.internal_authority`.
The internal URL is an origin without credentials, path, query or fragment.
HTTPS is required unless the operator explicitly enables internal HTTP.

The consumer sends the internal administrator bearer only to that origin,
refuses redirects, and has a five-second request timeout. It does not forward
the caller's JWT or interpret a service outage as a missing identity.

UIDs remain one encoded path segment. Exact `.` and `..` values accepted by the
Firebase importer are explicitly percent-encoded, because `quote()` preserves
dots and HTTPX otherwise resolves them as path navigation. Literal percent
characters remain encoded once; Express decodes the route parameter once.

| Operation | Accepted response |
| --- | --- |
| `DELETE /internal/users/{uid}` | HTTP200 with exactly `{"success":true}`, or HTTP404 with exactly `{"error":"user_not_found"}` |
| `GET /internal/users/{uid}/residuals` | HTTP200 with exactly `{"users":0,"sessions":0,"accounts":0}`; every count must be an integer, not a boolean |

Every accepted delete is followed by residual proof. The final completion
wrapper checks again after required provider cleanup, before publishing the
existing PG completion receipt. A missing user with orphaned sessions/accounts
does not pass. A DELETE that succeeded remotely but lost its response fails the
current attempt; retry may accept explicit absence plus zero residuals.

This is an idempotent client of the existing identity authority, not a new
deletion state machine. The existing worker, PG marker/receipt owner and
provider lock remain authoritative. Tests execute the real symbol replacement,
HTTP response decoding and final completion wrapper; live evidence is recorded
in `dev/unified-main/implementation-2026-09-04/AUTH-identity-deletion-verification.md`.
