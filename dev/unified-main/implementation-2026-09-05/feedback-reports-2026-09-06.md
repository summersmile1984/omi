# Cloudflare feedback ledger and admin reports

Base: `1da13c119371537c9ae19c08528831e4f2a7cdd9` on
`codex/unified-delivery`. This change completes the five feedback-report route
registrations and their rating, storage, privacy and scheduler dependencies.
The route inventory moves from **594 owned / 25 blocked** to **599 owned /
20 blocked**, out of 619 upstream identities. It does not qualify the full
Cloudflare production release.

## Implemented boundary

- The ordinary builder and Core test runner stage the original upstream
  feedback models, desktop rating schema and pure report policy. Their source
  owners participate in release identity. No default prompt was edited.
- Five rating surfaces append immutable events in the same D1 transaction as
  the current rating: text, voice, notification, conversation summary and memory
  keep/discard. Events preserve reason/comment and message coordinates. Missing
  legacy reason remains uncaptured; unknown legacy reasons retain the vote.
- Jobs owns the existing ADMIN_KEY gate and hashed administrator attribution.
  It signs a request-specific internal assertion for Core, whose feedback
  service rejects user assertions and unrelated internal actors. The admin key
  does not cross into Core, and all public report responses are no-store.
- Reports use upstream reason-preserving collapse and UTC/raw-row/entry/byte
  bounds. Current aggregate/entry publication commits atomically under a lease;
  failure preserves the previous report and late publishers cannot replace it.
- Message context uses actual wire timestamps, including timezone offsets and
  microseconds. The mixed `cf_chat_messages.created_at` history sort keys are
  not interpreted as epoch seconds. Generation reads only metadata. Admin text
  hydration is bounded, uid-scoped and excludes unreadable enhanced ciphertext.
- Events join user export. Events and independently erasable report entries
  join the Jobs account-deletion registry and SQL fences. Deleting an event
  cascades its report entries; an earlier generator snapshot cannot restore
  fenced/deleted entries. Aggregates contain no user identifiers.
- The existing five-minute Jobs scheduled lane ensures yesterday's report after
  01:30 UTC. Completed days are skipped; failed/busy generation is retried by a
  later invocation. Scheduler admission/idempotency is covered hermetically;
  this verification did not wait for a real Cloudflare cron tick.

The precise API and limits are documented in
[ForkCloudflareFeedback](../../../docs/doc/developer/ForkCloudflareFeedback.mdx).

## Hosted business evidence

An isolated Cloudflare run completed at **2026-09-06 10:33:12.909 UTC** with
five real Workers (Auth, Rate Limit, Core, Jobs, Edge), two fresh remote D1
databases, the complete 10 Auth / 173 App migration files, and synthetic users.
Both database migration applications succeeded. Only the probe Edge was public,
behind an additional private probe key; Core/Auth/Jobs were service bindings.
No AI provider calls or production bindings were used.

All **26 HTTP assertions** passed without unexpected responses:

1. Two actual Better Auth signups and token issuance, followed by three messages
   saved through the desktop public API. The feedback ledger was not seeded.
2. Text/voice/notification ratings, a later bare rating, summary feedback and a
   real memory create/review produced **six events across all five surfaces**.
   Rating another user's message returned 404.
3. User JWT without admin key returned 422; a wrong key returned 403. Invalid
   list limit returned 422. The correct admin key reached Core through Jobs.
4. Generating the UTC report returned **three distinct negative targets**. The
   later bare vote did not overwrite the informative reason. The report and
   stored events contained no copy of the rated message text.
5. Context hydration returned the exact synthetic question, answer and follow-up
   through the stored three-turn pointer. Listing dates and generating yesterday
   also returned successful contract responses.
6. A deletion tombstone immediately hid the user's report entries and made the
   event context return 404. Physical event deletion left zero corresponding
   report entries in remote D1.

The **11 changed Core runtime modules** match the hosted frozen module bytes.
The final Edge bundle also matches exactly. Jobs was reformatted after its
initial compilation; the locked esbuild whitespace-normalized emitted code is
identical (SHA-256
`8d2f49d52169153c1e50a4f8b60eb9ccd3cf58e85221681099b7c4a737b3ef25`).

Private evidence remains under
`~/.codex/eddy-production/feedback-hosted-20260906-d/`; its final journal SHA-256
is `4686f1142bcd1009bc6a16ed2fb303157a14c7dec632536942e04e2e5235516c`.
The earlier `b`/`c` runs are not passes: newly deployed workers.dev routing first
returned a 404 and then an intermittent Cloudflare 1104. The successful run
waited for health and permitted retries only for these read-only routing checks;
it did not replay uncertain writes. Both earlier runs also cleaned their owned
resources.

Every diagnostic Worker and D1 database was observed absent after cleanup.
At **10:35:27.798 UTC**, the Cloudflare API additionally showed zero Durable
Object namespaces remaining for the feedback probe prefixes. The nine Eddy
production Worker names were still absent. This turn made **zero production
mutations**; the retained production release candidate is stale and must be
rebuilt and qualified after the remaining contracts are complete.

## Local validation

`PATH=<pinned Node 22>:$PATH bash deploy/cloudflare/ci/routes.sh` exited **0**.
The route inventory, route manifests and TypeScript checks passed. Core's full
suite passed **668 tests**, AI passed **150**, and the Worker suite passed
**955 tests in 117 files** (**1,773 total**), including the public Edge/admin boundary, service assertions,
scheduler and deletion registry. Fifteen new Core tests exercise actual HTTP
handlers with migrated SQLite, including microsecond ordering, all rating
surfaces, atomic rollback, legacy defaults, report caps, stale leases and erasure.
The successful gate log is private at
`~/.codex/eddy-production/feedback-routes-final-20260906-b.log`.

The first complete lane found two existing AI/Core cancellation tests missing
Core's newly generated model module. Their test-only source stage now uses the
same projector as the ordinary Core build; both cancellation guards still
execute the real Core routes and retain their original assertions. No AI
runtime dependency or model prompt was changed to make the tests pass.

The four protected default-prompt sources remain byte-identical to
`9b7e48dca2`: `backend/utils/llm/chat.py`, `backend/utils/retrieval/agentic.py`,
`backend/utils/retrieval/tools/memory_tools.py`, and `backend/fork/patches/llm.py`.

Full release readiness, native live-model transport, remaining route families,
production login and the signed Eddy macOS application's production end-to-end
flow remain required work. No push, pull request, merge or production publish
is claimed by this record.
