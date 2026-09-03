# App — Fork Rules (cloud-neutral / self-hosted)

Upstream rules live in [`AGENTS.md`](./AGENTS.md); this file adds only what is
true for this fork. It is fork-owned, so it never conflicts on an upstream sync.

## Local Better Auth bridge

The local Better Auth sign-in bridge exists only in non-release builds, and only
when **both** `OMI_AUTH_SERVER_URL` and `OMI_AUTH_DEV_ISSUER_SECRET` are supplied
as compile-time defines. The bridge UID returned by the auth server is the stored
owner identity. Release builds never expose this path.

## Fork discipline

Do not modify upstream files under `app/`. Fork behavior belongs in fork-owned
files and package overrides; see
[`dev/unified-main/00-upstream-touch-policy.md`](../dev/unified-main/00-upstream-touch-policy.md).
