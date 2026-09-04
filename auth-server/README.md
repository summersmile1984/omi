# Omi Auth Server (Better Auth)

Self-hosted auth for the 4C8G Omi deployment, replacing Firebase Auth.

## What it does

- email+password signup / signin (Better Auth), plus explicitly configured
  operator Google/Apple OAuth
- JWT plugin signs **ES256**, 3,600-second JWTs with matching `uid`/`sub` and a real session `sid`; public keys are at `/api/auth/jwks`
- signed bearer session tokens let native clients exchange a persisted session for a short-lived JWT
- internal, secret-protected user lookup/deletion keeps account lifecycle provider-neutral
- User data stored in PostgreSQL (same server as the shim DB)

The Python backend verifies these JWTs via `utils/auth_shim.py` — the single
identity boundary before business routers run. It requires issuer/audience and
checks the current user/session through the trusted `/internal/verify` endpoint;
logout and deletion invalidate previously issued JWTs. See the shared
[access contract](../contracts/auth/README.md) for required environment variables,
legacy-token refresh, rotation and two-target validation.

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
# Optional operator OAuth pairs. A partial pair fails startup.
export AUTH_GOOGLE_CLIENT_ID="<operator Google OAuth client>"
export AUTH_GOOGLE_CLIENT_SECRET="<operator Google OAuth secret>"
export AUTH_APPLE_CLIENT_ID="<operator Apple Services ID>"
export AUTH_APPLE_CLIENT_SECRET="<operator-generated Apple client secret>"
npm run migrate
# Start only after migrate exits successfully.
npm start
```

The self-host Docker image routes both serving and migration through
`self-host-runtime.mjs`: `SELF_HOST_STAGE=local` selects development; `beta` and
`production` select production before Auth modules import. The default is
production, unknown stages/commands fail, and ambient `NODE_ENV` cannot relax
production or beta. Use `node self-host-runtime.mjs migrate [--check]` and
`node self-host-runtime.mjs serve` inside this image. The source helper lives at
`deploy/self-host/auth-runtime.mjs` and is copied beside `src` during the build.
Direct `npm start` remains the component development command shown above.

Source commit/tree labels are applied after dependency and source layers;
changing attribution alone must reuse the locked `npm ci` layer.

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

## Migrate Firebase Auth identities

The permanent importer preserves the Firebase UID used by PostgreSQL, MinIO,
Qdrant, and account-deletion ownership. It accepts the JSON emitted by
`firebase auth:export` plus a separate JSON file containing the Firebase
project's `SCRYPT` password-hash parameters. The signer key is used only at
the auth boundary and is never copied into PostgreSQL or an import receipt.
Imported password accounts store an envelope containing the per-user salt and
hash plus a non-secret configuration fingerprint. Better Auth verifies those
passwords locally with Firebase's modified-scrypt algorithm; new passwords
continue to use Better Auth's native scrypt format. A successful email sign-in
upgrades an imported credential to that native format. The PostgreSQL owner
re-verifies the current envelope and conditionally updates that exact hash,
so a concurrent password reset/import or account deletion keeps its newer state.
Upgrade persistence failure preserves the already verified session and legacy
credential for retry on the next login; shared `recordFallback` emits a bounded
`postgres` event without user, password, hash, or SQL details.

Run the normal schema migrator first, then validate, apply, and verify the exact
immutable export:

```bash
export AUTH_FIREBASE_SCRYPT_SIGNER_KEY='<base64_signer_key>'
export AUTH_FIREBASE_SCRYPT_SALT_SEPARATOR='<base64_salt_separator>'
export AUTH_FIREBASE_SCRYPT_ROUNDS='<rounds>'
export AUTH_FIREBASE_SCRYPT_MEM_COST='<mem_cost>'

npm run migrate
npm run migrate:firebase:validate -- \
  --users /migration/firebase-users.json \
  --hash-config /migration/firebase-hash-config.json
npm run migrate:firebase:apply -- \
  --users /migration/firebase-users.json \
  --hash-config /migration/firebase-hash-config.json
npm run migrate:firebase:verify -- \
  --users /migration/firebase-users.json \
  --hash-config /migration/firebase-hash-config.json
```

Both migration inputs contain customer identity material and must be regular
mode-0600 files (not symlinks); the importer rejects weaker permissions before
parsing either file.

The apply step takes a PostgreSQL advisory lock and a serializable transaction,
requires empty Better Auth user/account/session tables, and records a single
source SHA-256/config fingerprint/count/content receipt. Reapplying the exact
export is idempotent; a different source, a non-empty unreceipted target,
duplicate UID/email/provider identity, disabled account, unsupported provider,
or a user without a supported sign-in identity fails closed. Non-empty Firebase
`customAttributes` and `phoneNumber` fields also fail closed instead of being
silently discarded; reconcile those identities explicitly before importing.
Google and Apple accounts are imported only when their operator OAuth pairs are configured.
Sessions are intentionally not migrated; clients must establish a new signed
Better Auth session after cutover.

The import digest orders opaque IDs by UTF-8 bytes, matching PostgreSQL's
[`COLLATE "C"` ordering](https://www.postgresql.org/docs/16/collation.html).
Do not replace this with locale-sensitive sorting: mixed-case, punctuation and
Unicode IDs must reconcile identically on every migration host. Successful
existing receipts retain the same digest; failed transactions leave no receipt.

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
| POST | `/api/auth/sign-in/social` | begin an explicitly configured operator Google/Apple OAuth flow |
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
