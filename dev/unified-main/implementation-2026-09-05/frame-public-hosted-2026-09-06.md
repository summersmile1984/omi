# Frame requests: authenticated hosted business flow

The eight upstream frame-request routes now enter Core through the actual
Edge authenticated proxy. Better Auth establishes the owner, existing account
cutover gates admit traffic, and the Rate Limit Durable Object enforces the
upstream hourly budgets: read 120, write 120 and upload 30 per account. Edge
strips caller identity headers and signs its Core assertion; upload and image
response bytes are streamed unchanged across this boundary.

The route inventory keeps all 619 upstream identities. This family moves eight
slots from blocked to staging-owned after its scoped hosted proof. The result
is 594 staging-owned and 25 blocked, not complete product qualification.

The fixture freezes unchanged Core/Auth/Rate modules from candidate frame-c,
builds current Edge and snapshots current SQL (including the remote migration
repair). Two fresh D1 databases and two R2 buckets contain synthetic data only.
Public access to the diagnostic Edge requires a private probe header before
entering the unchanged handler. No production resource is bound. Core uses
native Cloudflare Images, and Rate Limit uses a real hosted Durable Object.
The storage-control wrapper imports the actual Jobs cleanup functions; it does
not exercise Queue delivery or cron scheduling.

A configured global JIT rollout and a synthetic completed conversation are the
only App data seeded by the fixture. Auth signup issues both principals and
sessions; the frame requests, state transitions, image bytes, receipts, photo
attachment and erasure all arise through production handlers. The fixture is
therefore not evidence of conversation creation, JIT trigger generation or
an actual desktop screenshot capture.

## Hosted evidence, 2026-09-06

The successful continuation completed **35 HTTP requests** using the two real
Better Auth accounts and original sessions registered by the d fixture. Session
recovery read only these synthetic sessions from its owned Auth D1; it did not
mint or replace identities. The client read the actual public account control
and carried generation 1; a generation-0 request was rejected. The preceding
run also proved that missing rollout denies frame work. Neither admission
policy nor default prompts were changed to make the fixture pass.

- Creation and device claim succeeded. A different account's upload returned
  404, a wrong device returned 403, and the owner's WebP upload returned 200.
- Temporary preview returned the exact canonical JPEG recorded in the D1
  SHA-256/byte-count receipt. Other-account access returned 404 and missing
  authentication returned 401. Status and pending delivery used the public API.
- A conversation frame was uploaded and promoted. Repeating promotion returned
  the same attachment. The public photo list and image endpoint returned the
  correct storage identity and exact same JPEG, with cross-account denial.
- The actual cleanup function removed the unreferenced temporary promotion copy:
  temporary/permanent object counts were 1/1. Enabling the global JIT kill denied
  temporary preview while the attached conversation photo remained readable.
- Public conversation deletion revoked the photo and cleanup produced counts
  1/0. Pruning the remaining temporary request then cleaned both buckets to 0/0.
  Public signout revoked the previously issued JWT: pending polling returned 401.
- Independent Pillow inspection of the received JPEG confirmed 200×400 pixels
  for a WebP with EXIF orientation 6. Corner maximum channel errors against an
  independently transposed reference were 0, 1, 1 and 0; EXIF was empty and the
  private metadata marker was absent. The stored/read JPEG was 1,490 bytes,
  SHA-256 `1a39755676a7817f3c8fbbf6a6f8013fd411b2c82da49a82b1fa9860998faa82`.

The immutable private journals are `frame-public-hosted-20260906-d/result.json`
and `frame-public-hosted-20260906-d-resume1/result.json`; the latter also holds
`image-semantics.json` and the received synthetic image. The first run stopped
on the test client's stale generation. Its first cleanup call received an
explicit API 401. Fresh credential lookup and authoritative version/resource
reads proved the five Workers and four data resources still belonged to that
run before continuing. No deployment or migration was replayed during recovery.
After success, all five Workers, both D1 databases and both R2 buckets were
removed with exact ownership checks and independently observed absent.

## Local checks and remaining scope

`env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`
passed route inventory/manifest validation, TypeScript, **116 Worker test files /
950 tests**, **653 Core tests** and **150 AI tests**. The new 17 Edge tests execute
all eight routes' signed identity, request bytes, quota selection and response
streaming, plus unauthenticated, revoked, account-fenced and quota-denied cases.
These tests control service/DO IO; the hosted run above uses the real services.
The separate [remote migration repair](frame-d1-migration-2026-09-06.md) records
why SQLite-only validation failed to catch the D1 grammar issue.

The hosted fixture was deleted, not promoted into production. It did not
exercise desktop capture, the other JIT routes, model inference, Queue delivery,
cron scheduling or whole-account erasure. Existing local tests cover the latter
storage/erasure owners through controllable IO. The full CF-4 and dual-target
release qualification remain required, along with the other 25 route slots.
The Eddy production Workers and signed desktop-to-production acceptance are
still separate incomplete deployment requirements.
