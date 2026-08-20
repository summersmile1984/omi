# Omi Auth Server (Better Auth)

Self-hosted auth for the 4C8G Omi deployment, replacing Firebase Auth.

## What it does

- email+password signup / signin (Better Auth)
- JWT plugin signs **ES256** JWTs carrying the Better Auth user id in `sub`, with public keys at `/api/auth/jwks`
- signed bearer session tokens let native clients exchange a persisted session for a short-lived JWT
- internal, secret-protected user lookup/deletion keeps account lifecycle provider-neutral
- User data stored in PostgreSQL (same server as the shim DB)

The Python backend verifies these JWTs via `utils/auth_shim.py` — the single
identity boundary before business routers run.

The Express 4 bridge catches every Better Auth handler rejection explicitly.
Database, schema, or signing-key outages return `503
identity_store_unavailable` with `Retry-After: 1` and `Cache-Control: no-store`
instead of leaving the request promise unhandled.

## Run

```bash
npm ci
export PORT=3000
export DATABASE_URL=postgresql://omi:omi-dev-password@localhost:5434/omi
export BETTER_AUTH_SECRET="<32+ byte secret>"
export BETTER_AUTH_URL="http://127.0.0.1:3000"
export BETTER_AUTH_TRUSTED_ORIGINS="http://127.0.0.1:3000"
export BETTER_AUTH_IP_HEADERS=x-forwarded-for
export AUTH_INTERNAL_ADMIN_SECRET="<separate internal secret>"
export AUTH_DEV_ISSUER_SECRET="<local-only bridge secret>"
npm run migrate
# Start only after migrate exits successfully.
npm start
```

Production Compose owns this ordering with the one-shot `auth-migrate` service
and `condition: service_completed_successfully`. `npm run migrate` is idempotent
and fails unless a post-migration schema read reports zero pending tables or
columns. `npm run migrate:check` is the non-mutating drift check.

The migrator also owns JWKS data compatibility. It adds explicit `alg`/`crv`
metadata, classifies legacy Ed25519/RSA keys from their public JWK, retires
incompatible signing keys without deleting them, and creates/validates a new
ES256 key. Retired public keys remain in `/api/auth/jwks` for
`AUTH_JWKS_GRACE_SECONDS`, so already-issued tokens continue to verify while
new tokens use ES256. Unknown or malformed key shapes and a missing active
ES256 key fail closed. Production requires explicit rotation and grace
intervals, and the grace interval must cover the issued JWT lifetime.

Native debug and release builds use the same email/password + signed bearer
session flow. Configure `OMI_AUTH_PROVIDER=better_auth` and
`OMI_AUTH_SERVER_URL`; `AUTH_DEV_ISSUER_SECRET` remains an optional local test
bridge and must never be included in a production app.

## Enable in the backend

```bash
export AUTH_PROVIDER=better_auth
export AUTH_JWKS_URL=http://127.0.0.1:3000/api/auth/jwks
export AUTH_JWT_ISSUER=http://127.0.0.1:3000
export AUTH_JWT_AUDIENCE=http://127.0.0.1:3000
export AUTH_SERVER_INTERNAL_URL=http://127.0.0.1:3000
export AUTH_INTERNAL_ADMIN_SECRET="<same internal secret as auth server>"
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/sign-up/email` | create account → session |
| POST | `/api/auth/sign-in/email` | sign in → session |
| GET | `/api/auth/token` | exchange a signed bearer session token for a short-lived JWT |
| POST | `/auth-issue` | optional local bridge; requires `AUTH_DEV_ISSUER_SECRET` bearer token |
| GET | `/api/auth/jwks` | public keys for the Python shim |
| GET | `/health` | liveness |
| GET | `/ready` | PostgreSQL readiness |
| GET/DELETE | `/internal/users/:uid` | internal lifecycle API; requires `AUTH_INTERNAL_ADMIN_SECRET` |
| GET | `/internal/users/:uid/residuals` | authoritative user/session/account residual counts for deletion reconciliation |

## Verify flow

1. Client signs in (`/api/auth/sign-in/email`) and persists the signed token from the `set-auth-token` response header
2. Client requests a JWT with `GET /api/auth/token` and `Authorization: Bearer <session-token>`
3. Calls backend with `Authorization: Bearer <jwt>`
4. Backend `verify_token` → `AUTH_PROVIDER=better_auth` → `auth_shim.verify_id_token`
   → pyjwt decodes with the `/api/auth/jwks` public key → returns `{'uid': ...}`

## Security notes

- `BETTER_AUTH_SECRET` must be a strong random value in production (never the dev default)
- production startup also requires an HTTPS `BETTER_AUTH_URL`, explicit trusted origins, database URL, and a separate internal admin secret
- production JWT issuer/audience must match the Python backend's `AUTH_JWT_ISSUER` / `AUTH_JWT_AUDIENCE`
- the public reverse proxy must overwrite the configured `BETTER_AUTH_IP_HEADERS` value with the client IP; never expose the bound origin where clients could spoof it
- ES256 keeps the signing private key server-side; only the public key is in `/api/auth/jwks`
- `/auth-issue` is not registered unless `AUTH_DEV_ISSUER_SECRET` is set; never expose that secret to a production app
- This is a self-hosted auth replacement; FCM push (`firebase_admin.messaging`)
  is out of scope and unchanged
