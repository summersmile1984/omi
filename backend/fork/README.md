# Fork runtime ownership

`python -m fork.migrate migrate` is the only schema-changing startup process.
It requires an explicit `FIRESTORE_PG_DSN`; `check` performs read-only admission.
Schema v3 registers frame requests/keyframe jobs and chat-first dead letters;
v1/v2 physical table mappings remain immutable.

`uvicorn fork.main:app` calls `bootstrap(Role.API)` before importing upstream's
`main.app`. `python -m fork.worker` calls `bootstrap(Role.WORKER)` without loading
ASGI/model modules, validates all four queue destinations and credentials, and
supervises one process per queue. Any unexpectedly completed child fails the
supervisor. `--check` validates admission and Redis connectivity without consuming.

`profile.py` reads the image's generated `deployment_profiles.generated.json`.
Build with `render.py --target self_hosted --manifest ... --stage ... --emit-json`;
select an exact row with `OMI_DEPLOYMENT_PROFILE=self_hosted.production` (or
`self_hosted.beta`/`self_hosted.local`). Conflicting target, brand or adapter
settings fail before workload import. `bootstrap.py` projects this choice into
the environment switches still consumed by upstream seams and checks the PG
schema. Omi-cloud API mode applies no patches or self-host configuration.

Do not use `sitecustomize` for admission: Python can continue after its import
fails. New process types must call the same explicit bootstrap before importing
or executing their workload. Tests execute real subprocess entrypoints, including
bad profile/dependency/patch cases; do not substitute source-order assertions.

`queue_config.py` owns the four handler paths and separate credentials. Producer
and authentication patches live in `patches/queue.py`; a worker receives only its
queue's selected credential through the internal adapter setting. This package
also owns MinIO/storage and speaker provider patches. It does not claim the
remaining model, push or vector adapters are complete; refer to the dated audit.

Run fork tests through `backend/test.sh` with an explicit file list; the fork
manifest runs startup and source-closure contracts in both local and CI lanes.
Run live PostgreSQL tests only against a disposable target and record their
results separately from the hermetic lane.
