# Common core product contract

The same public HTTP suite now exercises disposable Server OS and Cloudflare
targets. It covers eight identity/onboarding/Tasks cases and always records
`release_qualified: false`. This is the core slice of CI-1; recording, models,
restart, full wire errors, native platforms and release qualification remain
separate evidence.

`contracts/deployment/core.py` imports no application handlers, seeds no
database records and uses no authentication bypass. Its two accounts are created
by public signup, restored using opaque sessions and exchanged for JWTs. It
checks anonymous/invalid-token denial, repeated onboarding reads and persisted
updates, Tasks create/read/complete, cross-account denial, invalid-description
422, refresh and logout revocation. The 422 expectation is the production
FastAPI `ActionItemCreateRequest` contract. The first real CF run returned 400
and failed; the typed validation repair is integrated as `1272c0e2c1`.

The Server runner builds the unchanged standard backend Dockerfile and real
Auth image, renders an explicit core-only profile, and starts fresh normal
Compose services and migrations. Auth, PG, Redis, MinIO, Qdrant, FastAPI and
the four queue consumers are real. Speech is explicitly disabled. The
embedding HTTP provider is controlled; no inference quality or complete
production-profile claim follows from this test. Application containers use
an internal Docker network, with a fixed Auth/API loopback ingress. A random
Compose project owns its containers, volumes and networks.

## Recorded local execution

- `server-core-third-run.log`: eight cases passed through a freshly built
  standard runtime. Earlier fixture attempts exposed unreadable profile-file
  permissions and Docker Desktop internal-network ingress; both were fixed
  in the fixture before accepting this result.
- `server-core-final.log`: an attempted formal run failed while the local
  Docker VM stopped responding. It is retained as a failed run. The scoped
  `down` subsequently removed project `memweft-contract-121fd49ae079`; no
  production containers or unrelated volumes were selected. Stopping two
  completed, owned model fixtures restored Docker responsiveness without a
  daemon restart. Resource exhaustion was suspected, not proved by an OOM log.
- `server-core-final-recovery.log`: the formal `product.sh` entry passed all
  eight cases and automatically removed its fresh project. It reused standard
  runtime image `sha256:49d83f0f207d5699d8d962279ea509ebfe8515f849097795261c4ab8bbf86140`
  only after comparing every tracked backend Python file with the actual
  image bytes; fresh Auth/profile image layers were still built. This is
  source-byte cache admission, not dependency or release attestation.
- Independent review then found that the HTTP suite child bypassed the
  fixture's cancellation owner. `run_core` now uses the same process group,
  deadline and teardown path. `fixture-ownership-reviewed.log` records three
  passing tests, including a real `core.py` signup stalled on a local socket
  and terminated by cancellation. Existing-output ownership and descendant
  termination also pass.
- Cloudflare's actual local seven-Worker target passed the identical eight
  cases after the typed Tasks repair. Evidence is under
  `cloudflare/task-validation/live-common.log` and `live/`; the target also
  contained separately developing CF-4 recording code, so this is not an
  exact standalone Tasks-commit or full release qualification.

Logs above are under `/tmp/memweft-implementation/ci/`, unless a Cloudflare
path is given. Redacted HTTP traces, scope, source hashes and recovered results
are copied to `ci/server-core-recovered-evidence/`. Private fixture command
logs contain synthetic disposable credentials and are not committed.

The existing fork manifest runs `bash deploy/self-host/ci/product.sh` in both
local and CI lanes on relevant product, adapter and target paths. It exercises
existing owners rather than introducing another authorization or state policy.
The real onboarding admission failure and Server422/CF400 divergence are the
instances this shared guard would have caught. No remote Actions execution,
push, merge, signing or deployment is claimed.
