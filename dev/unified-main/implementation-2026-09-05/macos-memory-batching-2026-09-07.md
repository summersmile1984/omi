# macOS Cloudflare memory import byte batching — 2026-09-07

The preceding `3eb6bd3eaa` change adopts the user's requested 1,000,000-byte
Cloudflare batch limit. The macOS importer previously grouped only by its
100-item count; a group of individually valid long memories could therefore
receive 413. This change plans the request bodies at the importer, where
per-batch outcome and retry state already live, rather than splitting a single
API call into hidden partial writes.

## Implementation

`NativeMemoryBatching` measures actual `MemoryBatchItem` JSON through
`OmiHTTPTransport.makeEncoder()`, including the `memories` envelope, metadata,
UTF-8 characters and escaping. It selects an ordered prefix within both count
and byte limits. An oversized individual item increments the failed tally while
its neighbors remain importable. All planning finishes before any network
writes. Cloudflare selects the 1 MB limit; Server OS retains count-only groups
of 100. No API wire fields, server-generated identities or default prompts change.

The fork stage replaces one compiler-located, SHA-256-reviewed
`OnboardingMemoryBatchImportService.save` declaration. Its remaining code keeps
the original per-batch success/failure counts, retry policy, cancellation and
account/session fences. In-tree native calls to `createMemoriesBatch` originate
from this importer; the generated SDK declaration is not an additional app
import owner. The upstream Swift files remain byte-identical to the checkout.

## Verification

- `PATH=<pinned-node-22-bin>:$PATH bash desktop/macos/fork/test.sh` passed two
  Node asset tests, 16 Swift tests (including four Unicode/escaping planner
  cases) and 18 stage tests. Private log:
  `macos-memory-batching-tests-final-20260907.log`.
- The stage test compiles and executes the actual staged import enum, current
  wire models and original encoder method. The controlled API verifies body
  sizes, ordered complete delivery, one rejected batch without replaying prior
  successful batches, oversized-item isolation, owner changes and both targets.
- The ordinary `ci_build.py --output
  /private/tmp/eddy-memory-batching-matrix-20260907-a --dependency-cache
  /private/tmp/eddy-macos-production-20260905-b/Desktop/.build` completed both
  full debug binaries. Server OS: 255,170,544 bytes; Cloudflare: 255,167,056 bytes.
  The planner source in both stages is byte-identical to the final source.
  Private log: `macos-memory-batching-build-20260907.log`.
- Pinned swift-format 602.0.0, pinned Python formatting and diff whitespace
  checks passed. The staged tests are part of the existing
  `fork-macos-native-identity` local/CI lane; the full matrix uses the existing
  `fork-macos-native-compile` lane.

## Actual native-to-Cloudflare HTTP integration

The private `macos-memory-hosted-20260907-a` run built and deployed fresh Auth,
Rate Limit, Core and Edge Workers with two isolated D1 databases and all
10 Auth / 175 App migrations. The native executable contains the staged import
function, actual wire models, transport encoder and final planner. Its URLSession
adapter supplied only the synthetic account's real Better Auth JWT and the
private test-deployment guard key; Edge/Core handled normal authentication and
business writes. No model calls, production bindings or user data were used.

The native importer saved 125 long Chinese/emoji/escaped-text memories in seven
HTTP requests: 992,164; 992,174; 992,174; 992,174; 992,174; 992,182; and 937,071
bytes. It verified the full returned content and 125 unique IDs. Independent
D1 observations confirmed 125 new items, operations, commits and usage sources,
250 kernel events and zero admission guards. Failed count was zero. The same
run also exercised actual single intake, owner export and the three existing
injected transaction rollback cases.

The run completed at `2026-09-07T03:11:38.250Z`. All four owned Workers and both
D1 databases were observed absent after cleanup. Journal SHA-256:
`e32b39d97c1a82ff20f8c77f7618da349b0b2a8362cac4760b581aa888c608d8`.
Source/binary hashes and private results are recorded under
`$CODEX_HOME/eddy-production/macos-memory-batching-evidence-20260907.json` and
`macos-memory-hosted-20260907-a/`. This is real native business code over HTTP,
not a UI onboarding or production-service acceptance claim.

## Refreshed signed macOS artifact

The ordinary `desktop/macos/fork/release.py` completed from implementation commit
`4654e8e14385907203069a0f04fd4e5411d0cc14` with Eddy's manifest, the
`cloudflare.production` profile, version `0.1.0` and build `2026090701`.
The local artifact is `/private/tmp/eddy-macos-production-20260907-a/Eddy.app`;
its sibling `Eddy-0.1.0-2026090701-macos.zip` contains the signed app.

`codesign --verify --deep --strict` passed. The normal bundle dependency audit
passed for all 19 Mach-O files. The packaged profile points to Eddy's production
Edge/Auth/Web endpoints; the staged planner is byte-identical to the committed
source. Main executable SHA-256:
`e636ef79dc7034715ae0ed018b2bd3e86ce46f69e2865257c7237e5eea7083ee`.
Archive, manifest and planner hashes are recorded in the private
`macos-memory-batching-artifact-20260907.json` evidence file.

The bundle requires macOS 26.0 because of its actual linked dependency floor.
It has a timestamped Developer ID signature but is not notarized. This refreshed
app has not been launched for UI acceptance, and its production services are
not deployed. The artifact correctly retains `service_verified: false` and
`release_ready: false`.

## Remaining delivery work

Electron's count-only importer still needs the same byte-aware adaptation.
Production Worker release, complete CF-4/CI-1 qualification, canonical writer
convergence and Eddy's production UI loop remain unfinished. The signed app and
HTTP integration evidence do not establish a deployed product.
