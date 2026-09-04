import { describe, expect, it, vi } from "vitest";
import { digest } from "../scripts/resource-input.mjs";
import {
  applyRelease,
  assertMigrationPrefix,
  createJournal,
  provisionResources,
  recoveryPlan,
  restoreRelease,
} from "../scripts/release-transaction.mjs";

const old = "11111111-1111-4111-8111-111111111111",
  next = "22222222-2222-4222-8222-222222222222";
function fixture({ first = false } = {}) {
  const plan = {
    brand: "fixture",
    stage: "beta",
    target: "cloudflare",
    account_id: "a".repeat(32),
    resources: [],
    migrations: [
      {
        authority: "auth",
        files: [{ name: "0001.sql", sha256: "a".repeat(64) }],
      },
    ],
    deploy_order: ["fixture-auth", "fixture-edge"],
    rollback_order: ["fixture-edge", "fixture-auth"],
  };
  plan.plan_digest = digest(plan);
  const candidate = {
    schema_version: 1,
    brand: plan.brand,
    stage: plan.stage,
    account_id: plan.account_id,
    candidate_digest: "b".repeat(64),
    resource_plan: plan,
    inventory: { d1_ids: {} },
    workers: {
      auth: { name: "fixture-auth", sha256: "c".repeat(64) },
      edge: { name: "fixture-edge", sha256: "d".repeat(64) },
    },
  };
  const states = Object.fromEntries(
    plan.deploy_order.map((name) => [
      name,
      first
        ? { status: "absent" }
        : { status: "present", version: old, tag: null, message: null },
    ]),
  );
  const history = [],
    saved = [];
  let ledger = [];
  const adapter = {
    observeWorker: vi.fn(async (name) => structuredClone(states[name])),
    observeResource: vi.fn(async () => ({ status: "present" })),
    policies: () => [],
    preconditions: vi.fn(async () => {}),
    migrationLedger: vi.fn(async () => ledger),
    migrate: vi.fn(() => {
      history.push("migrate");
      ledger = ["0001.sql"];
      return { exit: 0 };
    }),
    deploy: vi.fn((role, tag, message) => {
      history.push(`deploy:${role}`);
      states[candidate.workers[role].name] = {
        status: "present",
        version: next,
        tag,
        message,
      };
      return { exit: 0 };
    }),
    rollback: vi.fn((name, version) => {
      history.push(`restore:${name}`);
      states[name] = { status: "present", version, tag: null, message: null };
      return { exit: 0 };
    }),
    readiness: vi.fn(async () => ({ edge: "ready", web: "ready" })),
  };
  const persist = vi.fn((value) => {
    saved.push(structuredClone(value));
  });
  const qualify = vi.fn(async (observations) => [
    {
      exit: 0,
      candidate_digest: candidate.candidate_digest,
      observation_digest: digest(observations),
    },
  ]);
  const verify = vi.fn();
  const journal = createJournal(candidate, "apply");
  return {
    candidate,
    journal,
    adapter,
    persist,
    qualify,
    verify,
    history,
    states,
    saved,
  };
}
describe("Cloudflare release transaction ownership", () => {
  it("qualifies before writes, journals intent, applies SQL before dependencies, and confirms versions after health", async () => {
    const f = fixture();
    await applyRelease(f);
    expect(f.history).toEqual(["migrate", "deploy:auth", "deploy:edge"]);
    expect(
      f.saved.some((entry) =>
        entry.events.some(
          (event) => event.id === "deploy:auth" && event.state === "in_flight",
        ),
      ),
    ).toBe(true);
    expect(f.journal.state).toBe("completed");
    expect(f.journal.release_ready).toBe(true);
    expect(f.adapter.observeWorker).toHaveBeenCalledTimes(10);
  });
  it("leaves a legacy principal's existing D1 prefix intact and refuses changed/unknown histories", () => {
    const authority = { files: [{ name: "old.sql" }, { name: "new.sql" }] };
    expect(assertMigrationPrefix(authority, ["old.sql"])).toEqual(["new.sql"]);
    for (const ledger of [
      ["new.sql"],
      ["old.sql", "unknown.sql"],
      ["old.sql", "old.sql"],
    ])
      expect(() => assertMigrationPrefix(authority, ledger)).toThrow(
        "exact prefix",
      );
  });
  it.each(["qualification", "source", "journal"])(
    "does not mutate remotely when %s fails",
    async (failure) => {
      const f = fixture();
      if (failure === "qualification")
        f.qualify.mockRejectedValue(new Error("CF-4 pending"));
      if (failure === "source")
        f.verify.mockImplementation(() => {
          throw new Error("source drift");
        });
      if (failure === "journal")
        f.persist.mockImplementation((journal) => {
          if (journal.events.length) throw new Error("disk full");
        });
      await expect(applyRelease(f)).rejects.toThrow();
      expect(f.history).toEqual([]);
    },
  );
  it("records an owned version after a failed publish command but requires recovery for potentially incomplete triggers", async () => {
    const f = fixture(),
      original = f.adapter.deploy;
    f.adapter.deploy = vi.fn((...args) => {
      original(...args);
      return { exit: 1, signal: null };
    });
    await expect(applyRelease(f)).rejects.toThrow(
      "publish command did not complete",
    );
    expect(
      f.journal.events.find((event) => event.id === "deploy:auth").process.exit,
    ).toBe(1);
    expect(f.journal.state).toBe("recovery_required");
    expect(
      (await recoveryPlan(f.candidate, f.journal, f.adapter)).actions[0].action,
    ).toBe("restore_version");
    expect(f.adapter.deploy).toHaveBeenCalledTimes(1);
  });
  it("never retries or rolls back an unknown mutation or a concurrent version", async () => {
    const f = fixture();
    f.adapter.deploy = vi.fn(() => ({ exit: 1 }));
    await expect(applyRelease(f)).rejects.toThrow("reconciliation");
    expect(f.adapter.deploy).toHaveBeenCalledTimes(1);
    expect(
      (await recoveryPlan(f.candidate, f.journal, f.adapter)).actions[0].action,
    ).toBe("manual_reconciliation");
    await expect(restoreRelease(f)).rejects.toThrow("unproven owners");
    expect(f.adapter.rollback).not.toHaveBeenCalled();
  });
  it("after a health failure restores only observed owned versions in reverse dependency order", async () => {
    const f = fixture();
    f.adapter.readiness.mockRejectedValueOnce(new Error("503"));
    await expect(applyRelease(f)).rejects.toThrow("readiness");
    await restoreRelease(f);
    expect(f.history.slice(-2)).toEqual([
      "restore:fixture-edge",
      "restore:fixture-auth",
    ]);
    expect(f.journal.state).toBe("restored");
    expect(f.journal.release_ready).toBe(false);
    expect(f.adapter.migrate).toHaveBeenCalledTimes(1);
  });
  it("retains first-release Workers and data instead of guessing domain/queue/DO cleanup authority", async () => {
    const f = fixture({ first: true });
    f.adapter.readiness.mockRejectedValue(new Error("503"));
    await expect(applyRelease(f)).rejects.toThrow();
    const plan = await recoveryPlan(f.candidate, f.journal, f.adapter);
    expect(
      plan.actions.every((action) => action.action === "retain_first_release"),
    ).toBe(true);
    expect(plan.sql_rollback).toBe("never");
    await expect(restoreRelease(f)).rejects.toThrow("unproven owners");
  });
  it("rejects an altered prior-version journal before recovery observations", async () => {
    const f = fixture();
    await applyRelease(f);
    f.journal.before["fixture-auth"].version = next;
    f.adapter.observeWorker.mockClear();
    await expect(
      recoveryPlan(f.candidate, f.journal, f.adapter),
    ).rejects.toThrow("integrity");
    expect(f.adapter.observeWorker).not.toHaveBeenCalled();
  });
  it("does not adopt a concurrent resource when the create response is lost", async () => {
    const f = fixture();
    f.candidate.resource_plan.resources = [
      { key: "d1:auth", kind: "d1", name: "fixture-auth" },
    ];
    delete f.candidate.resource_plan.plan_digest;
    f.candidate.resource_plan.plan_digest = digest(f.candidate.resource_plan);
    f.journal = createJournal(f.candidate, "provision");
    f.adapter.observeResource
      .mockResolvedValueOnce({ status: "absent" })
      .mockResolvedValue({ status: "present", id: next });
    f.adapter.create = vi.fn(async () => {
      throw new Error("response lost");
    });
    await expect(provisionResources(f)).rejects.toThrow("reconciliation");
    expect(f.adapter.create).toHaveBeenCalledTimes(1);
    expect(f.journal.events[0].state).toBe("unknown");
    expect(f.journal.result_inventory.d1_ids).toEqual({});
  });
  it("returns a new input with actual create IDs and requires rebuilding the candidate", async () => {
    const f = fixture();
    f.candidate.resource_plan.resources = [
      { key: "d1:auth", kind: "d1", name: "fixture-auth" },
    ];
    delete f.candidate.resource_plan.plan_digest;
    f.candidate.resource_plan.plan_digest = digest(f.candidate.resource_plan);
    f.journal = createJournal(f.candidate, "provision");
    f.adapter.observeResource
      .mockResolvedValueOnce({ status: "absent" })
      .mockResolvedValue({ status: "present", id: next });
    f.adapter.create = vi.fn(async () => ({ created_id: next }));
    await provisionResources(f);
    expect(f.journal.result_inventory.d1_ids.auth).toBe(next);
    expect(f.journal.state).toBe("provisioned_requires_new_candidate");
    expect(f.journal.release_ready).toBe(false);
    await expect(provisionResources(f)).rejects.toThrow("cannot replay");
  });
  it("does not mutate a Worker changed after qualification", async () => {
    const f = fixture();
    const migrate = f.adapter.migrate.getMockImplementation();
    f.adapter.migrate.mockImplementation(() => {
      migrate();
      f.states["fixture-auth"].version = next;
      return { exit: 0 };
    });
    await expect(applyRelease(f)).rejects.toThrow("changed since");
    expect(f.adapter.deploy).not.toHaveBeenCalled();
  });
  it("does not describe an unknown partial SQL migration as a successful restore", async () => {
    const f = fixture();
    f.adapter.migrate.mockReturnValue({ exit: 1 });
    await expect(applyRelease(f)).rejects.toThrow("reconciliation");
    await expect(restoreRelease(f)).rejects.toThrow("unproven owners");
    expect(f.adapter.rollback).not.toHaveBeenCalled();
    expect(f.journal.state).toBe("reconciliation_required");
  });
  it("does not declare recovery complete when restored readiness fails", async () => {
    const f = fixture();
    f.adapter.readiness.mockRejectedValue(new Error("503"));
    await expect(applyRelease(f)).rejects.toThrow("readiness");
    await expect(restoreRelease(f)).rejects.toThrow("recovery readiness");
    expect(f.journal.state).toBe("recovery_required");
    expect(f.journal.release_ready).toBe(false);
  });
  it("keeps release readiness false if the deployed product contract fails after healthy endpoints", async () => {
    const f = fixture();
    const original = f.qualify.getMockImplementation();
    f.qualify.mockImplementation(async (observations) => {
      if (observations.release_phase === "deployed")
        throw new Error("product contract failed");
      return original(observations);
    });
    await expect(applyRelease(f)).rejects.toThrow("readiness");
    expect(f.journal.state).toBe("recovery_required");
    expect(f.journal.release_ready).toBe(false);
  });
});
