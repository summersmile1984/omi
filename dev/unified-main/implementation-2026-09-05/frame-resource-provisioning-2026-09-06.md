# Eddy frame storage provisioning and lifecycle reconciliation

The complete production candidate was prepared from `757e568442` and verified by
the official `release.mjs check` command. Candidate digest:
`d5e4b375df082ccdd71ebcae395ce6bf24503fdf06a76dd96f754bd1604f4edf`.
Its route/manifest, Worker/Core/AI, upstream/fork Web, client/builder, dual-target
Web builds, frozen SQL and nine Worker compile/dry-run stages passed. This proves
local candidate construction; `release_ready` remains false and CF-4/CI-1 are
still missing executable product qualification owners.

## Remote changes and actual observations

- At 07:40:40 UTC on 2026-09-06, a read found six Eddy production R2 buckets and
  no `eddy-cf-*` Workers. This list filter does not inspect the separately named
  `eddy-web-production`; full Worker absence requires the release schema owner.
- The official provisioning transaction created and read back
  `eddy-cf-frame-requests-temporary-production` at 07:41:43 UTC and
  `eddy-cf-frame-requests-production` at 07:41:46 UTC. Their creation responses
  and matching observations establish transaction ownership. Existing data
  resources were retained: **2 D1, 8 R2, 4 Queue and 7 Vectorize**, 21 in total.
- The temporary bucket's seven-day expiry rule was successfully created, but
  immediate validation failed because the API represents whole-bucket scope as
  `conditions: {}`. The original transaction remains `reconciliation_required`;
  its journal was not rewritten, replayed or marked successful by hand.
- Read-only reconciliation at 07:42:57 UTC confirmed the actual rule has
  `enabled: true`, `type: Age` and `maxAge: 604800`. The permanent bucket has
  only its default multipart-abort rule and no object-expiration rule.
- At 07:49:37 UTC, the corrected adapter read **all seven policies as present**:
  three Vectorize metadata indexes, three R2 object-expiration rules and the
  permanent frame bucket retention check. No additional mutation was needed.

## Boundary repair and tests

The locked Wrangler 4.127.0 writer builds an empty `conditions` object and only
sets `conditions.prefix` when a nonempty prefix was requested. The release
observer now interprets both `{}` and `{prefix: ""}` as whole-bucket scope. It
still rejects missing/null/array conditions, non-string or different prefixes,
unknown extra conditions, wrong retention periods and unowned expiration rules.
The shared R2 policy observation owner applies this interpretation to every
declared lifecycle policy, rather than adding a frame-specific retry exception.

This repair follows review of the recent release-adapter fixes: asynchronous
Vectorize policy observation (`1b40c4dca1`) and release process ownership
(`ff3610160d`). The regression executes the production adapter through its HTTP
response seam using the exact non-secret live R2 rule shape. It also proves that
omitted scope cannot satisfy a required nonempty prefix. No upstream source,
default prompt or product permission boundary was changed.
The fix declares the existing `FC-cloudflare-policy-observation-contract`;
its registry definition stays open and its existing adapter guard surface gains
this observed R2 case.

- Focused release adapter suite: **29 passed**.
- Full `deploy/cloudflare` `npm test`: **115 files / 932 passed**.
- Pinned Prettier 2.8.8 used on changed JavaScript; `git diff --check` passed.
- The actual Cloudflare policy read exercises the corrected production parser;
  the test is not the only evidence for the response interpretation.

The source repair changes candidate identity. A fresh preparation and new
provisioning journal must observe the now-existing resources and policies before
any later release; the old candidate cannot represent the repaired source.
No production Worker was published and no remote business SQL was applied here.

## Hosted image qualification is not yet proven

This section records the initial failed preview. The subsequent
[hosted runtime repair and verification](frame-image-hosted-runtime-2026-09-06.md)
proved full Core startup and the supported frame-image envelope; complete
product qualification remains pending.

A private `wrangler dev --remote` preview copied the frozen Core modules and
Python dependencies, imported the full Core app, and added an authenticated test
adapter invoking the unchanged image validator/canonicalizer. It had no D1, R2,
AI or production resource bindings. This separates a hosted image decode probe
from any claim about product data or public routing.

The initial preview uploaded successfully but returned 503 to `/health` until
its bounded readiness deadline. Its supervisor then closed the owned process
group. **No image case executed**, so this is neither a passing input-limit
qualification nor evidence that an image exceeded Worker memory. A separate
bounded diagnostic captures the readiness response before deciding the next
action. That diagnostic also returned 503 from Cloudflare (KIX), with an HTML
`Temporarily unavailable` page rather than a Core JSON health response. The
preview processes were closed; production bindings were not used. CF-4, CI-1,
supported hosted image limits and production macOS business
acceptance remain open.

Private evidence in `/Users/macstudio/.codex/eddy-production/`:
`candidate-production-20260906-frame-a/`,
`provision-production-20260906-frame-a/journal.json`,
`frame-resources-before-20260906.json`,
`frame-lifecycle-reconciliation-20260906.json`,
`frame-lifecycle-corrected-20260906.json`,
`frame-prefix-{adapter,worker}-tests-20260906.log`, and
`frame-canonicalizer-remote-20260906-a/{scope.json,result.json,runtime.log}`.
Probe authorization is synthetic and private; production model credentials were
not used in this preview.
