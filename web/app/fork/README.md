# Web client deployment boundary

The `WEB-1` fork builder consumes `overlays.json` in an isolated staging copy of
the current Moonshine/Bun app. It never rewrites or commits the upstream sources.
Both deployment targets use the same declared overlays and the same modules under
`src/lib/fork/`. The builder must verify each target exists and report every
overlay it applied. Upstream builds and tests use their original modules.

`NEXT_PUBLIC_OMI_PROFILE_JSON` contains one explicitly selected rendered profile.
The builder projects only public profile fields, plus
`NEXT_PUBLIC_OMI_PRODUCT_NAME` from `brand.display_name` and the allowlisted
`NEXT_PUBLIC_OMI_PRESENTATION_JSON` (tagline, support email and eight destination
links); secrets and manifest asset paths never enter the client environment.
API, WebSocket, and MCP URL consumers must use that same
profile. The existing settings MCP expression is transformed in staging to call
`mcpServerUrl()`, preserving a distinct MCP origin and any path prefix.

## Brand presentation

`deploy/web/presentation.ts` transforms static presentation literals in an explicit
list of reviewed components and metadata owners. It runs after the realtime/MCP
transforms and before staged production typechecking. JSX and template literals
retain escaping; dynamic user content and model prompt modules are not rewritten.
The artifact manifest records every changed owner and its output hash.

Footer, help, mobile notice and activity-mark overlays consume the same public
presentation. Activity indicators use the generated `/logo.png` and honor pause
and reduced motion. Help uses declared contact links without the upstream support
iframe. Empty community configuration omits its link. Download destinations use
the selected profile API's `/v2/desktop/download/latest`; current copy identifies
the macOS deliverable. An unconfigured binary still returns its actual server
error; changing the link does not establish that an installer is published.

Fork Vitest executes the production components through the same presentation
transform. It verifies the visible brand and links while submitting a goal that
contains the upstream name unchanged. This is a Web presentation boundary; it
does not change assistant identity, default prompts or backend response data.

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
- Referral login waits for the signed-in identity before claiming the query's
  code against the selected profile API. A query `environment` cannot choose a
  backend. Failures remain visible with explicit retry and a route into the
  account; success navigates to that profile's desktop download endpoint. The
  upstream referral helper and server proxy are also overlaid so neither keeps
  a separate managed-production destination. Login and referral flows retain
  the same Better Auth controller and do not use the referral cookie as identity.
- The current target profiles advertise webhook push. This is not browser FCM:
  the adapter reports notifications unsupported and never creates Firebase
  tokens or registers its service worker. Browser push requires its own client
  contract before that capability can be enabled.
- `WEB-1` also calls `applyRealtimeOverlay(stage)` from `realtime-overlay.ts`.
  The transformed Home composer hides direct-model conversation controls when
  `allow_direct_model_providers` is false, and the actual `useGeminiLive.start`
  method refuses before token acquisition or connection. Microphone transcription
  through `/v4/web/listen` remains available. The same transform runs in fork
  Vitest against the production hook; source-owner drift fails the build.

## Verification

`bash web/app/fork/test.sh` executes the real session controller and visible login
form through deterministic transport/storage seams. The upstream Web check runs
separately with the original files. `WEB-1` additionally builds and runs both
staged outputs; real browser sign-up, reload, API/WS auth, expiry refresh and
logout must use the same candidate and both real local authorities. Provider
OAuth, signing, native clients, and a complete white-label output scan are
separate gates and are not implied by these tests.
