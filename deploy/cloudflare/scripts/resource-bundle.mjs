import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readlinkSync,
  readdirSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { canonical, digest, exactKeys } from "./resource-input.mjs";

export function assertPlanIntegrity(plan) {
  const { plan_digest, ...body } = plan;
  if (typeof plan_digest !== "string" || digest(body) !== plan_digest)
    throw new Error("resource plan digest mismatch");
}
export function rollbackSnapshot(plan, observations) {
  assertPlanIntegrity(plan);
  exactKeys(observations, plan.deploy_order, "rollback version observations");
  const versions = {};
  for (const [name, observed] of Object.entries(observations)) {
    if (observed === null) {
      versions[name] = null;
      continue;
    }
    if (
      observed?.versions?.length !== 1 ||
      observed.versions[0].percentage !== 100 ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        observed.versions[0].version_id,
      )
    )
      throw new Error(
        "rollback requires one explicitly observed 100% active Worker version",
      );
    versions[name] = observed.versions[0].version_id;
  }
  return {
    schema_version: 1,
    brand: plan.brand,
    target: plan.target,
    stage: plan.stage,
    account_id: plan.account_id,
    plan_digest: plan.plan_digest,
    versions,
    migrations: plan.migrations.map(
      ({ authority, database_name, database_id, files }) => ({
        authority,
        database_name,
        database_id,
        files,
      }),
    ),
  };
}
export function rollbackActions(plan, snapshot) {
  assertPlanIntegrity(plan);
  if (
    snapshot.schema_version !== 1 ||
    ["brand", "target", "stage", "account_id", "plan_digest"].some(
      (key) => snapshot[key] !== plan[key],
    )
  )
    throw new Error("rollback snapshot belongs to a different resource plan");
  exactKeys(snapshot.versions, plan.deploy_order, "rollback snapshot Workers");
  const expectedMigrations = plan.migrations.map(
    ({ authority, database_name, database_id, files }) => ({
      authority,
      database_name,
      database_id,
      files,
    }),
  );
  if (
    JSON.stringify(canonical(snapshot.migrations)) !==
    JSON.stringify(canonical(expectedMigrations))
  )
    throw new Error("rollback migration authority differs");
  return {
    schema_migrations_are_not_rolled_back: true,
    requires_worker_schema_compatibility_proof: true,
    actions: plan.rollback_order.map((name) => {
      const version = snapshot.versions[name];
      if (
        version !== null &&
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
          version,
        )
      )
        throw new Error("invalid rollback Worker version");
      // A first release has no previous version. Only a release transaction can
      // prove which Workers it created; this plan never guesses or deletes.
      return version === null
        ? { action: "no_previous_version", worker: name }
        : { action: "rollback_worker_version", worker: name, version };
    }),
    retained_data: plan.migrations.map(({ database_name, database_id }) => ({
      database_name,
      database_id,
    })),
  };
}
export function materializeResourceBundle(
  plan,
  { root, output, webArtifact, check = false },
) {
  assertPlanIntegrity(plan);
  const destination = resolve(output);
  const marker = resolve(destination, "resource-plan.json");
  if (
    existsSync(destination) &&
    readdirSync(destination).length &&
    !existsSync(marker)
  )
    throw new Error("nonempty output has no resource-plan ownership marker");
  if (existsSync(marker)) {
    const previous = JSON.parse(readFileSync(marker, "utf8"));
    if (
      ["brand", "target", "stage", "account_id"].some(
        (key) => previous[key] !== plan[key],
      )
    )
      throw new Error(
        "output directory belongs to another deployment identity",
      );
  }
  const files = new Map(),
    links = new Map();
  const json = (path, data) =>
    files.set(path, JSON.stringify(canonical(data), null, 2) + "\n");
  json("resource-plan.json", plan);
  for (const [role, entry] of Object.entries(plan.configs)) {
    const config = structuredClone(entry.config);
    const sourceDirectory =
      role === "web"
        ? resolve(webArtifact)
        : dirname(resolve(root, entry.source_config));
    config.main = resolve(sourceDirectory, config.main);
    config.$schema = resolve(root, "node_modules/wrangler/config-schema.json");
    for (const [name, path] of Object.entries(config.alias ?? {}))
      if (path.startsWith("."))
        config.alias[name] = resolve(sourceDirectory, path);
    if (config.assets)
      config.assets.directory = resolve(
        sourceDirectory,
        config.assets.directory,
      );
    for (const database of config.d1_databases ?? [])
      database.migrations_dir = resolve(root, database.migrations_dir);
    if (
      !existsSync(config.main) ||
      (config.assets && !existsSync(config.assets.directory))
    )
      throw new Error(`missing source/artifact closure for ${role}`);
    json(`workers/${role}/wrangler.json`, config);
    if (role.startsWith("api-")) {
      const modules = resolve(sourceDirectory, "python_modules");
      if (!existsSync(modules))
        throw new Error(
          `run npm run python -- ${role} sync before materializing`,
        );
      links.set(`workers/${role}/python_modules`, modules);
    }
  }
  for (const authority of plan.migrations)
    json(`migrations/${authority.authority}.json`, {
      name: authority.worker,
      account_id: plan.account_id,
      compatibility_date:
        plan.configs[authority.authority === "auth" ? "auth" : "api-core"]
          .config.compatibility_date,
      d1_databases: [
        {
          binding: authority.binding,
          database_name: authority.database_name,
          database_id: authority.database_id,
          migrations_dir: resolve(root, authority.directory),
        },
      ],
    });
  json("rollback-contract.json", {
    brand: plan.brand,
    target: plan.target,
    stage: plan.stage,
    account_id: plan.account_id,
    plan_digest: plan.plan_digest,
    worker_order: plan.rollback_order,
    d1_authorities: plan.migrations.map(
      ({ authority, database_name, database_id }) => ({
        authority,
        database_name,
        database_id,
      }),
    ),
    requires_observed_worker_versions: true,
    schema_migrations_are_not_rolled_back: true,
    requires_worker_schema_compatibility_proof: true,
  });
  const changed = [];
  for (const [path, content] of files) {
    const full = resolve(destination, path);
    if (existsSync(full) && lstatSync(full).isSymbolicLink())
      throw new Error("generated configuration path cannot be a symlink");
    if (!existsSync(full) || readFileSync(full, "utf8") !== content)
      changed.push(path);
  }
  for (const [path, target] of links) {
    const full = resolve(destination, path);
    if (
      existsSync(full) &&
      (!lstatSync(full).isSymbolicLink() || readlinkSync(full) !== target)
    )
      throw new Error("Python module link has a different owner");
    if (!existsSync(full)) changed.push(path);
  }
  if (!check) {
    for (const [path, content] of files) {
      const full = resolve(destination, path);
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, content);
    }
    for (const [path, target] of links) {
      const full = resolve(destination, path);
      if (!existsSync(full)) symlinkSync(target, full, "dir");
    }
  }
  return {
    changed,
    files: [...files.keys()],
    release_ready: false,
    render_valid: !check || changed.length === 0,
    dry_run_verified: false,
  };
}
