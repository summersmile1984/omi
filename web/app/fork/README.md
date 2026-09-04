# Web client deployment boundary

The `WEB-1` fork builder consumes `overlays.json` in an isolated staging copy of
the current Moonshine/Bun app. It never rewrites or commits the upstream sources.
Both deployment targets use the same four overlays and the same modules under
`src/lib/fork/`. The builder must verify each target exists and report every
overlay it applied. Upstream builds and tests use their original modules.

`NEXT_PUBLIC_OMI_PROFILE_JSON` contains one explicitly selected rendered profile.
The builder projects only public profile fields, plus
`NEXT_PUBLIC_OMI_PRODUCT_NAME` from `brand.display_name`; secrets never enter the
client environment. API, WebSocket, and MCP URL consumers must use that same
profile. The existing settings MCP expression is transformed in staging to call
`mcpServerUrl()`, preserving a distinct MCP origin and any path prefix.

## Identity behavior

- Email sign-in and sign-up call the configured `auth_base_url` directly at
  `/api/auth/*`. The authority must allow the exact Web origin, handle preflight,
  and expose `set-auth-token` to JavaScript. No wildcard origin is acceptable.
- The opaque Better Auth session lives in `sessionStorage`, scoped by profile
  name and auth origin. Closing the tab ends local persistence. Every restored
  session is checked with `/get-session` before publishing the user.
- `/token` receives that session as Bearer and returns a separate access JWT.
  JWTs stay in memory, refresh within 60 seconds of expiration, and concurrent
  API/realtime calls share one refresh. Decoding checks cache lifetime and user
  binding; only the authority/backend verifies signatures and revocation.
- Requests explicitly omit cookies. Email/session flows therefore do not rely
  on same-site or third-party cookie behavior. Cross-origin topology still
  requires CORS. OAuth buttons remain absent until the provider callback,
  cookie/state and exchange contract has a browser acceptance test.
- 401 clears a rejected stored session. Network/server errors preserve it and
  offer retry. Successful sign-out revokes the session at the authority before
  clearing local state. A late token exchange cannot restore a signed-out user.
- The upstream authentication port (`getIdToken`, `auth.currentUser`) resolves
  to this controller for REST, referrals, transcription and summary creation.
  There is no Firebase initialization or managed analytics in the fork provider.
- The current target profiles advertise webhook push. This is not browser FCM:
  the adapter reports notifications unsupported and never creates Firebase
  tokens or registers its service worker. Browser push requires its own client
  contract before that capability can be enabled.

## Verification

`bash web/app/fork/test.sh` executes the real session controller and visible login
form through deterministic transport/storage seams. The upstream Web check runs
separately with the original files. `WEB-1` additionally builds and runs both
staged outputs; real browser sign-up, reload, API/WS auth, expiry refresh and
logout must use the same candidate and both real local authorities. Provider
OAuth, signing, native clients, and a complete white-label output scan are
separate gates and are not implied by these tests.
