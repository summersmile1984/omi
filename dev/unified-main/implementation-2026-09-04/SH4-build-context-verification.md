# Runtime image context excludes local validation state

Both fork-owned Docker context filters excluded `.venv` but missed the upstream
OpenAPI runner's `.openapi-venv` and `backend/_temp`. The local tree contained
581 MiB and 110 MiB in those directories. Actual runtime builds copied them
through `COPY backend/ .`; the subsequent ownership layer copied their data
again. Repeated builds contributed to the recorded host disk exhaustion.

Both filters now exclude these local-only directories. No upstream Dockerfile,
test, dependency lock or workflow was edited. The new contract runs two actual
offline scratch Docker builds using the real context filters, synthetic cache
files and two real runtime source witnesses. It relies on Docker's matching
behavior rather than introducing an ignore-file parser. Both variants failed
before the filter change and pass afterward. The same command is registered
in the existing Fork Checks local/CI manifest:

```sh
python3 deploy/self-host/ci/build_context.py
```

The unchanged upstream production Dockerfile with pinned Python 3.11.10 also
built `memweft-server-base:clean-context` for Linux AMD64. Its OCI index is
`sha256:dae2572b595b0db13d5f6296ca47da52dac52e315b31f42221364bb576b46d1c`.
An actual container with no network or source mount confirmed both excluded
paths were absent and the model contract/migration source hashes matched the
candidate. The preceding v5 build transferred 666.17 MB of context; this build
transferred 41.04 MB. These are observed build transfers, not a fixed artifact
size guarantee. This package does not claim another full product or model run.

Logs are under `/tmp/memweft-implementation/build-context/`: `red.log`,
`green.log`, `standard-build.log` and `standard-image.log`. Exact manifest
results are in the local commit message. Separately, cleanup selected 96 exact
cache IDs created by this work's self-host builds, checked that each remained
reclaimable and unshared, and removed only those cache records. Images and
container volumes were retained. The filtered inventory and command log are
under `/tmp/memweft-implementation/server/selected-owned-cache-cleanup.*`.
