# Shared deployment product contracts

`core.py` executes the same HTTP cases against either real target: two public
signups, opaque session restore and JWT exchange, protected admission,
onboarding persistence, task creation/completion, cross-account denial, refresh
and logout revocation. It imports no backend handlers and seeds no business
records. The task request contract comes from the actual FastAPI
`ActionItemCreateRequest`: an empty description returns **422**. Cloudflare's
observed 400 is a failing divergence, not an accepted alternative.

Run an already-owned disposable target with its runner-generated metadata:

```bash
python3 contracts/deployment/core.py --metadata /absolute/fixture/metadata.json
```

Metadata contains `api_origin`, `auth_origin`, `target` (`self_hosted` or
`cloudflare`), `brand_id`, and `trace_dir`. Origins must be explicit loopback
HTTP origins. The client follows no redirects, retains no cookie jar, writes
only route/status/timing traces, and never writes credentials or response
bodies. Every run needs a fresh trace directory; it reports each case and exits
nonzero on failure. Target runners own the disposable accounts and state.

The Server runner is `deploy/self-host/ci/product.py`; its local/CI command is
`bash deploy/self-host/ci/product.sh`. It builds the unchanged upstream Dockerfile
and the actual Auth image, uses the same Compose service declarations and
forward-only migration commands, and starts actual Auth/PG/Redis/MinIO/Qdrant,
API and four queue consumers. Application containers use an internal-only
Docker network; a fixed two-port HTTP proxy exposes Auth/API on loopback. A
fresh random Compose project owns all state and removes its own containers,
volumes and networks on exit. No production container is selected or reused.

The core slice explicitly generates a **speech-disabled test profile** from the
real profile renderer and validates it through the normal capability owner.
An isolated HTTP embedding fixture returns deterministic vectors through the
real model-identity/dimension adapter. This is controlled inference for product
state tests; it is not model quality, speech, external egress, or production
configuration evidence. LLM routes on this fixture remain unavailable.

For local iteration, `SELF_HOST_CI_RUNTIME_IMAGE` may name a previously built
standard runtime image. The runner hashes every tracked backend Python source
inside that actual image and refuses missing or different bytes before serving.
It still builds the fresh Auth/profile layers. Omit the variable for a standard
build. `SELF_HOST_CI_PORT` changes the loopback port group (default 34800).
Private command logs, profile hash, source hashes and `core-results.json` remain
under the printed trace directory. They contain only disposable fixture state.

This is the identity/onboarding/tasks slice of **CI-1**. It deliberately does
not implement `qualify-dual-target.mjs`, which owns the complete candidate and
platform/brand product qualification. Recording finalization, conversation and
memory retrieval, process restart, full error shapes, client UI, supported OSes
and actual releases require their corresponding evidence. A core result always
sets `release_qualified` to false.
