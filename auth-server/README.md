# Omi Auth Server (Better Auth)

Self-hosted auth for the 4C8G Omi deployment, replacing Firebase Auth.

## What it does

- email+password signup / signin (Better Auth)
- JWT plugin signs **ES256** JWTs carrying a `uid` claim, served at `/jwks`
- User data stored in PostgreSQL (same server as the shim DB)

The Python backend verifies these JWTs via `utils/auth_shim.py` — the single
`verify_id_token` drop-in — so no business code changes.

## Run

```bash
npm install
PORT=3000 \
DATABASE_URL=postgresql://omi:omi-dev-password@localhost:5434/omi \
BETTER_AUTH_SECRET="<32+ byte secret>" \
BETTER_AUTH_URL="http://127.0.0.1:3000" \
npm start
```

## Enable in the backend

```bash
export AUTH_PROVIDER=better_auth
export AUTH_JWKS_URL=http://127.0.0.1:3000/jwks
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/sign-up/email` | create account → session |
| POST | `/api/auth/sign-in/email` | sign in → session |
| POST | `/api/auth/jwt/sign` | mint a JWT (uid claim) |
| GET | `/jwks` | public keys for the Python shim |
| GET | `/health` | liveness |

## Verify flow

1. Client signs in (`/api/auth/sign-in/email`) → gets session
2. Client requests a JWT (`/api/auth/jwt/sign` with the session)
3. Calls backend with `Authorization: Bearer <jwt>`
4. Backend `verify_token` → `AUTH_PROVIDER=better_auth` → `auth_shim.verify_id_token`
   → pyjwt decodes with the `/jwks` public key → returns `{'uid': ...}`

## Security notes

- `BETTER_AUTH_SECRET` must be a strong random value in production (never the dev default)
- ES256 keeps the signing private key server-side; only the public key is in `/jwks`
- This is a self-hosted auth replacement; FCM push (`firebase_admin.messaging`)
  is out of scope and unchanged
