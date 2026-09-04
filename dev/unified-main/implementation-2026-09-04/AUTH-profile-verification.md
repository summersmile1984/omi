# Selected identity profile reads

The actual Server OS recording finalizer reached `process_conversation` but
`database.auth.get_user_name` still called Firebase and logged that a project ID
was required. Token verification and identity deletion had already selected
Better Auth. This is another consumer of the same identity ownership boundary
repaired by commits `a87bbe4dca` and `2965b29656`.

`fork/auth_identity.py` now validates a typed profile from the existing protected
`GET /internal/users/:uid` authority. The self-host registry replaces only
`database.auth._firebase_get_user`; previously captured public profile/name
functions therefore use the same authority. UID mismatch, malformed fields,
unknown responses and outages yield a sanitized authority error and shared
fallback telemetry before the existing optional-profile/default-name path.
An exact authoritative missing-user response returns absence. The upstream
profile keeps its original SDK call. No public auth route, schema or upstream
file changes.

The behavioral regression captures actual public consumer functions before
applying the actual registry and fails if they invoke Firebase. Before the fix,
11 new cases failed and the upstream-mode case passed. The repository's formal
file-isolated `backend/test.sh` now passes 29 profile/deletion cases and seven
HTTP/WebSocket consumer cases. The first combined pytest invocation exposed an
eager telemetry import introduced by this adapter, which registered metrics
outside the consumer fixture's lifetime. Importing the shared helper only in
the fallback branch repairs that issue: combined pytest now passes all 36
cases (`unit-lazy.log`). The earlier six errors remain in `unit.log`; they are
not successful evidence. No upstream tests or isolation helpers were changed.

The live probe executes these exact candidate modules in a separate process in
the existing Linux API test container. It captures real `database.auth`
consumers, then uses actual internal HTTP to the running Better Auth/PG service
for a synthetic account. Profile lookup and first-name lookup pass, an absent
synthetic identity returns None, and a trap records zero Firebase calls. The
serving process and its source mounts were not restarted or changed. This is
real authority/consumer evidence, not proof that the complete recording model
pipeline or a fresh standard image has finished qualification.

Commands and local logs:

- `BACKEND_UNIT_TEST_FILE_LIST=/tmp/memweft-implementation/auth-profile/test-files.txt bash backend/test.sh`
- `python3 /tmp/memweft-implementation/auth-profile/run-live.py`
- `/tmp/memweft-implementation/auth-profile/{before,unit,unit-lazy,formal-unit,live}.log`

The existing AUTH local/CI lane already discovers `test_auth_identity.py`, and
the startup lane discovers the HTTP/WebSocket consumer suite. The typed adapter
extends the shared selected identity primitive; no separate static checker is
introduced. It would have caught the observed recording-finalizer failure.
Failure class: `FC-split-mutation-authority` (existing canonical prevention and
merged examples #9365/#9597). Exact candidate checks and independent review are
recorded in the commit. No push, PR, merge, remote operation or release claim.
