# Cloudflare brand/stage resource plans

`npm run resources` renders one non-secret deployment plan from the existing
brand/profile authority, a resource inventory, and the current Moonshine Web
artifact. It does not provision resources, upload secrets, deploy Workers, query
the Cloudflare account, or change DNS. Historical `deploy:staging` and
`deploy:production` publishers are not consumers yet; CF-5 owns that migration.

## Input and ownership

Start from [`fixtures/resource-inventory.example.json`](fixtures/resource-inventory.example.json).
Its account and database IDs are synthetic engineering fixtures. Replace them
with explicit, reviewed IDs for a real plan; format validation does not establish
that an ID exists remotely or belongs to the account.

| Field | Contract |
| --- | --- |
| `schema_version` | `1` |
| `brand`, `target`, `stage` | Must match the rendered brand/profile; target is `cloudflare`; stage is `local`, `beta`, or `production` |
| `account_id` | Explicit 32-character account ID |
| `routing` | `mode` is `local`, `custom_domains`, or `workers_dev`; `workers_subdomain` is explicit for workers.dev and otherwise null |
| `allocation` | `new` derives all names; `existing` requires every physical name in `existing_names` |
| `d1_ids` | Distinct explicit `auth` and `app` UUIDs |
| `secret_refs` | Every Worker maps its required secret bindings to environment variable **names**, never values |
| `existing_names` | Empty for a new allocation; complete `kind:role` mapping for an existing allocation |
| `migration_lineage` | Explicit account cutover lineage label; no inference from upstream's staging label |

New names derive from brand, stage and logical owner. Workers use
`<brand>-cf-<role>-<stage>` (Web uses `<brand>-web-<stage>`); storage follows the
same rule, with `-v1` on Vectorize names. D1, R2, Queue and Vectorize are separate
Cloudflare namespaces. The complete plan includes eight Workers, two D1 databases,
five R2 buckets, four queues, seven Vectorize indexes and two active Durable
Object namespaces: 28 resource entries. Account-managed AI/Images bindings are
listed separately in `platform_bindings`. Vectorize model/dimension metadata
comes from the existing namespace manifest; no embedding model is changed.

An existing allocation must list all 26 provisioned physical resource names;
the two Durable Object namespace identities derive from their Worker/class.
`resourceKeys()` in `scripts/resource-input.mjs` is the typed logical catalog.
No prefix substitution guesses existing names. Existing templates' legacy and
activation-fence policies are retained, and DO migration tags/class names are
preserved in both modes. New allocations disable the known staging-only legacy
bridges and use the profile's activation-fence capability. This is not an account
cutover or a declaration that imported accounts passed all migration gates.

The required secret mapping covers base identity, internal assertions, admin
boundaries and encryption keys in `REQUIRED_SECRETS`. All internal assertion
consumers must reference the same deployment-specific environment name; every
other credential has an independent reference. Optional model/payment/OAuth
provider credentials and their business flows remain separate qualification
work. This package neither reads environment values nor uploads these references.
A release consumer must resolve the named values securely and qualify enabled
provider capabilities before publishing.

## Profile and routing

`deploy/web/profile_input.py` is the common brand/profile renderer. The Web
artifact must match its brand, target, stage, full public profile and source HEAD.
The plan projects `auth_base_url` into Better Auth and product JWT issuer/audience;
MCP authorization-server issuer is `auth_base_url + /api/auth`, and the protected
resource is `mcp_base_url + /v1/mcp/sse`. Auth/Edge CORS comes from `web_base_url`.
The MCP issuer remains distinct from product JWT issuance.

For custom domains, Auth, Edge and Web have separate public origin owners.
API/MCP/share/objects may alias the same Edge origin. For workers.dev, the profile
must already match the derived Worker name and explicit account subdomain.
Remote origins require HTTPS with no custom port; local origins require loopback
hosts. API/MCP/share/object **mount paths currently fail qualification** (CF-4):
this package does not add path rewriting or silently discard a path. Supporting
mount paths later requires actual API, MCP OAuth redirect, share and object route
tests on one explicit prefix contract.

## Local workflow

From the repository root, using the frozen toolchain described in the parent
README and `deploy/web/README.md`:

```bash
npm --prefix deploy/cloudflare ci
npm --prefix deploy/cloudflare run python -- api-core sync
npm --prefix deploy/cloudflare run python -- api-ai sync
bun deploy/web/build.ts --target cloudflare --stage beta --manifest /path/brand.json --output /tmp/brand-web
npm --prefix deploy/cloudflare run resources -- --manifest /path/brand.json --inventory /path/resources.json --web-build /tmp/brand-web --output /tmp/brand-plan
npm --prefix deploy/cloudflare run resources -- --manifest /path/brand.json --inventory /path/resources.json --web-build /tmp/brand-web --output /tmp/brand-plan --check
```

Use `--brand <checked-in-brand-id>` instead of `--manifest` when appropriate.
Repeat `--compare /path/other/resource-plan.json` to reject cross-brand/stage
resource names, D1 IDs, public origin or secret-reference collisions before writing.
Nonempty output directories need a matching deployment identity marker; an output
owned by another brand/stage/account is rejected. `--check` compares without writing.
The selected output root, existing descendant directories and configuration files
must not be symbolic links, including dangling links. Validation finishes before
any output is rewritten. Only the declared Python module link leaves may point
to their exact source directory. System directory aliases above the selected
output root, such as macOS `/tmp`, remain usable. workers.dev requires a string
subdomain; JSON nulls, booleans and numbers are not coerced into account names.

The bundle contains `resource-plan.json`, eight `workers/<role>/wrangler.json`
files, two `migrations/<authority>.json` files, and `rollback-contract.json`.
Source paths, aliases, Web assets and migration directories are absolute paths
to the current checkout/build. Python configs also get owned `python_modules`
symlinks. It is a checkout-bound configuration bundle, not a portable archive;
regenerate after moving the checkout or Web artifact. Credentials and `.dev.vars`
are not copied. A dirty source checkout is explicitly recorded in the Web proof;
the plan never promotes it to a qualified release.

Run actual compilation separately for every role (these commands upload nothing):

```bash
node deploy/cloudflare/node_modules/wrangler/bin/wrangler.js deploy --dry-run --config /tmp/brand-plan/workers/auth/wrangler.json
npm --prefix deploy/cloudflare run python -- api-core deploy --dry-run --config /tmp/brand-plan/workers/api-core/wrangler.json
npm --prefix deploy/cloudflare run python -- api-ai deploy --dry-run --config /tmp/brand-plan/workers/api-ai/wrangler.json
```

Repeat the first command for `rate-limit`, `realtime`, `jobs`, `edge`, and `web`.
The Python entry retains its pinned tool/version/lock checks. An already-installed
1.16.7 tool override is documented in the parent README; it is not proof of a
successful fresh-machine install.

## Migration and rollback qualification

The plan carries exact, ordered filenames and SHA-256 hashes for both D1
authorities. All consumers use the same explicit IDs and the migration configs
are derived from those authorities. Every render executes the SQL in isolated
SQLite fixtures, retaining an existing user/session and task across the final
migration. Its in-memory ledger reentry is a SQL contract test, not evidence about
Wrangler's migration ledger or old Worker binary compatibility.

The real local D1 commands use the same bundle:

```bash
node deploy/cloudflare/node_modules/wrangler/bin/wrangler.js d1 migrations apply AUTH_DB --local --config /tmp/brand-plan/migrations/auth.json --persist-to /tmp/brand-state
node deploy/cloudflare/node_modules/wrangler/bin/wrangler.js d1 migrations apply APP_DB --local --config /tmp/brand-plan/migrations/app.json --persist-to /tmp/brand-state
```

Repeat both to verify no pending migrations. Use isolated synthetic rows for local
data ownership checks. No command here uses `--remote`.

`rollbackSnapshot(plan, observations)` accepts one explicitly observed 100% active
version per Worker, or null for no previous version. `rollbackActions` binds that
snapshot to the exact account/brand/stage/plan digest and migration authorities.
It orders Worker restoration in reverse dependency order, retains both D1 stores,
and emits `no_previous_version` instead of guessing a destructive cleanup.
Neither primitive executes a rollback. D1 migration reversal is never inferred;
CF-5 must prove older Worker/schema compatibility and execute an approved release
transaction before this becomes operational rollback support.

`release_ready` and `remote_state_verified` stay false; a successful render reports
`render_valid`, with `dry_run_verified: false`. Remote identity, credentials,
provider flows, complete product contracts and the release transaction need their
own evidence. The existing `fork-cloudflare-routes` local/CI lane discovers the
resource contract tests through `npm test`, including the production SQL verifier.
See [CF3 evidence](../../dev/unified-main/13-cf-resource-evidence.md) for the actual
commands, logs and limits of the current local qualification.
