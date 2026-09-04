# AUTH-1 PostgreSQL migration verification

Local candidate `1be55bf3bd` completes the PG first-login password upgrade and
moves the existing Workers fallback producer to `runtime/shared`. All 14
Workers callers were migrated; the Auth Dockerfile copies the shared runtime.
A verified session triggers the upgrade through Better Auth's real after hook.
The current envelope is verified again and compared in the SQL update, so an
intervening reset/import/deletion keeps its own value. A persistence failure
keeps the verified session usable and emits only bounded event fields.

Contract references: [Better Auth after hooks](https://better-auth.com/docs/concepts/hooks)
and the [official Firebase scrypt sample](https://github.com/firebase/scrypt#password-hashing).
The existing immutable import verification remains a pre-cutover operation:
zero sessions and exact imported rows are required. Native upgrades after login
are expected runtime mutations, not an invitation to overwrite the import receipt.

## Executed evidence

The isolated project is `memweft-pg-upgrade-20260904`, initially PostgreSQL 16.4
on 55467 and Auth 33067. No production service, account, or external provider was
used. Logs and executable probes live in `/tmp/memweft-implementation/auth/pg-upgrade/`.

- `npm --prefix auth-server test`: 48 passed, including four new production-hook
  and conditional-update tests. A real Better Auth memory adapter exercises
  failed/correct login, returned session, native second login and bounded fault
  telemetry; concurrent reset and changed-envelope tests execute the PG owner.
- CF typecheck,106 files / 792 tests and Auth `wrangler deploy --dry-run` passed.
  Shared AUTH runner also passed its backend 27 and CF 54 cases; full route lane
  passed Core 398 and 612 upstream route identities.
- Staged candidate `1db29579372880a8b9520d0e13d683420c6e3d46` passed 12 upstream
  and 3 fork checks. Commit tree equals that verified candidate. The historical
  failure-class guard explicitly skips in the shallow checkout.
- Standard `auth-server/Dockerfile` built a new Linux ARM64 Node 22.23.2 image,
  OCI index `sha256:03a1b5a68a2ee2064b29b58721ecbbe11d5133ec8a4e89a7cdbcf059edf9be1a`.
  It contains current source directly, with no runtime source mounts, and is
  labeled as the local uncommitted candidate preceding the source commit.
- `python3 .../live.py`: fresh schema check refuses, migrate/check pass; exact
  Firebase export validate/apply/verify/reapply pass before any sessions.
  Wrong password 401; correct login 200 retains imported UID and changes the
  actual PG credential to native format; native second login passes.
- A real PG trigger rejects only password updates. Correct login still returns
  a usable session, the legacy hash remains, and one bounded `postgres` fallback
  event is emitted. Remove the fault; next login upgrades successfully.
- `seed-legacy-jwk.js` reproduced incompatible legacy EdDSA signing. Check-only
  refused pending metadata, migrate/check converged. The unchanged real
  `auth-flow-smoke.py` then verified new and legacy JWTs through the backend,
  refresh, logout revocation and identity/account/session residuals. Moving
  that legacy key beyond publication grace removed it from JWKS and rejected
  its otherwise signed token. Synthetic subjects were reconciled to zero.

The initial probe used a copied database username incorrectly; only the private
fixture was corrected. The Linux AMD64 Auth build failed at unchanged `npm ci`
with npm's `Exit handler never called!`; its log remains `build.log`. ARM64
success is not an AMD64 qualification. Python backend AMD64 success is separate
SH2/SH4 evidence. External OAuth, signed native delivery, full provider erasure,
and production cutover remain outside this package.
