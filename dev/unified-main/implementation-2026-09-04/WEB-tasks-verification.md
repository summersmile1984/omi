# Dual-target Web Tasks verification

Both targets now have actual browser evidence for signup, session restoration,
task creation, completion and persistence. This is a bounded Tasks contract,
not qualification of the complete conversation, memory, voice or white-label
product. All accounts, tasks, credentials and services used here are synthetic.

## Server OS

The formal `deploy/web/build.ts` built all 26 routes from candidate
`1be55bf3bd`, selecting `self_hosted.local`, explicit fixture endpoints and
brand `fixture-server-tasks`. Its portable artifact ran under pinned Bun 1.3.14
on port 33059. It used the new standard Linux ARM64 Auth image on 33067,
PostgreSQL 16.4 on 55467, and the standard Linux AMD64 backend image
`memweft-server:sh2` on 33068, all in the root-owned
`memweft-pg-upgrade-20260904` project with its own pinned Redis 7.4.2 service.

The backend image labels remain unattributed from its engineering build;
32 production files under fork/firestore_pg plus auth_shim were independently
hashed against root candidate source and all matched. The generated server
profile is the image's explicit self-host local profile. This establishes the
exercised implementation, not an immutable release or complete image lineage.

CUA's in-app Chromium ran the real UI. Network observations used CDP only to
read request/response evidence; no page state or HTTP implementation was replaced.

| Real UI operation | Result |
| --- | --- |
| Create synthetic account and enter Tasks | Auth success; actual task list GET 200 |
| Add `Server persistence fixture 1788515411741` | POST `/v1/action-items` 200, ID `10ce10663ad640608d6d43090bae5315` |
| Full page reload, select List | GET 200 and same task visible |
| Click completion control | PATCH `/v1/action-items/<id>/completed?completed=true` 200, response `status=completed`, `completed=true` |
| Full reload | Completed(1), zero pending, GET 200 |
| Restart only the dedicated backend, reload again | Same ID and completed state returned from PG, GET 200 |
| Sign Out through real sidebar | Returned to Server Fixture sign-in form |

`server-cross-user.py` then used an independently created Auth identity:
list 200 excluded the first user's ID; item GET, description PATCH and completion
PATCH all returned 404. Anonymous list returned 401; empty create body returned
422 under the real upstream FastAPI/Pydantic contract. The second identity's
user/session/account residuals were zero after cleanup. The browser's completed
synthetic task is retained only in this disposable PG fixture. The temporary
Bun server and tab were closed after verification.

Evidence directory: `/tmp/memweft-implementation/auth/pg-upgrade/` contains
`server-web-build.log`, `server-cross-user.py`, `server-cross-user.log`,
`server-restart.log` and `backend-source-comparison.json`. Private Compose and
brand inputs are separate from the portable artifact. Fixture setup initially
used the wrong PG driver and omitted required Redis authentication; corrected
private configuration and the exact checked-in Redis image produced a healthy
API. The early Tasks500 was observed while that backend was not running, not
reported as a passing request or fixed by changing product code.

## Cloudflare

The independent white-label and Cloudflare agents exercised the same production
Web UI on their Core/Edge/Auth/D1 fixtures. Task
`2984eef1a166421fb37395797e8df0ae` was created, shown after full reload, completed,
and restored after reload. An independent identity could not list/read/change
it; GET/PATCH/completion returned404. The Cloudflare agent's own synthetic item
survived a real Core workerd restart as completed, then was deleted with204 and
subsequently returned404. Anonymous list returned401.

Evidence is recorded in `/tmp/memweft-implementation/whitelabel/CLIENT1-EVIDENCE.md`
and `client1-cf-api-{created,reloaded,completed,completed-reload}.log`; Core/Edge
negative and restart proofs are under `/tmp/memweft-implementation/cloudflare/`.
Core recovered using the official Pyodide disk cache; the formal pinned Python
launcher package is still being finalized separately.

## Remaining boundaries

Empty create body currently differs: Server422 versus Cloudflare400. This is
explicit protocol-parity debt for CI-1/CF-4, despite both rejecting the input.
Real STT quality, conversation/memory persistence, provider deletion/write
fencing, cloud egress controls and full dual-target failure parity remain.
The built pages still expose upstream Omi titles, artwork, onboarding and links;
WL-5 must replace and verify them before white-label release. A fixture brand
on the sign-in page does not qualify the entire application.
