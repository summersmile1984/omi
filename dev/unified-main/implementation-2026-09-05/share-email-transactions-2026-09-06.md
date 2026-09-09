# Cloudflare share-email transactions — 2026-09-06

The Jobs/Core transaction now reserves recipients, charges daily quota, publishes
the owned summary, performs one provider call, and records its result. The public
Edge POST remains unregistered: Eddy's native sender identity and real outbound
delivery have not been qualified. This work does not promote that route or claim
production deployment. The inventory remains 601 staging-owned / 18 blocked out
of 619 identities.

## Ownership and behavior

Migration 0171 adds three D1 tables and an export view. Preparation atomically
claims only new recipients, charges at most 30 recipients per UTC day, publishes
the conversation without downgrading public visibility, and records its write
revision. Every subsequent conversation write advances that revision. Rejection
refunds the original day's quota and restores private visibility only if that
attempt still owns the publication revision. A concurrent same-value share write
also defeats rollback. Recipient claims and the ledger response are read in the
same batch so a later rejection cannot alter an already computed response.

Core's three private endpoints require signed, request-bound internal authority.
Jobs changes prepared to dispatching before calling the structured Cloudflare
Email Service binding once. A confirmed rejection releases claims. Acceptance or
an unknown outcome retains them; an unknown outcome returns 504, records shared
sanitized fallback telemetry and cannot authorize another provider call. The
scheduled bounded sweep retires abandoned preparations or marks interrupted
dispatches ambiguous. It never sends mail. Terminal records drop the HTML payload.

The original upstream recipient, request, sender-name and safe Markdown contracts
are staged from their authoritative source files. The only email-template change
is the white-label footer: brand name and URL replace the hardcoded Omi link.
Sending calls no model. The four protected default-prompt files remain identical
to baseline `9b7e48dca2`.

Export includes receipts, recipient addresses and quota through the existing
owner boundary, excluding HTML and dispatch leases. The three tables have account
deletion fences and join the existing residual scan and purge. Conversation
deletion cascades the associated receipts and recipient claims.

## Local verification

- `env PATH=<pinned-node-22-bin>:$PATH bash deploy/cloudflare/ci/routes.sh` passed
  inventory, manifest and typecheck checks, 974 Worker tests, 730 Core tests and
  150 AI tests. Core reports one existing Starlette/AnyIO deprecation warning.
- After adding the scheduled-recovery test, seeding the existing account-erasure
  workflow with pending mail, and correcting two documented provider error codes,
  `npm test` passed **122 files / 977 Worker tests**; `npm run typecheck` passed.
  Core and AI production code did not change after the full lane above.
- Eighteen Core transaction tests execute the real ASGI routes against the full
  174-migration App schema. Twelve Jobs transport tests cover dispatch ordering,
  deduplication, provider rejection/ambiguity, persistence failure, signed Core
  identity and unconfigured sender denial. The scheduled SQL test exercises the
  100-preparation/100-dispatch sweep bound, refund and retained ambiguity. The
  existing 16-test account-erasure suite now seeds pending mail as well.
- Python formatting checks for the new implementation, projector and tests,
  TypeScript formatting for the new files, and `git diff --check` passed.

## Hosted transaction verification

The successful run used `eddy-mail-20260906-b`: five actual Workers (Auth, Rate
Limit, Core, Jobs and Edge), ten Auth migrations and all 174 App migrations in
fresh D1 databases. Two synthetic accounts signed up and acquired actual JWTs.
The private harness added the otherwise unregistered POST to a diagnostic copy
of Edge, using its production authentication/proxy helper. The Jobs mail binding
was controlled and recorded synthetic outcomes in the owned test database;
**zero outbound emails were sent**. This is native-platform transaction evidence,
not Email Service integration or production-route qualification.

The HTTP path proved unauthenticated and foreign-owner denial, invalid input,
accepted send and replay without resending, definite rejection with private-link
rollback followed by a successful retry, ambiguous delivery without resending,
and rejection after another actor's same-value share write without revoking that
actor's publication. Five controlled provider calls produced five terminal
receipts, three retained recipient claims and quota usage of three. Actual owner
export returned those records and excluded mail payloads. The structured sender,
reply-to and footer used the synthetic owner's identity and Eddy branding.

The run finished at `2026-09-06T12:35:29.371Z`. All five owned Workers and both
owned databases were observed absent after cleanup. Journal SHA-256:
`abb8d2207d7275be8498ed9b5157dbb22d8f9503c23114122a691acaab9df0ff`.
The frozen Core transaction modules match the current source byte for byte.
The later Jobs change replaces guessed validation codes with the documented
`E_VALIDATION_ERROR` and `E_FIELD_MISSING`; its two cases passed locally. The
hosted rejection used unchanged `E_RATE_LIMIT_EXCEEDED`, so this run does not
claim that the complete current Jobs bundle was deployed.

The earlier `-a` harness failed while deploying its diagnostic Edge wrapper:
the wrapper imported `index.js`, but its entry compiled as `edge-source.js`.
It executed no business HTTP requests, and all owned resources were cleaned.
The corrected `-b` run used fresh resource identities.

Private evidence is under `$CODEX_HOME/eddy-production/`:

- `share-email-hosted-20260906-{a,b}/result.json`
- `share-email-routes-20260906.log`
- `share-email-workers-final-20260906.log`
- `share-email-account-observation-20260906.json`

## Remaining release boundary

Eddy needs an onboarded sender domain and a reviewed native `SHARE_EMAIL` binding
with `SHARE_EMAIL_FROM_ADDRESS`. The account's existing sending domain belongs to
another product; it was not repurposed. Real provider acceptance/delivery,
binding/resource qualification, public Edge admission and the complete CF-4 and
CI-1 candidate qualifiers remain required. Default model prompts, Server OS and
production resources were not changed by this transaction implementation.

Provider contract: [Cloudflare Workers Email Service API](https://developers.cloudflare.com/email-service/api/send-emails/workers-api/).
