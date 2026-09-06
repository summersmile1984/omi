import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import YAML from "yaml";
import {
  assertDisjointPlans,
  REQUIRED_SECRETS,
  resourceKeys,
  WORKERS,
} from "../scripts/resource-input.mjs";
import {
  parseJsonc,
  readWorkerTemplates,
  renderResourcePlan,
} from "../scripts/resource-configs.mjs";
import {
  materializeResourceBundle,
  rollbackActions,
  rollbackSnapshot,
} from "../scripts/resource-bundle.mjs";

const root = resolve(import.meta.dirname, "..");
const sourceCommit = "a".repeat(40);
const temporary = [];
function temporaryDirectory() {
  const path = mkdtempSync(resolve(tmpdir(), "cf-resource-contract-"));
  temporary.push(path);
  return path;
}
afterEach(() =>
  temporary
    .splice(0)
    .forEach((path) => rmSync(path, { recursive: true, force: true }))
);
function resourceFixture(brand = "alpha", stage = "beta", index = 1) {
  const profile = {
    name: `cloudflare.${stage}`,
    target: "cloudflare",
    stage,
    identity_provider: "better_auth",
    api_base_url: `https://api.${brand}-${stage}.invalid/`,
    auth_base_url: `https://auth.${brand}-${stage}.invalid`,
    web_base_url: `https://web.${brand}-${stage}.invalid`,
    mcp_base_url: `https://mcp.${brand}-${stage}.invalid`,
    share_base_url: `https://share.${brand}-${stage}.invalid`,
    objects_base_url: `https://objects.${brand}-${stage}.invalid`,
    auth_callback_scheme: `${brand}-${stage}`,
    capabilities: {
      account_activation_fence: false,
      embedding_dims: 1536,
      allow_direct_model_providers: false,
    },
  };
  if (stage === "local")
    for (const [name, port] of Object.entries({
      api: 8787,
      auth: 8788,
      web: 8789,
      mcp: 8787,
      share: 8789,
      objects: 8787,
    }))
      profile[`${name}_base_url`] = `http://127.0.0.1:${port}`;
  const input = {
    schema_version: 1,
    brand,
    target: "cloudflare",
    stage,
    account_id: "a".repeat(32),
    routing: {
      mode: stage === "local" ? "local" : "custom_domains",
      workers_subdomain: null,
    },
    allocation: "new",
    d1_ids: {
      auth: `${String(index).padStart(8, "0")}-1111-4111-8111-111111111111`,
      app: `${String(index).padStart(8, "0")}-2222-4222-8222-222222222222`,
    },
    secret_refs: Object.fromEntries(
      WORKERS.map((role) => [
        role,
        Object.fromEntries(
          REQUIRED_SECRETS[role].map((binding) => [
            binding,
            `${brand}_${stage}_${
              binding === "INTERNAL_ASSERTION_SECRET"
                ? "SHARED_INTERNAL"
                : binding === "SCREEN_FRAME_SIGNING_SECRET"
                ? "SHARED_SCREEN_FRAME"
                : role + "_" + binding
            }`
              .toUpperCase()
              .replaceAll("-", "_"),
          ])
        ),
      ])
    ),
    existing_names: {},
    migration_lineage: `${brand}-${stage}-native-v1`,
  };
  return {
    root,
    input,
    projected: {
      brand_id: brand,
      product_name: `${brand} fixture`,
      support_email: `support@${brand}.example.invalid`,
      brand_runtime: {
        brand_id: brand,
        display_name: `${brand} fixture`,
        ai_persona_name: "Mira",
      },
      firmware_policy: {
        schema_version: 1,
        brand_id: brand,
        device_model: `${brand} CV1`,
        device_model_aliases: ["nrf5340"],
        release_tag_prefix: `${brand}_CV1_v`,
        release_asset_prefix: `${brand}_CV1_OTA_v`,
        github_releases_url: `https://api.github.com/repos/${brand}/firmware/releases`,
      },
      profile,
    },
    sourceCommit,
    web: {
      config: {
        name: `${brand}-web-${stage}`,
        main: "cloudflare/worker.js",
        compatibility_date: "2026-08-27",
        compatibility_flags: ["nodejs_compat"],
        workers_dev: false,
        assets: {
          directory: "public",
          binding: "ASSETS",
          run_worker_first: true,
        },
      },
      manifest: {
        brand,
        target: "cloudflare",
        stage,
        source_commit: sourceCommit,
        source_dirty: true,
        entry: "wrangler.json",
        routes: ["/", "/login"],
        profile: structuredClone(profile),
        pending_qualification: ["fixture is not a built Web artifact"],
      },
    },
  };
}
function render(fixture = resourceFixture()) {
  return renderResourcePlan(fixture);
}
function changedProfile(fixture, key, value) {
  fixture.projected.profile[key] = value;
  fixture.web.manifest.profile[key] = value;
}

describe("one brand/stage Cloudflare resource authority", () => {
  it("consumes actual brand/profile rendering for every stage and the documented inventory example", () => {
    const manifest = YAML.parse(
      readFileSync(
        resolve(root, "../../brand/omi-upstream/manifest.yaml"),
        "utf8"
      )
    );
    manifest.brand.id = "cf-alpha";
    manifest.brand.display_name = "Atlas 中文";
    manifest.brand.ai_persona_name = "Mira";
    manifest.brand.support_email = "help@atlas.example.invalid";
    manifest.identifiers.url_scheme = "cf-alpha";
    manifest.deployments = { cloudflare: {} };
    for (const stage of ["local", "beta", "production"]) {
      const fixture = resourceFixture("cf-alpha", stage);
      manifest.deployments.cloudflare[stage] = Object.fromEntries(
        Object.entries({
          api_base: "api",
          auth_base: "auth",
          web_app: "web",
          mcp_base: "mcp",
          share_base: "share",
          objects_base: "objects",
        }).map(([field, owner]) => [
          field,
          fixture.projected.profile[`${owner}_base_url`],
        ])
      );
      const path = resolve(temporaryDirectory(), "brand.json");
      writeFileSync(path, JSON.stringify(manifest));
      const result = spawnSync(
        process.env.PYTHON ?? "python3",
        [
          resolve(root, "../web/profile_input.py"),
          "--target",
          "cloudflare",
          "--stage",
          stage,
          "--manifest",
          path,
        ],
        { encoding: "utf8" }
      );
      expect(result.stderr).toBe("");
      expect(result.status).toBe(0);
      fixture.projected = JSON.parse(result.stdout);
      fixture.web.manifest.profile = structuredClone(fixture.projected.profile);
      if (stage === "beta")
        fixture.input = JSON.parse(
          readFileSync(
            resolve(root, "fixtures/resource-inventory.example.json"),
            "utf8"
          )
        );
      const plan = render(fixture);
      expect(plan.configs["api-core"].config.vars.BRAND_SUPPORT_EMAIL).toBe(
        manifest.brand.support_email
      );
      for (const role of ["api-core", "api-ai"])
        expect(
          JSON.parse(plan.configs[role].config.vars.BRAND_RUNTIME_JSON)
        ).toEqual({
          brand_id: "cf-alpha",
          display_name: "Atlas 中文",
          ai_persona_name: "Mira",
        });
      expect(
        JSON.parse(
          plan.configs["api-core"].config.vars.FIRMWARE_BRAND_POLICY_JSON
        )
      ).toEqual(fixture.projected.firmware_policy);
      expect(plan.configs.auth.config.vars.AUTH_JWT_ISSUER).toBe(
        fixture.projected.profile.auth_base_url
      );
      expect(plan.configs.edge.config.vars.MCP_AUTHORIZATION_SERVER_URL).toBe(
        `${fixture.projected.profile.auth_base_url}/api/auth`
      );
      expect(plan.configs.auth.config.vars.MCP_RESOURCE_URL).toBe(
        `${fixture.projected.profile.mcp_base_url}/v1/mcp/sse`
      );
      expect(plan.configs.auth.config.vars.ALLOWED_ORIGINS).toBe(
        [
          fixture.projected.profile.web_base_url,
          fixture.projected.profile.share_base_url,
        ]
          .filter((value, index, values) => values.indexOf(value) === index)
          .join(",")
      );
      expect(plan.configs["api-core"].config.vars.PUBLIC_SHARE_BASE_URL).toBe(
        fixture.projected.profile.share_base_url
      );
    }
  });

  it("rejects absent, malformed and cross-brand runtime projections before rendering", () => {
    const good = resourceFixture();
    for (const invalid of [
      undefined,
      null,
      {},
      { ...good.projected.brand_runtime, brand_id: "foreign" },
      { ...good.projected.brand_runtime, display_name: true },
      { ...good.projected.brand_runtime, ai_persona_name: " \t" },
      { ...good.projected.brand_runtime, ai_persona_name: "Mira\nInjected" },
      { ...good.projected.brand_runtime, extra: "owner" },
    ]) {
      const fixture = resourceFixture();
      fixture.projected.brand_runtime = invalid;
      expect(() => render(fixture)).toThrow(/brand runtime/);
    }
  });

  it("rejects a Web product display name from a second naming authority", () => {
    const fixture = resourceFixture();
    fixture.projected.product_name = "Other product";
    expect(() => render(fixture)).toThrow("Web product identity differ");
  });

  it("rejects a firmware policy that does not belong to the rendered brand", () => {
    const fixture = resourceFixture();
    fixture.projected.firmware_policy = {
      ...fixture.projected.firmware_policy,
      brand_id: "foreign",
    };
    expect(() => render(fixture)).toThrow(/firmware policy/);
  });

  it("requires a plain support address in the same manifest projection", () => {
    for (const value of [
      undefined,
      null,
      true,
      "",
      "no-address",
      "help@example.invalid\nInjected",
      "Two People <a@example.invalid>",
      "a@[127.0.0.1]",
      "a,b@example.invalid",
    ]) {
      const fixture = resourceFixture();
      fixture.projected.support_email = value;
      expect(() => render(fixture)).toThrow(/support contact/);
    }
  });

  it("keeps direct Wrangler development identity aligned with its actual manifest", () => {
    const manifest = YAML.parse(
      readFileSync(
        resolve(root, "../../brand/omi-upstream/manifest.yaml"),
        "utf8"
      )
    );
    const { id, display_name, ai_persona_name } = manifest.brand;
    const templates = readWorkerTemplates(root);
    expect(templates["api-core"].config.vars.BRAND_SUPPORT_EMAIL).toBe(
      manifest.brand.support_email
    );
    for (const role of ["api-core", "api-ai"])
      expect(
        JSON.parse(templates[role].config.vars.BRAND_RUNTIME_JSON)
      ).toEqual({
        brand_id: id,
        display_name,
        ai_persona_name,
      });
  });

  it("renders real templates across all stages with auth, MCP, CORS and dependency ownership", () => {
    for (const stage of ["local", "beta", "production"]) {
      const fixture = resourceFixture("alpha", stage),
        plan = render(fixture);
      expect(plan.resources).toHaveLength(30);
      expect(
        plan.resources.find((resource) => resource.key === "r2:screen-frames")
          .owners
      ).toEqual(["screen-frame-writer"]);
      expect(plan.configs["screen-frame-writer"].config.ai).toBeUndefined();
      expect(plan.configs["screen-frame-writer"].config.services ?? []).toEqual(
        []
      );
      expect(plan.configs.auth.config.vars).toMatchObject({
        BETTER_AUTH_URL: plan.origins.auth,
        AUTH_JWT_ISSUER: plan.origins.auth,
        AUTH_JWT_AUDIENCE: plan.origins.auth,
        ALLOWED_ORIGINS: [
          ...new Set([plan.origins.web, plan.origins.share]),
        ].join(","),
        MCP_RESOURCE_URL: `${plan.origins.mcp}/v1/mcp/sse`,
        MCP_ALLOW_UNAUTHENTICATED_DCR: "false",
      });
      expect(plan.configs.edge.config.vars.MCP_AUTHORIZATION_SERVER_URL).toBe(
        `${plan.origins.auth}/api/auth`
      );
      if (stage === "local") {
        expect(plan.configs.web.config.routes).toBeUndefined();
      } else {
        expect(plan.configs.web.config.routes).toEqual(
          expect.arrayContaining([
            {
              pattern: new URL(plan.origins.web).hostname,
              custom_domain: true,
            },
            {
              pattern: new URL(plan.origins.share).hostname,
              custom_domain: true,
            },
          ])
        );
        expect(plan.configs.edge.config.routes).not.toContainEqual({
          pattern: new URL(plan.origins.share).hostname,
          custom_domain: true,
        });
      }
      expect(
        plan.configs.realtime.config.vars.ACCOUNT_ACTIVATION_FENCE_ENABLED
      ).toBe("false");
      for (const role of ["edge", "realtime", "api-core"])
        expect(
          plan.configs[role].config.vars.ACCOUNT_CUTOVER_BOOTSTRAP_ENABLED
        ).toBe("true");
      expect(
        plan.configs["api-core"].config.vars.ACCOUNT_CUTOVER_MANIFEST_ID
      ).toBe(fixture.input.migration_lineage);
      for (const database of plan.migrations)
        for (const { config } of Object.values(plan.configs))
          for (const binding of config.d1_databases ?? [])
            if (binding.binding === database.binding)
              expect([
                binding.database_name,
                binding.database_id,
                binding.migrations_dir,
              ]).toEqual([
                database.database_name,
                database.database_id,
                database.directory,
              ]);
      for (const [role, dependencies] of Object.entries(plan.dependencies))
        for (const dependency of dependencies)
          expect(
            plan.deploy_order.indexOf(plan.configs[dependency].config.name)
          ).toBeLessThan(
            plan.deploy_order.indexOf(plan.configs[role].config.name)
          );
      expect(plan.rollback_order).toEqual([...plan.deploy_order].reverse());
      expect(JSON.stringify(plan)).not.toMatch(
        /summersmile1984|omi-cf-|omi-web-app/
      );
      expect(plan.release_ready).toBe(false);
      expect(plan.remote_state_verified).toBe(false);
    }
  });

  it("is deterministic and isolates two brands and two stages, including D1 IDs and secret references", () => {
    const alpha = render(resourceFixture("alpha", "beta", 1));
    expect(alpha).toEqual(render(resourceFixture("alpha", "beta", 1)));
    const other = render(resourceFixture("bravo", "beta", 2)),
      production = render(resourceFixture("alpha", "production", 3));
    expect(() => assertDisjointPlans([alpha, other, production])).not.toThrow();
    const collision = structuredClone(other);
    collision.migrations[0].database_id = alpha.migrations[0].database_id;
    expect(() => assertDisjointPlans([alpha, collision])).toThrow("collision");
    const secretCollision = structuredClone(other);
    secretCollision.secrets.auth.BETTER_AUTH_SECRET =
      alpha.secrets.auth.BETTER_AUTH_SECRET;
    expect(() => assertDisjointPlans([alpha, secretCollision])).toThrow(
      "collision"
    );
  });

  it("rejects incomplete identity, IDs and secret ownership before generating configs", () => {
    const badIdentity = resourceFixture();
    badIdentity.input.stage = "production";
    expect(() => render(badIdentity)).toThrow("identity differ");
    const badIds = resourceFixture();
    badIds.input.d1_ids.app = badIds.input.d1_ids.auth;
    expect(() => render(badIds)).toThrow("distinct explicit");
    const missingSecret = resourceFixture();
    delete missingSecret.input.secret_refs.auth.BETTER_AUTH_SECRET;
    expect(() => render(missingSecret)).toThrow("secret name mapping");
    const split = resourceFixture();
    split.input.secret_refs.jobs.INTERNAL_ASSERTION_SECRET = "OTHER_INTERNAL";
    expect(() => render(split)).toThrow("one shared reference");
    const rawSecret = resourceFixture();
    rawSecret.input.secret_refs.auth.BETTER_AUTH_SECRET =
      "secret-content-not-an-environment-name";
    expect(() => render(rawSecret)).toThrow("never secret values");
  });

  it("refuses unqualified mount paths and ambiguous public owners, including matching Web inputs", () => {
    for (const key of ["api", "mcp", "share", "objects"]) {
      const fixture = resourceFixture();
      changedProfile(
        fixture,
        `${key}_base_url`,
        `https://${key}.alpha.invalid/service`
      );
      expect(() => render(fixture)).toThrow("CF-4");
    }
    const collision = resourceFixture();
    changedProfile(
      collision,
      "web_base_url",
      collision.projected.profile.auth_base_url
    );
    expect(() => render(collision)).toThrow("different Worker owners");
    const staleWeb = resourceFixture();
    staleWeb.web.manifest.stage = "production";
    expect(() => render(staleWeb)).toThrow("matching Moonshine");
  });

  it("requires the allocated workers.dev origins instead of silently inheriting an account", () => {
    const fixture = resourceFixture();
    fixture.input.routing = {
      mode: "workers_dev",
      workers_subdomain: "fixture-account",
    };
    expect(() => render(fixture)).toThrow("workers.dev allocation");
    for (const [key, role] of Object.entries({
      api: "edge",
      auth: "auth",
      web: "web",
      mcp: "edge",
      share: "web",
      objects: "edge",
    }))
      changedProfile(
        fixture,
        `${key}_base_url`,
        `https://${
          role === "web" ? "alpha-web-beta" : "alpha-cf-" + role + "-beta"
        }.fixture-account.workers.dev`
      );
    expect(render(fixture).configs.edge.config.workers_dev).toBe(true);
    for (const subdomain of [null, true, 123]) {
      const invalid = structuredClone(fixture);
      invalid.input.routing.workers_subdomain = subdomain;
      for (const key of ["api", "auth", "web", "mcp", "share", "objects"])
        changedProfile(
          invalid,
          `${key}_base_url`,
          fixture.projected.profile[`${key}_base_url`].replace(
            "fixture-account",
            String(subdomain)
          )
        );
      expect(() => render(invalid)).toThrow("explicit subdomain");
    }
  });

  it("preserves explicitly allocated existing resource names and Durable Object migration histories", () => {
    const fixture = resourceFixture();
    fixture.input.allocation = "existing";
    fixture.input.existing_names = Object.fromEntries(
      resourceKeys().map((key) => [key, `owned-${key.replace(":", "-")}-beta`])
    );
    const plan = render(fixture),
      templates = readWorkerTemplates(root);
    for (const resource of plan.resources.filter(
      (item) => item.kind !== "durable-object"
    ))
      expect(resource.name).toBe(fixture.input.existing_names[resource.key]);
    for (const role of ["edge", "realtime", "rate-limit"])
      expect(plan.configs[role].config.migrations).toEqual(
        templates[role].config.migrations
      );
    expect(plan.configs.web.config.services).toEqual([
      { binding: "EDGE", service: fixture.input.existing_names["worker:edge"] },
    ]);
    expect(
      plan.deploy_order.indexOf(fixture.input.existing_names["worker:edge"])
    ).toBeLessThan(
      plan.deploy_order.indexOf(fixture.input.existing_names["worker:web"])
    );
    expect(plan.configs.edge.config.vars.ACCOUNT_ACTIVATION_FENCE_ENABLED).toBe(
      "true"
    );
    delete fixture.input.existing_names["d1:app"];
    expect(() => render(fixture)).toThrow("existing resource names");
  });

  it("fails on unknown bindings, split D1 authority and a deployment dependency cycle", () => {
    const screenshotLeak = resourceFixture();
    screenshotLeak.templates = readWorkerTemplates(root);
    screenshotLeak.templates.jobs.config.r2_buckets.push(
      screenshotLeak.templates["screen-frame-writer"].config.r2_buckets[0]
    );
    expect(() => render(screenshotLeak)).toThrow("isolated writer");
    const screenshotAi = resourceFixture();
    screenshotAi.templates = readWorkerTemplates(root);
    screenshotAi.templates["screen-frame-writer"].config.ai = { binding: "AI" };
    expect(() => render(screenshotAi)).toThrow("isolated writer");
    const screenshotKey = resourceFixture();
    screenshotKey.input.secret_refs[
      "screen-frame-writer"
    ].SCREEN_FRAME_SIGNING_SECRET =
      screenshotKey.input.secret_refs.jobs.INTERNAL_ASSERTION_SECRET;
    expect(() => render(screenshotKey)).toThrow("isolated shared secret");
    const unknown = resourceFixture();
    unknown.templates = readWorkerTemplates(root);
    unknown.templates.edge.config.services[0].service = "unregistered-worker";
    expect(() => render(unknown)).toThrow("unknown service");
    const split = resourceFixture();
    split.templates = readWorkerTemplates(root);
    split.templates.jobs.config.d1_databases[0].database_name = "another-db";
    expect(() => render(split)).toThrow("drifts between Workers");
    const cycle = resourceFixture();
    cycle.templates = readWorkerTemplates(root);
    cycle.templates["api-core"].config.services[0].service =
      cycle.templates.edge.config.name;
    expect(() => render(cycle)).toThrow("cycle");
    const kv = resourceFixture();
    kv.templates = readWorkerTemplates(root);
    kv.templates.auth.config.kv_namespaces = [
      { binding: "NEW_CACHE", id: "unowned" },
    ];
    expect(() => render(kv)).toThrow("unclassified kv_namespaces");
  });

  it("materializes the same plan into Worker, migration and rollback references without rewriting another output owner", () => {
    const plan = render(),
      source = temporaryDirectory(),
      output = resolve(temporaryDirectory(), "bundle"),
      web = temporaryDirectory();
    for (const [role, entry] of Object.entries(plan.configs)) {
      const directory =
        role === "web" ? web : dirname(resolve(source, entry.source_config));
      const main = resolve(directory, entry.config.main);
      mkdirSync(dirname(main), { recursive: true });
      writeFileSync(main, "fixture-only module");
      if (role.startsWith("api-"))
        mkdirSync(resolve(directory, "python_modules"));
      if (role === "web") mkdirSync(resolve(web, "public"));
    }
    expect(
      materializeResourceBundle(plan, {
        root: source,
        output,
        webArtifact: web,
      }).render_valid
    ).toBe(true);
    expect(
      materializeResourceBundle(plan, {
        root: source,
        output,
        webArtifact: web,
        check: true,
      }).changed
    ).toEqual([]);
    const config = JSON.parse(
      readFileSync(resolve(output, "migrations/app.json"))
    );
    expect(config.d1_databases[0].database_id).toBe(
      plan.migrations[1].database_id
    );
    const foreign = render(resourceFixture("bravo", "beta", 2));
    expect(() =>
      materializeResourceBundle(foreign, {
        root: source,
        output,
        webArtifact: web,
      })
    ).toThrow("another deployment");
    const corrupt = structuredClone(plan);
    corrupt.configs.auth.config.name = "wrong";
    expect(() =>
      materializeResourceBundle(corrupt, {
        root: source,
        output,
        webArtifact: web,
      })
    ).toThrow("digest mismatch");

    // Real filesystem links, including dangling leaves, must be rejected before
    // any output is rewritten. /tmp may itself be an OS alias above the root.
    for (const [relative, broken] of [
      ["workers/auth/wrangler.json", true],
      ["workers/auth/wrangler.json", false],
      ["workers/auth", false],
      ["workers", false],
      ["resource-plan.json", true],
      ["workers/api-core/python_modules", true],
      ["", false],
    ]) {
      const bundle = resolve(temporaryDirectory(), "bundle");
      materializeResourceBundle(plan, {
        root: source,
        output: bundle,
        webArtifact: web,
      });
      const marker = resolve(bundle, "resource-plan.json");
      const originalMarker = readFileSync(marker, "utf8") + "\n";
      writeFileSync(marker, originalMarker);
      const outside = temporaryDirectory(),
        sentinel = resolve(outside, "sentinel");
      writeFileSync(sentinel, "outside-owned");
      if (relative === "")
        writeFileSync(
          resolve(outside, "resource-plan.json"),
          JSON.stringify(plan)
        );
      const target = resolve(bundle, relative);
      rmSync(target, { recursive: true, force: true });
      symlinkSync(
        broken
          ? resolve(outside, "missing")
          : relative.endsWith(".json")
          ? sentinel
          : outside,
        target
      );
      const before = readdirSync(outside, { recursive: true }).sort();
      for (const check of [true, false]) {
        expect(() =>
          materializeResourceBundle(plan, {
            root: source,
            output: bundle,
            webArtifact: web,
            check,
          })
        ).toThrow(/symlink|link.*owner/);
        expect(readdirSync(outside, { recursive: true }).sort()).toEqual(
          before
        );
        expect(readFileSync(sentinel, "utf8")).toBe("outside-owned");
        if (relative !== "" && relative !== "resource-plan.json")
          expect(readFileSync(marker, "utf8")).toBe(originalMarker);
      }
    }
  });

  it("ties rollback versions to one release attempt and retains D1 rather than inventing a schema reversal", () => {
    const plan = render(),
      observed = Object.fromEntries(
        plan.deploy_order.map((name) => [
          name,
          {
            versions: [
              {
                version_id: "11111111-1111-4111-8111-111111111111",
                percentage: 100,
              },
            ],
          },
        ])
      );
    const snapshot = rollbackSnapshot(plan, observed),
      actions = rollbackActions(plan, snapshot);
    expect(actions.schema_migrations_are_not_rolled_back).toBe(true);
    expect(actions.requires_worker_schema_compatibility_proof).toBe(true);
    expect(actions.actions.map((action) => action.worker)).toEqual(
      plan.rollback_order
    );
    expect(
      actions.actions.every(
        (action) => action.action === "rollback_worker_version"
      )
    ).toBe(true);
    const tampered = structuredClone(snapshot);
    tampered.migrations[0].database_id = plan.migrations[1].database_id;
    expect(() => rollbackActions(plan, tampered)).toThrow(
      "migration authority"
    );
    expect(() =>
      rollbackActions(plan, { environment: "staging", versions: observed })
    ).toThrow("different resource plan");
    observed[plan.deploy_order[0]] = null;
    expect(
      rollbackActions(plan, rollbackSnapshot(plan, observed)).actions.at(-1)
        .action
    ).toBe("no_previous_version");
    observed[plan.deploy_order[0]] = {
      versions: [
        { version_id: "11111111-1111-4111-8111-111111111111", percentage: 50 },
      ],
    };
    expect(() => rollbackSnapshot(plan, observed)).toThrow("100%");
  });

  it("parses JSONC comments and trailing commas without mutating string values", () => {
    expect(
      parseJsonc(
        '{"url":"https://example.invalid/a,}",/* note */"escaped":"a\\"b", // note\n}'
      )
    ).toEqual({ url: "https://example.invalid/a,}", escaped: 'a"b' });
    expect(() => parseJsonc("{/* broken")).toThrow("unterminated");
  });
  it("executes all planned SQL files and keeps an existing user/session and task across the last migration", () => {
    const plan = render();
    const run = (value) =>
      spawnSync(
        process.env.PYTHON ?? "python3",
        [
          resolve(root, "scripts/verify-resource-migrations.py"),
          "--root",
          root,
        ],
        { input: JSON.stringify(value), encoding: "utf8" }
      );
    const result = run(plan);
    expect(result.stderr).toBe("");
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({
      older_worker_compatibility_proven: false,
      sql_fixture: [
        {
          authority: "auth",
          sql_files: plan.migrations[0].files.length,
          legacy_row_preserved: true,
          reentry_applied: 0,
        },
        {
          authority: "app",
          sql_files: plan.migrations[1].files.length,
          legacy_row_preserved: true,
          reentry_applied: 0,
        },
      ],
    });
    plan.migrations[0].files[0].sha256 = "0".repeat(64);
    const tampered = run(plan);
    expect(tampered.status).toBe(1);
    expect(tampered.stderr).toContain("planned digest");
  });
});
