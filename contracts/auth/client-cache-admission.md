# Client JWT cache admission

The opaque Better Auth session is the durable credential. A client exchanges it
for a short-lived JWT; API and WebSocket services remain authoritative for
signature, issuer/audience, key publication and live-session revocation.

Client cache admission allows `iat <= floor(device UTC seconds) + 60`, inclusive.
It still requires a nonnegative integral `iat`, integral `exp`, `exp > now`,
`exp > iat`, lifetime at most 3600 seconds, matching `uid = sub = current owner`,
and a nonempty `sid`. No grace extends an expired JWT. This bound does not change
server verification or permit a JWT-only legacy cache to restore a session.

[RFC 7519 sections 4.1.4–4.1.6](https://www.rfc-editor.org/rfc/rfc7519.html#section-4.1.4)
define NumericDate claims and acknowledge small clock-skew leeway. The precise
60-second client bound is our engineering policy, not a value prescribed by the
RFC. On 2026-09-04 the Android emulator clock lagged the live Node/Postgres auth
fixture by 26 seconds, causing a real successful registration to fail the
previous zero-tolerance client check. Regression cases accept +30 and +60,
reject +61, and continue rejecting expiration/owner/lifetime failures.

Flutter, Web and macOS consumers use this same cache contract. Platform tests
execute their production exchange decoder; successful cache admission never
substitutes for a real authenticated backend request.

Verification (2026-09-04): Web `bash web/app/fork/test.sh` passed 14 auth
controller tests and six UI/realtime tests; macOS `xcrun swift test
--package-path desktop/macos/fork` passed 12 production decoder/owner tests.
Flutter's native identity package records its real Android 26-second incident,
both target APKs and production decoder regression in `app/fork/VERIFICATION.md`.
Raw local logs: `/tmp/memweft-implementation/flutter/clock-{web-suite,macos-tests}.log`.
