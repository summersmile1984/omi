# Shared Web target builder

`build.ts` builds the current upstream Moonshine application for standard Server
OS (Bun 1.3.14) or Cloudflare Workers + Assets. Both use one source stage, the
same validated brand/profile resolver and CLIENT-1 source overlays. It never
writes to `web/app`, upstream tests or tracked lockfiles.

## Build and run

First run `make setup-backend` and `cd web/app && bun install --frozen-lockfile`.
Bun 1.3.14 matches the upstream Web Docker image. Use a validated private brand
manifest with explicit endpoints; `brand/omi-upstream` is a regression identity,
not a deployment template for another brand.

```bash
bun deploy/web/build.ts --target cloudflare --stage local \
  --manifest /private/brand.json --output /tmp/web-cloudflare-build
bun deploy/web/build.ts --target self_hosted --stage local \
  --manifest /private/brand.json --output /tmp/web-server-build
```

`--brand <id>` selects a checked-in manifest; `--manifest` uses the shared
loader for private YAML/JSON. `--stage` is local, beta or production. Output must
be a fresh empty directory outside the upstream source tree; symlink aliases
are resolved before copying or bundling. Dependencies come from the frozen Web
lock. The build does not create Cloudflare resources or deploy anything.

Only `artifact/` is the portable runtime payload. `source/` is the isolated
build workspace; it is not a deployable context. `build-manifest.json` records
the selected profile, source commit and dirty state, exact replacement hashes and all 26 current
routes. The Builder deliberately declares `release_ready: false` until joint
identity/realtime and brand qualification is recorded by the delivery owner.

```bash
# Server OS, with no adjacent repository or node_modules required:
cd /tmp/web-server-build/artifact
PORT=3000 bun start.js

# Optional Server OS container; build context contains only runtime artifacts:
docker build -f /path/to/repo/deploy/web/Dockerfile \
  -t local-web /tmp/web-server-build/artifact

# Cloudflare; use the repository's pinned Wrangler after npm ci in its directory:
/path/to/repo/deploy/cloudflare/node_modules/.bin/wrangler deploy --dry-run \
  --config /tmp/web-cloudflare-build/artifact/wrangler.json
/path/to/repo/deploy/cloudflare/node_modules/.bin/wrangler dev --local --port 8789 \
  --config /tmp/web-cloudflare-build/artifact/wrangler.json
```

Keep Web origin, Auth trusted origins and the selected local profile consistent
when choosing a port. Auth uses its explicit origin and session bearer; this
builder does not add an OAuth cookie proxy. Static assets and server-rendered
pages use the same upstream route/renderer code on both targets.

## Ownership and boundaries

- `profile_input.py` calls the existing `scripts/profiles/render.py` and brand
  manifest loader. `public-environment.ts` projects only public profile fields,
  API/WS bases and the brand display name. Environment secrets cannot enter the
  client banner. API/WS mount paths survive; MCP uses its independently specified
  origin/path through CLIENT-1's `mcpServerUrl()`.
- `source-stage.ts` applies CLIENT-1's `web/app/fork/overlays.json` full source
  paths, so relative and `@/` imports share one facade. Missing required auth
  overlays or changed upstream MCP/banner expressions fail the build. The
  complete staged production source is typechecked; untouched upstream tests
  run separately, since they assert the original identity implementation. `.env*`
  files and source symlinks are not copied. The MCP rewrite uses the TypeScript
  AST to replace exactly the known initializer and preserve `use client`.
- The original Moonshine compiler/assets scripts still own routes, layout
  discovery and generated application logic. Better Auth/webhook targets skip
  Firebase service-worker generation and omit its generated/template files.
  Webhook push transport is not implemented by this omission.
- `compile-worker.ts` rebuilds the original generated server module entry with
  workerd conditions; the original Bun target includes Bun-specific React
  code. Only the Bun server startup adapter is replaced by `worker-runtime.ts`.
  The app continues to own SSR, marketplace metadata, routes and response
  headers. Workers Assets replaces filesystem asset delivery. Build paths are
  canonicalized so `/tmp` and `/private/tmp` cannot duplicate the fetch owner.
- Server OS bundles the generated server into one Bun executable module and
  ships only that module plus public assets. The Dockerfile consumes this
  artifact; an image build is separate evidence from a successful Bun run.

`bash deploy/web/ci.sh` runs the same typecheck and three behavioral build-boundary
contract tests in the local and CI fork manifest. It covers public projection,
source/MCP drift, original-source preservation, path escape and symlink
counterexamples. It is not browser, Auth, WebSocket, D1 or production evidence.

The current release scripts under `deploy/cloudflare/scripts/` still reference
retired Next/vinext commands. The joint INTEGRATION-1 delivery must move their
qualification/publish/smoke contract to these artifacts before release; this
builder does not make those old commands usable. Full browser login, logout,
refresh/reconnect and first-frame WebSocket contracts are shared with AUTH-1,
CLIENT-1 and CF-2. Brand strings, image assets and links still need the complete
white-label qualification; a branded login form alone does not prove it.

## Local verification, 2026-09-04

The implementation worktree ran both final target builds using the same private
synthetic `web-proof` manifest and CLIENT-1 commit `7263d03f4f49`. Each staged
production typecheck and all 26 route builds passed. Standalone Bun and locked
Wrangler/workerd served `/`, `/login`, `/home`, marketplace list/detail, CSS and
client JavaScript with 200, unknown routes with 404, escaped synthetic marketplace
metadata and the original response security headers. A real Chromium session
loaded both login pages and switched the Cloudflare form to signup after hydration.
No actual Auth or ASR service is represented by this builder-only HTTP fixture.

Evidence is in `/tmp/memweft-implementation/cloudflare/`: `web-ci-final.log`
(typecheck + 3 tests / 20 assertions), `web-upstream-final.log` (unchanged Web types,
5 Moonshine smoke tests and 417 Vitest tests), `web-build-{cf,os}-final.log`
(staged production types + builds), `web-dryrun-final.log` (local dry-run, no
publish), `web-{cf,os}-http-final.log` and `web-{cf,os}-browser-final.log`, plus
`web-cf-hydration-final.log`. The fixture API serves only one synthetic marketplace
record at `/v2/apps` and `/v1/approved-apps`; it is not a product API qualification.
A Server OS Docker image build is not claimed here. The old release publisher
commands and complete white-label assets remain explicit follow-up work.
