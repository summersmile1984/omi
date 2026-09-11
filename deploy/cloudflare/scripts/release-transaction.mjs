import { randomUUID } from "node:crypto";
import { digest } from "./resource-input.mjs";
import { assertPlanIntegrity } from "./resource-bundle.mjs";

export function assertMigrationPrefix(authority, ledger) {
  const expected = authority.files.map((file) => file.name);
  if (
    !Array.isArray(ledger) ||
    ledger.length > expected.length ||
    ledger.some((name, index) => name !== expected[index])
  )
    throw new Error(
      "remote D1 migration history is not an exact prefix of this SQL authority",
    );
  return expected.slice(ledger.length);
}
export function createJournal(candidate, phase) {
  if (!["provision", "apply"].includes(phase))
    throw new Error("invalid release transaction phase");
  assertPlanIntegrity(candidate.resource_plan);
  const journal = {
    schema_version: 1,
    transaction: randomUUID(),
    candidate_digest: candidate.candidate_digest,
    brand: candidate.brand,
    account_id: candidate.account_id,
    stage: candidate.stage,
    phase,
    state: "prepared",
    events: [],
    before: {},
    qualification: [],
    release_ready: false,
  };
  seal(journal);
  return journal;
}
function seal(journal) {
  delete journal.journal_digest;
  journal.journal_digest = digest(journal);
}
function record(journal, persist) {
  seal(journal);
  persist(journal);
}
export function assertJournal(candidate, journal) {
  const { journal_digest, ...body } = journal;
  if (journal_digest !== digest(body))
    throw new Error("journal content integrity differs");
  if (
    journal.schema_version !== 1 ||
    journal.candidate_digest !== candidate.candidate_digest ||
    ["brand", "account_id", "stage"].some(
      (key) => journal[key] !== candidate[key],
    )
  )
    throw new Error("journal belongs to a different release candidate");
  if (
    !Array.isArray(journal.events) ||
    !/^[0-9a-f-]{36}$/.test(journal.transaction)
  )
    throw new Error("invalid release transaction journal");
}
const stamp = () => new Date().toISOString();
async function mutate(journal, persist, id, invoke, observe) {
  const event = { id, state: "in_flight", started_at: stamp() };
  journal.events.push(event);
  record(journal, persist); // Durable intent must precede the remote operation.
  try {
    const result = await invoke();
    event.process = result;
    record(journal, persist);
    event.observation = await observe(result);
    event.state = "confirmed";
    event.completed_at = stamp();
    record(journal, persist);
    return event.observation;
  } catch {
    event.state = "unknown";
    journal.state = "reconciliation_required";
    record(journal, persist);
    throw new Error(`remote outcome requires reconciliation: ${id}`);
  }
}

export async function provisionResources({
  candidate,
  journal,
  adapter,
  persist,
}) {
  assertJournal(candidate, journal);
  if (journal.phase !== "provision" || journal.state !== "prepared")
    throw new Error("provision cannot replay a used transaction");
  journal.state = "observing";
  record(journal, persist);
  const ids = { ...candidate.inventory.d1_ids };
  journal.result_inventory = { ...candidate.inventory, d1_ids: ids };
  record(journal, persist);
  for (const resource of candidate.resource_plan.resources.filter((entry) =>
    ["d1", "r2", "queue", "vectorize"].includes(entry.kind),
  )) {
    const before = await adapter.observeResource(resource);
    journal.before[resource.key] = before;
    record(journal, persist);
    if (before.status === "present") {
      // Provision is not adoption. Existing resources must already have the
      // exact inventory identity; another owner's resources cannot be claimed.
      if (
        resource.kind === "d1" &&
        before.id !== ids[resource.key.split(":")[1]]
      )
        throw new Error("existing D1 differs from explicit inventory");
      continue;
    }
    const result = await mutate(
      journal,
      persist,
      `create:${resource.key}`,
      () => adapter.create(resource),
      async (created) => {
        if (!created?.created_id)
          throw new Error("creation response did not establish ownership");
        const observed = await adapter.observeResource(resource);
        if (observed.status !== "present" || observed.id !== created.created_id)
          throw new Error("created resource identity differs from observation");
        return { ...observed, owned_by_transaction: true };
      },
    );
    if (resource.kind === "d1") ids[resource.key.split(":")[1]] = result.id;
    record(journal, persist);
  }
  for (const policy of adapter.policies()) {
    const before = await adapter.observePolicy(policy);
    if (before.status === "present") continue;
    await mutate(
      journal,
      persist,
      `policy:${policy.kind}:${policy.name}:${policy.id}`,
      () => adapter.addPolicy(policy),
      async () => {
        const result = await adapter.waitForPolicy(policy);
        if (result.status !== "present")
          throw new Error(
            "policy is not ready; observe before retrying an asynchronous mutation",
          );
        return result;
      },
    );
  }
  journal.result_inventory = { ...candidate.inventory, d1_ids: ids };
  journal.state = "provisioned_requires_new_candidate";
  record(journal, persist);
  return journal;
}

export async function observeReleaseCandidate(candidate, adapter, continuation) {
  const before = {};
  for (const resource of candidate.resource_plan.resources.filter((entry) =>
    ["d1", "r2", "queue", "vectorize"].includes(entry.kind),
  )) {
    const observed = await adapter.observeResource(resource);
    if (observed.status !== "present")
      throw new Error(`resource is not provisioned: ${resource.key}`);
    if (
      resource.kind === "d1" &&
      observed.id !== candidate.inventory.d1_ids[resource.key.split(":")[1]]
    )
      throw new Error("remote D1 UUID differs from candidate");
    before[resource.key] = observed;
  }
  for (const worker of Object.values(candidate.workers))
    before[worker.name] = await adapter.observeWorker(worker.name);
  before.release_phase = "candidate";
  if (continuation) {
    continuation.verify(before);
    before.continuation = continuation.reference;
  }
  return before;
}

// The journal is retained on the host and copied into the run's failure
// evidence, so a recorded reason must not carry a credential. Redact by value
// rather than by key: the referenced secret values, the API token and the whole
// bundle (which also covers an unparseable bundle, redacted as one value).
function redactableValues(candidate) {
  const values = new Set();
  const add = (value) => {
    if (typeof value === "string" && value.length >= 8) values.add(value);
  };
  add(process.env.CLOUDFLARE_API_TOKEN);
  add(process.env.RELEASE_SECRETS_JSON);
  try {
    for (const value of Object.values(
      JSON.parse(process.env.RELEASE_SECRETS_JSON || "{}"),
    ))
      add(value);
  } catch {
    // An unparseable bundle cannot be decomposed; it is redacted whole above.
  }
  for (const refs of Object.values(candidate?.resource_plan?.secrets ?? {}))
    for (const reference of Object.values(refs ?? {}))
      add(process.env[reference]);
  // Longest first: a value containing another must be replaced as a whole.
  return [...values].sort((left, right) => right.length - left.length);
}

export function redactReason(reason, values) {
  let text = String(reason ?? "");
  for (const value of values) text = text.split(value).join("***");
  return text.length > 2000 ? `${text.slice(0, 2000)}…` : text;
}

export function releaseFailure(candidate, error) {
  return {
    target: "cloudflare",
    stage: candidate.stage,
    at: stamp(),
    error_name: error instanceof Error ? error.name : typeof error,
    reason: redactReason(
      error instanceof Error ? error.message : error,
      redactableValues(candidate),
    ),
  };
}

export async function applyRelease({
  candidate,
  journal,
  adapter,
  persist,
  qualify,
  verify,
  continuation,
}) {
  assertJournal(candidate, journal);
  if (journal.phase !== "apply" || journal.state !== "prepared")
    throw new Error("apply cannot replay a used transaction; reconcile first");
  verify();
  // Product and schema runner evidence must be produced in this invocation,
  // never accepted from an operator-authored approval JSON file.
  journal.state = "qualifying";
  record(journal, persist);
  journal.before = await observeReleaseCandidate(candidate, adapter, continuation);
  journal.qualification = await qualify(journal.before);
  if (
    !journal.qualification.length ||
    journal.qualification.some(
      (entry) =>
        entry.exit !== 0 ||
        entry.candidate_digest !== candidate.candidate_digest ||
        entry.observation_digest !== digest(journal.before),
    )
  )
    throw new Error("release qualification is incomplete or stale");
  await adapter.preconditions();
  record(journal, persist);
  for (const authority of candidate.resource_plan.migrations) {
    const before = await adapter.migrationLedger(authority);
    const pending = assertMigrationPrefix(authority, before);
    journal.before[`sql:${authority.authority}`] = before;
    record(journal, persist);
    if (!pending.length) continue;
    verify();
    await mutate(
      journal,
      persist,
      `migrate:${authority.authority}`,
      () => adapter.migrate(authority),
      async () => {
        const after = await adapter.migrationLedger(authority);
        if (assertMigrationPrefix(authority, after).length)
          throw new Error("D1 migration result is incomplete");
        return { applied_names: after, sql_hashes: authority.files };
      },
    );
  }
  // The ingress precondition demanded a DNS record the attach refuses to
  // replace, so the release takes its own reservation down here, immediately
  // before the first publish. Only the documented originless placeholder is
  // adopted; anything else is left for the attach to refuse.
  await mutate(
    journal,
    persist,
    "adopt:ingress-placeholders",
    () => adapter.releaseIngressPlaceholders(),
    async (released) => ({ released }),
  );
  journal.state = "deploying";
  record(journal, persist);
  for (const name of candidate.resource_plan.deploy_order) {
    const [role, worker] = Object.entries(candidate.workers).find(
      ([, entry]) => entry.name === name,
    );
    verify();
    const now = await adapter.observeWorker(name),
      before = journal.before[name];
    if (digest(now) !== digest(before))
      throw new Error("Worker changed since release qualification");
    const tag = `${journal.transaction}:${role}`;
    const message = `candidate=${candidate.candidate_digest};artifact=${worker.sha256}`;
    await mutate(
      journal,
      persist,
      `deploy:${role}`,
      () => adapter.deploy(role, tag, message),
      async () => {
        const after = await adapter.observeWorker(name);
        if (
          after.status !== "present" ||
          after.tag !== tag ||
          after.message !== message ||
          after.version === before.version
        )
          throw new Error("active version does not belong to this transaction");
        return {
          ...after,
          name,
          owned_by_transaction: true,
          created_worker: before.status === "absent",
        };
      },
    );
    if (journal.events.at(-1).process.exit !== 0) {
      // A version can be live even when Wrangler failed later while updating
      // triggers/domains. Its ownership permits recovery, not release success.
      journal.state = "recovery_required";
      record(journal, persist);
      throw new Error(
        "Worker version is owned but the publish command did not complete; recovery required",
      );
    }
  }
  try {
    journal.health = await adapter.readiness();
    await adapter.preconditions();
    const deployedObservations = {
      ...journal.before,
      release_phase: "deployed",
      prior_versions: Object.fromEntries(
        Object.values(candidate.workers).map(({ name }) => [
          name,
          journal.before[name],
        ]),
      ),
    };
    for (const event of journal.events.filter((entry) =>
      entry.id.startsWith("deploy:"),
    )) {
      const current = await adapter.observeWorker(event.observation.name);
      if (
        digest(current) !==
        digest(
          Object.fromEntries(
            Object.entries(event.observation).filter(([key]) =>
              ["status", "version", "tag", "message"].includes(key),
            ),
          ),
        )
      )
        throw new Error("version changed before release completion");
      deployedObservations[event.observation.name] = current;
    }
    journal.deployed_qualification = await qualify(deployedObservations);
    if (
      !journal.deployed_qualification.length ||
      journal.deployed_qualification.some(
        (entry) =>
          entry.exit !== 0 ||
          entry.candidate_digest !== candidate.candidate_digest ||
          entry.observation_digest !== digest(deployedObservations),
      )
    )
      throw new Error("deployed product qualification did not complete");
    verify();
    for (const { name } of Object.values(candidate.workers))
      if (
        digest(await adapter.observeWorker(name)) !==
        digest(deployedObservations[name])
      )
        throw new Error("version changed during deployed qualification");
    journal.state = "completed";
    journal.release_ready = true;
    record(journal, persist);
  } catch (error) {
    // Binding the error is the point: the previous `catch {}` discarded the one
    // record of which contract failed, so every readiness failure needed manual
    // archaeology on the host. The reason is redacted, then kept in the journal
    // and echoed to the job log, and the original error stays attached as the
    // cause for a stack trace.
    journal.state = "recovery_required";
    journal.failure = releaseFailure(candidate, error);
    record(journal, persist);
    console.error(
      `release failure evidence: ${JSON.stringify(journal.failure)}`,
    );
    throw new Error(
      `release did not pass readiness: ${journal.failure.reason} (inspect recovery plan)`,
      { cause: error },
    );
  }
  return journal;
}

export async function recoveryPlan(candidate, journal, adapter) {
  assertJournal(candidate, journal);
  const actions = [],
    unresolved = journal.events
      .filter((event) => ["in_flight", "unknown"].includes(event.state))
      .map(({ id, state }) => ({ id, state }));
  for (const name of candidate.resource_plan.rollback_order) {
    const role = Object.entries(candidate.workers).find(
      ([, worker]) => worker.name === name,
    )?.[0];
    const event = journal.events.find((entry) => entry.id === `deploy:${role}`);
    const current = await adapter.observeWorker(name),
      before = journal.before[name];
    if (!event) {
      if (before && digest(current) !== digest(before))
        unresolved.push({ id: name, state: "changed_by_another_owner" });
      continue;
    }
    const restored = journal.events.find(
      (entry) => entry.id === `restore:${name}` && entry.state === "confirmed",
    );
    if (restored && current.version === before?.version) continue;
    if (
      event.state !== "confirmed" ||
      current.version !== event.observation?.version ||
      current.tag !== `${journal.transaction}:${role}`
    )
      actions.push({
        worker: name,
        action: "manual_reconciliation",
        reason: "current version or mutation ownership is unproven",
      });
    else if (before.status === "present")
      actions.push({
        worker: name,
        action: "restore_version",
        version: before.version,
      });
    else
      actions.push({
        worker: name,
        action: "retain_first_release",
        reason:
          "Worker, queue consumers, domains and DO state require separate observed cleanup ownership",
      });
  }
  return {
    candidate_digest: candidate.candidate_digest,
    transaction: journal.transaction,
    actions,
    unresolved,
    data_resources_retained: true,
    sql_rollback: "never",
    requires_schema_compatibility_runner: true,
  };
}
export async function restoreRelease({
  candidate,
  journal,
  adapter,
  persist,
  qualify,
  verify,
}) {
  const plan = await recoveryPlan(candidate, journal, adapter);
  if (
    journal.phase !== "apply" ||
    plan.unresolved.length ||
    plan.actions.some((action) => action.action !== "restore_version")
  )
    throw new Error(
      "automatic recovery cannot delete or overwrite unproven owners",
    );
  const recoverySql = {};
  for (const authority of candidate.resource_plan.migrations) {
    const ledger = await adapter.migrationLedger(authority);
    if (assertMigrationPrefix(authority, ledger).length)
      throw new Error("recovery requires the fully observed candidate schema");
    recoverySql[authority.authority] = ledger;
  }
  const observations = {
    ...journal.before,
    release_phase: "restore",
    recovery_sql: recoverySql,
  };
  const proof = await qualify(observations);
  if (
    !proof.length ||
    proof.some(
      (entry) =>
        entry.exit !== 0 ||
        entry.candidate_digest !== candidate.candidate_digest ||
        entry.observation_digest !== digest(observations),
    )
  )
    throw new Error(
      "new-schema compatibility proof is required before recovery",
    );
  for (const action of plan.actions) {
    verify();
    const refreshed = await recoveryPlan(candidate, journal, adapter);
    if (
      refreshed.unresolved.length ||
      refreshed.actions.some((entry) => entry.action !== "restore_version") ||
      !refreshed.actions.some((entry) => digest(entry) === digest(action))
    )
      throw new Error("recovery ownership changed");
    await mutate(
      journal,
      persist,
      `restore:${action.worker}`,
      () =>
        adapter.rollback(action.worker, action.version, journal.transaction),
      async () => {
        const current = await adapter.observeWorker(action.worker);
        if (current.version !== action.version)
          throw new Error("rollback did not activate the retained version");
        return current;
      },
    );
  }
  try {
    const health = await adapter.readiness();
    const remaining = await recoveryPlan(candidate, journal, adapter);
    if (remaining.actions.length || remaining.unresolved.length)
      throw new Error("recovery version ownership changed");
    journal.state = "restored";
    journal.recovery_health = health;
    journal.release_ready = false;
    record(journal, persist);
  } catch {
    journal.state = "recovery_required";
    journal.release_ready = false;
    record(journal, persist);
    throw new Error(
      "restored versions did not pass recovery readiness/ownership",
    );
  }
  return journal;
}
