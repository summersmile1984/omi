# Backend — Fork Rules (cloud-neutral / self-hosted)

Upstream rules live in [`AGENTS.md`](./AGENTS.md); this file adds only what is
true for this fork. It is fork-owned: upstream never touches it, so it never
conflicts on an upstream sync.

## Cloud-neutral runtime switches

The self-hosted deployment selects adapters through environment variables that
are read at call boundaries, never at import time:

| Variable | Effect |
|---|---|
| `FIRESTORE_PG_DSN` | Routes **both** customer and compute data through the `firestore_pg` PostgreSQL facade. Never split one process between PostgreSQL and Firestore. |
| `STORAGE_BACKEND=minio` | Selects the GCS-compatible MinIO adapter instead of Google Cloud Storage. |
| `QUEUE_BACKEND=redis` | Selects the Redis worker queue instead of Cloud Tasks; workers authenticate with `QUEUE_REDIS_WORKER_SECRET`. |
| `AUTH_PROVIDER=better_auth` | Verifies asymmetric JWTs fetched from `AUTH_JWKS_URL` instead of Firebase ID tokens. |

`get_firestore_client()` from `database._client` remains the only supported way
to obtain a client, and `FIRESTORE_PG_DSN` changes what it returns for the whole
runtime, including customer entitlement and quota reads.

## Self-hosted identity

`AUTH_PROVIDER=better_auth` is the self-hosted path. JWT verification accepts
only asymmetric ES256/RS256/EdDSA keys from `AUTH_JWKS_URL`. The optional
auth-server `/auth-issue` development bridge must stay disabled unless it is
protected by `AUTH_DEV_ISSUER_SECRET`, and it must never be reachable in a
production deployment.

## Fork discipline

Do not modify upstream files under `backend/`. Fork behavior belongs in
fork-owned modules and is attached at startup; see
[`dev/unified-main/00-upstream-touch-policy.md`](../dev/unified-main/00-upstream-touch-policy.md)
for the technique catalog and the allowlist that CI enforces.
