# Public screenshot API and scheduled erasure — 2026-09-08

Source base: `1bb71b6f26`, plus the Edge routes and quota in this commit.
The ordinary Auth, Rate Limit, Edge, Core and screenshot writer entrypoints
were freshly built and frozen. Core and the writer retain the Qwen behavior
verified in [the preceding run](screen-frame-qwen-2026-09-08.md).

## Public boundary

Edge now registers the seven authenticated screenshot operations, the public
shared-frame-set read and `/v1/screen-frame-content`. Owner operations use
verified sessions, account cutover admission and the existing method/path-bound
Core assertion. Forged caller identity headers do not select the owner.

Adjudication uses the original `screenshots:adjudicate` policy from
`backend/utils/rate_limit_config.py`: 30 requests per account per hour. Admission
occurs before request-body forwarding, including for replayed attempts. Shared
sets and image capabilities use a dedicated public forwarder that drops caller
credentials, claimed identity and cache validators while preserving the exact
URL. It streams Core's current response and privacy cache headers. Core and the
writer remain responsible for visibility and capability validation.

The 17 new Edge tests execute the real router. They cover all registered
operations, encoded path identities, exact body forwarding, forged headers,
missing/revoked sessions, cutover denial, exhausted quota and public 200/404
responses with no forwarded caller credentials. The tests run in the existing
`deploy/cloudflare/ci/routes.sh` Workers lane.

## Hosted execution

This run used public signup, opaque session restoration and JWT exchange for
two temporary users, with the ordinary Auth/Rate Limit/Edge/Core/writer Workers.
App D1 applied all 192 migrations through 0189; Auth D1 applied all ten.
Completed conversations and the two synthetic images were fixtures. No successful
frame, approval, verdict, attempt, quota receipt or cleanup result was seeded.
The writer had no public workers.dev route; its cron matched the ordinary
`*/5 * * * *` configuration.

| Exercised behavior | Observed result |
| --- | --- |
| Public signup/session/JWT | Both users signed in through the actual Auth Worker |
| Unauthenticated adjudication | 401 before Core/model work |
| Two-image adjudication | The meeting presentation was committed; the synthetic credential image was omitted |
| Workers AI usage | Two `@cf/qwen/qwen3.8-27b` calls; 4,344 input and 592 output tokens |
| Upstream quota and replay | Thirty accepted requests, consisting of initial adjudication plus 29 identical replays; request 31 returned 429; inference stayed at two calls |
| Owner versus another user with the same conversation ID | The second user's owner view was empty |
| Image and thumbnail through public Edge | 63,080 and 9,679 bytes; both hashes matched the write receipt |
| Anonymous sharing and share withdrawal | Shared image readable, then 404 after withdrawal; owner image remained readable |
| Global screenshot setting disable/enable | Old owner URL returned 404, then became readable again |
| Single-image and whole-set deletion | Empty results; old image URL immediately returned 404 |
| Actual five-minute scheduler | Receipt changed from `cleanup` to `deleted`; both upload handles were cleared without an operator image deletion |
| Public logout | Old JWT received 401; the other user could still read the default-enabled screenshot setting |

There were **56 HTTP/image assertions**. Automatic erasure was observed at
**2026-09-08T03:25:02.776Z**; the public business run passed at
**03:25:05.087Z**. The deletion owner awaits both multipart aborts and R2 deletion
before setting `deleted`. No cleanup endpoint was manually invoked, no receipt
was patched, and no object-delete CLI ran. The recovery later confirmed that
state and removed the empty disposable bucket without manual object removal.

The stored full image hash was
`689d02bf6d1767e385d865cf2e71c17b32da084d048f00032ce491b9075676f1`,
matching the visually inspected meeting fixture from the preceding hosted run.
The public contract omits per-candidate rejection reasons; omission is not a
claim about the specific negative verdict or comprehensive privacy-model quality.
The default prompt and selection rules remain unchanged.

The terminal driver exited 1 after the successful run: its post-delete Edge
Worker observation encountered HTTP 404. After confirming that process was
terminal, recovery re-observed Edge as absent, matched all remaining versions,
tags and resource IDs, and finished teardown. No code or model call was rerun.
All five Workers, both D1 databases, the Queue and R2 bucket were observed absent
by **2026-09-08T03:27:16.781Z**. Recovery exited 0, and all five temporary secret
files were removed.

## Verification and archive

- Node 22 `node_modules/vitest/vitest.mjs run tests/screen-frame-edge.test.ts` — **17 passed**, 0.268 seconds.
- Full Workers: Node 22 `node_modules/vitest/vitest.mjs run` — **1049 passed**, 129 files, 18.47 seconds.
- Node 22 `node_modules/typescript/bin/tsc --noEmit`, `scripts/validate-manifests.mjs` and `git diff --check` passed.
- Core runtime did not change in this commit. Its preceding full suite had **1193 passed**; this hosted run executes that same runtime behind the new Edge boundary.
- Official [Cron Trigger documentation](https://developers.cloudflare.com/workers/configuration/cron-triggers/) explains the UTC cadence and propagation interval; this run waited for the actual scheduler.

Retained evidence:
`/Users/macstudio/.codex/eddy-production/screen-frame-public-hosted-20260908/`.
The archive includes the driver, frozen module/config trees, migrations,
fixtures, received images, business journal, terminal teardown observation,
recovery and test logs. `archive-sha256.json` identifies retained files.

## Remaining delivery scope

The eight screenshot inventory slots remain blocked until the full supported
input envelope, native capture, queued account erasure and the common two-target
contract are qualified. This run proves ordinary image deletion by the real
scheduler; it does not prove an entire account-deletion job. The seeded meeting
was not produced by actual desktop recording.

The large-input boundary needs transport work. Near 20 MiB per candidate,
eight base64 images alone exceed 200 MiB. Cloudflare documents a 128 MB isolate
memory budget and account-plan request body limits in its
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/).
The current native `MeetingFrameJudge` constructs one JSON request with up to
eight images and no aggregate byte budget. Normal two-image success cannot
qualify that maximum or justify silently dropping selected frames. No smaller
supported-input contract was substituted in this commit.

Full CF-4, the other outstanding product families and production/native release
qualification remain unfinished. `release_qualified` remains false.
