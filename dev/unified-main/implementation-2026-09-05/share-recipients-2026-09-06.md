# Cloudflare calendar recipient verification — 2026-09-06

The public `GET /v1/conversations/{conversation_id}/share-recipients` route now
belongs to API Core. The original upstream calendar-source, participant-count,
captured-name, normalization, owner-exclusion and response contracts are staged
from `backend/utils/conversations/share_email.py` and
`backend/routers/conversations.py`. No prompt or backend behavior was changed.

Edge verifies the session, applies account cutover admission, strips caller
identity headers and signs the specific Core request. Core reads only the owned
conversation's calendar metadata. Auth supplies the current owner's email using
a signed private request. Core rechecks locks, deletion fences and calendar state
after that lookup; a concurrent calendar change returns 409, a lock returns the
upstream 402, and an unavailable subject returns 404. Missing owner email produces
an empty proposal with the shared suppression event. Provider errors do not
become successful empty proposals. Responses use `private, no-store`.

## Verification

- `uvx uv==0.12.3 run pytest -q tests/test_share_recipients.py` in API Core:
  25 passed. The production HTTP route executes against all 173 App D1 migrations
  in SQLite with only the Auth transport controlled. Cases include all five
  allowed calendar sources, inferred-source suppression, duplicate addresses,
  owner exclusion, legacy accounts without a cutover row, the five-recipient and
  ten-attendee boundaries, profile errors, and changes during the Auth await.
- `node node_modules/vitest/vitest.mjs run tests/share-recipients-edge.test.ts`
  in `deploy/cloudflare`: 3 passed against the production Edge handler, covering
  request-bound identity, unauthenticated denial and cutover denial.
- `env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh`:
  route inventory, backend inventory, manifests and typecheck passed;
  120 Worker test files / 964 tests, Core 712 tests, AI 150 tests passed.
  The Core suite has one existing Starlette/AnyIO deprecation warning.
- `scripts/backend-python-format --check` for the changed Python files and
  `git diff --check` passed.

The live run used the isolated `eddy-recipients-20260906-a` prefix, four Workers
(Auth, Rate Limit, Core and Edge), a fresh Auth D1 with all ten migrations and a
fresh App D1 with all 173 migrations. Two synthetic accounts signed up through
the actual Auth API, acquired JWTs, and reached Core through the public Edge.
Synthetic calendar records were seeded directly into the owned test D1; calendar
provider synchronization was not part of this run.

All 37 HTTP assertions passed, including 12 recipient business requests. The live
cases proved the five attributed calendar sources, deduplication, owner exclusion,
cross-user denial despite forged identity headers, locked-conversation denial,
screen-identity suppression, paired calendar-event attendees and deletion denial.
The other HTTP requests covered Auth and route readiness. No model call or email
dispatch was made. The Edge wrapper only restricted the synthetic deployment to
the private probe key; the business request used the unchanged Edge/Core handlers.

The live run finished at `2026-09-06T11:55:23.722Z`. Every owned Worker and both D1
databases were observed absent after cleanup. Its private journal SHA-256 is
`95fbdcd1ed5f7379a1d762402b07517eb753b95fb0818c4d95e05d6d3a4a59e9`.
The four relevant frozen Core modules match the current source/staged contract
byte for byte. The four protected default-prompt files remain byte-identical to
`9b7e48dca2`.

Private evidence resides under `$CODEX_HOME/eddy-production/`:

- `share-recipients-hosted-20260906-a/result.json`
- `share-recipients-routes-20260906.log`
- `share-recipients-source-identity-20260906.json`
- `share-recipients-production-observation-20260906.json`

## Remaining release work

The inventory now contains 601 staging-owned and 18 blocked identities out of
619. `POST /v1/conversations/{conversation_id}/share-email` remains blocked:
recipient reservations, quota, visibility publication/rollback, ambiguous-delivery
handling and the native sender configuration still require their implementation
and verification. This read-only change does not count as email delivery.

Cloudflare's Email Service supports arbitrary recipients on Workers Paid after
sender-domain onboarding. The account audit found one sending subdomain belonging
to another product and zero verified routing destination addresses. Eddy's sender
identity has not been selected; no DNS, email configuration or delivery was changed.
See the [native binding documentation](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/)
and [sending prerequisites](https://developers.cloudflare.com/email-service/platform/limits/).

At `2026-09-06T11:58:27.194Z`, all nine Eddy production Worker names were still
absent. The existing Eddy macOS app passed a fresh strict codesign verification,
but its live production end-to-end path remains unverified. The missing CF-4
product and CI-1 dual-target qualifiers, remaining route contracts and current
release candidate qualification remain required. No production gate was bypassed.
