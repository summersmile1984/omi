# Frame pixels: remote D1 migration repair

On 2026-09-06 the first isolated hosted frame-flow fixture applied all migrations
through `0167_frame_request_metadata.sql`, then failed on
`0168_frame_request_pixels.sql` with `incomplete input: SQLITE_ERROR`, code 7500.
The same SQL had passed the local SQLite migration and pixel behavior tests.

The remote D1 query parser can treat an unparenthesized CASE expression's END as
the trigger terminator. This is the platform behavior documented in
[workers-sdk issue 4727](https://github.com/cloudflare/workers-sdk/issues/4727).
Migration 0168 now parenthesizes its CASE expressions. Admission, byte quota,
atomic photo publication, state transitions and cleanup retain their predicates
and values. No prior production migration was rewritten remotely.

The second fixture used a fresh Auth database and App database and copied the
current SQL into its private evidence directory. Actual Wrangler 4.127.0
`d1 migrations apply AUTH_DB --remote --config <owned-auth-config>` and
`d1 migrations apply APP_DB --remote --config <owned-core-config>` both exited
zero. All 171 App migration files, including 0168, were applied. This is remote
migration evidence, not an assertion that the whole product was deployed.
The run later failed because its private Edge wrapper omitted an imported
module; the four created Workers and four data resources were deleted only
after ownership rechecks, and all were observed absent. The third fixture adds
an explicit ES-module inclusion rule and verifies the unchanged Edge bytes in
the frozen upload before creating remote resources.

The private records are `frame-public-hosted-20260906-a/result.json` and
`frame-public-hosted-20260906-b/result.json` under the operator's
`eddy-production` evidence directory. Migration logs retain the exact command
results and per-file application status. They contain no real account content.

The existing `frame-request-storage.test.ts` now carries a labeled static
portability tripwire for the affected SQL form. It is deliberately not described
as behavioral coverage or a generic SQL parser. The same suite and
`test_frame_request_pixels.py` execute the actual migrations and publication,
rollback, retention and erasure behavior. They run in the existing local/CI
`bash deploy/cloudflare/ci/routes.sh` lane. The new tripwire would have caught
this observed deployment failure; it is specific to the affected platform
grammar, rather than a new product abstraction.
