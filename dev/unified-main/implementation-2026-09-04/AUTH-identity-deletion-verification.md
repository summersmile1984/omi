# AUTH identity deletion consumer verification

The SH2 provider review found that the worker still called Firebase through
`utils.other.endpoints.delete_account`. The self-host registry now binds that
same seam to Better Auth's internal DELETE plus residual proof. Session checks
and deletion share the existing trusted origin/secret validation. Final
provider completion independently checks identity absence before its receipt.
Upstream/Firebase mode is unchanged.

The wire owner and exact accepted responses are documented in
[the consumer contract](../../../contracts/auth/self-host-identity-deletion.md).
Adding identity proof to the existing provider completion test preserves its
previous provider-failure assertion and verifies that identity proof runs only
after provider absence. Expected counts come from the actual service's
`identityResiduals` queries, not an invented test response shape.

## Executed live path

Evidence is under `/tmp/memweft-implementation/auth/identity-deletion/`.
Both standard Dockerfiles built a new Linux AMD64 backend image, with source
directly inside the image and no source mounts. The image OCI index is
`sha256:5c2823fc13c41503f6d32fc6afd228805f773a15495ce50999f35766c3d82733`;
labels explicitly describe a local uncommitted candidate based on `822b9d4163`.
It is not an immutable full release.

An isolated project ran fresh PG16.4, the already verified AMD64 Node Auth
image, Redis7.4.2, Qdrant1.15.4 and the pinned MinIO image. Auth/PG/Qdrant
migrations completed and the standard API was healthy. Fixture ports are
34174/34175/34176 and PG55474. An initial MinIO host-port collision was resolved
by selecting unused ports; the process occupying the old port was untouched.

`live.py` executed the real worker and final completion wrappers, backed by
actual Auth, PG, Qdrant and MinIO. API requests verified JWT results over HTTP:

- Synthetic signup and protected API reads returned 200. Admitting the PG
  deletion intent changed the owner's protected response to 403.
- The real Auth DELETE completed, then the probe discarded that response and
  raised a transport timeout. The worker returned failure, kept a failed marker
  and retained provider data; the old JWT returned 401 because the real session
  was already gone. No successful receipt was published.
- Retry received actual user-not-found plus zero identity counts, purged the
  real vectors/objects and product PG rows, and published the minimal receipt.
  Another synthetic identity and its data remained usable. Repeated identity
  deletion succeeded idempotently. All five captured provider fences rejected
  new writes for the completed account.
- A separate still-live Auth identity blocked direct final completion even
  after product PG/provider rows were empty. Actual identity deletion then
  allowed completion; its old JWT returned 401.
- All synthetic identities, vectors, owned objects and control records were
  removed after the probe.

Billing, VM, Twilio, telemetry and unrelated derived-provider families were
controlled in this bounded probe. Their real erasure, direct PG writer fencing,
unknown provider-write outcomes and the full product deletion UI remain outside
this result. Auth/PG/MinIO/Qdrant calls were real; embedding inference was not
part of this package. The shared AUTH manifest runs the new hermetic regression
file, including live-user residuals, booleans/malformed counts, missing routes,
wrong credentials, timeout and legacy-profile selection.
