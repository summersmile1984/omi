# Encoded request assertion paths — 2026-09-07

The hosted canonical-review run A received 401 for a valid authenticated request
whose review ID contained percent-encoded colons. Edge signs `URL.pathname`;
Python's composition middleware compared that signature with `request.url.path`,
which ASGI has already decoded. The request never reached owner isolation or
review business logic. All four owned Workers and both D1 databases from the
failed run were subsequently observed absent.

Both Python middleware owners now use `python/shared/assertion_path.py` to read
the original ASCII `raw_path`. All six additional Python assertion consumers
use the same extractor. Missing/malformed raw bytes fail closed; the internal
verifier also rejects absent/non-path values. There is no decode-and-compare
alternative. Authority, audience, method, signature and expiry checks retain
their original contracts. Existing plain-path clients still authenticate.

The boundary follows the [ASGI HTTP scope specification](https://asgi.readthedocs.io/en/latest/specs/www.html#http-connection-scope)
and the [Cloudflare Workers SDK request-to-scope implementation](https://github.com/cloudflare/workers-py/blob/main/packages/runtime-sdk/src/asgi.py).
Each component's existing entry test executes its production middleware with
encoded review IDs, Unicode/spaces, double encoding, plain paths, a changed
representation, another path and missing raw bytes. Core uses TestClient HTTP;
AI uses actual ASGI Request objects and its production middleware, avoiding a
new HTTPX dependency. The public-share request fixture now includes the raw
path that the actual Workers adapter supplies.

Hosted run B rebuilt actual Auth, Rate Limit, Python Core and Edge. Its encoded
review resolution first returned the correct 404 for another account, then
completed the owner's actual business operation. Subsequent encoded HTTP calls
covered acceptance, correction, rejection, timeout/drop, full rollback and
cleanup retry. The shared extractor bytes and Core composition module match the
final source (AST for the composition module). Later migration of the six
secondary consumers and their additional malformed-path verifier check was
covered locally, not claimed as a hosted test of those separate endpoints.

B completed at `2026-09-07T08:02:24.421Z`, with all four Workers and both D1s
observed absent. Journal SHA-256:
`5008bb25c12e5a46588dbc39c47b18b6a575cb2b7fadc55ce87d287092a5e64f`.
Private evidence is under `$CODEX_HOME/eddy-production/memory-review-*`.

The existing Worker lane passed 995 tests. The final Core suite passed 915
cases and AI passed 151. Commands were `uvx uv==0.12.3 run pytest -q --tb=short`
in each component (the Core final invocation supplied its explicit project and
test directory from the repository root). The focused consumer run passed 89 cases across private review/privacy/share/feedback/screen
and public-share consumers. No provider inference, default-prompt change,
Server OS change or production release was part of this repair.

`FC-signed-path-decoded-before-verification` records the failed contract and
shared prevention surface. Its regression is in existing component runners,
not a standalone guard. The real incident is hosted run A above; no external
PR number is fabricated. The fix declares `Failure-Class: new` and adds exactly
one definition without changing another class's lifecycle.
