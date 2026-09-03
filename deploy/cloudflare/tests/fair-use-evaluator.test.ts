import { describe, expect, it } from "vitest";
import {
  evaluateFairUseCandidate,
  type FairUseCandidate,
} from "../workers/jobs/fair-use-evaluator";

type State = {
  stage: "none" | "warning" | "throttle" | "restrict";
  lease: string | null;
  leaseUntil: number | null;
  nextEvaluationAt: number | null;
  lastCaseRef: string;
};

function fairUseDatabase() {
  const state: State = {
    stage: "none",
    lease: null,
    leaseUntil: null,
    nextEvaluationAt: null,
    lastCaseRef: "",
  };
  const events: Array<Record<string, unknown>> = [];
  const notifications: Array<Record<string, unknown>> = [];

  const statement = (sql: string) => ({
    sql,
    args: [] as unknown[],
    bind(...args: unknown[]) {
      this.args = args;
      return this;
    },
    async first() {
      if (sql.startsWith("SELECT stage, evaluation_lease_token")) {
        return { stage: state.stage, evaluation_lease_token: state.lease };
      }
      if (sql.startsWith("SELECT COUNT(*) AS thirty_days")) {
        return { seven_days: events.length, thirty_days: events.length };
      }
      if (sql.startsWith("SELECT COALESCE(SUM(transcription_seconds)")) {
        return { used_seconds: 0 };
      }
      return null;
    },
    async all() {
      if (sql.startsWith("SELECT id, created_at")) {
        return {
          results: [
            {
              id: "conversation-1",
              created_at: 2_000_000_000,
              started_at: 1_999_996_400,
              finished_at: 2_000_000_000,
              source: "omi",
              structured_json: JSON.stringify({
                title: "Chapter 1",
                overview: "A recorded book chapter",
                category: "media",
              }),
            },
          ],
        };
      }
      return { results: [] };
    },
    async run() {
      if (sql.startsWith("INSERT INTO cf_fair_use_states")) {
        const now = Number(this.args[1]);
        if (
          state.stage === "restrict" ||
          (state.nextEvaluationAt !== null && state.nextEvaluationAt > now) ||
          (state.leaseUntil !== null && state.leaseUntil > now)
        ) {
          return { meta: { changes: 0 } };
        }
        state.lease = String(this.args[2]);
        state.leaseUntil = Number(this.args[3]);
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("INSERT INTO cf_fair_use_events")) {
        if (state.lease !== this.args[16]) return { meta: { changes: 0 } };
        events.push({
          eventId: this.args[0],
          caseRef: this.args[2],
          action: this.args[12],
          previousStage: this.args[13],
          newStage: this.args[14],
        });
        return { meta: { changes: 1 } };
      }
      if (
        sql.startsWith("UPDATE cf_fair_use_states SET stage = ?") &&
        sql.includes("next_evaluation_at")
      ) {
        if (state.lease !== this.args[20]) return { meta: { changes: 0 } };
        state.stage = this.args[0] as State["stage"];
        if (this.args[1] !== "none") state.lastCaseRef = String(this.args[2]);
        state.nextEvaluationAt = Number(this.args[17]);
        state.lease = null;
        state.leaseUntil = null;
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("INSERT INTO cf_notification_outbox")) {
        if (events.some((event) => event.eventId === this.args[9])) {
          notifications.push({
            eventId: this.args[1],
            title: this.args[3],
            data: JSON.parse(String(this.args[5])),
          });
          return { meta: { changes: 1 } };
        }
        return { meta: { changes: 0 } };
      }
      if (
        sql.startsWith("UPDATE cf_fair_use_states SET evaluation_lease_token")
      ) {
        if (state.lease === this.args[1]) {
          state.lease = null;
          state.leaseUntil = null;
          return { meta: { changes: 1 } };
        }
      }
      return { meta: { changes: 0 } };
    },
  });

  const database = {
    prepare: statement,
    batch: async (statements: Array<ReturnType<typeof statement>>) => {
      const snapshots = {
        state: { ...state },
        events: [...events],
        notifications: [...notifications],
      };
      try {
        const results = [];
        for (const item of statements) results.push(await item.run());
        return results;
      } catch (error) {
        Object.assign(state, snapshots.state);
        events.splice(0, events.length, ...snapshots.events);
        notifications.splice(
          0,
          notifications.length,
          ...snapshots.notifications,
        );
        throw error;
      }
    },
  };
  return { database, state, events, notifications };
}

const candidate: FairUseCandidate = {
  uid: "fair-use-user",
  plan: "architect",
  daily_ms: 14_400_001,
  three_day_ms: 14_400_001,
  weekly_ms: 14_400_001,
};

describe("fair-use scheduled evaluator", () => {
  it("claims once and atomically records a classifier-gated warning plus notification", async () => {
    const fake = fairUseDatabase();
    const env = {
      APP_DB: fake.database,
      AI: {
        run: async () => ({
          response: JSON.stringify({
            misuse_score: 0.91,
            usage_type: "audiobook",
            confidence: 0.9,
            evidence: [{ conversation_id: "conversation-1" }],
          }),
        }),
      },
    };
    expect(
      await evaluateFairUseCandidate(env as never, candidate, 2_000_000_000),
    ).toBe(true);
    expect(fake.state.stage).toBe("warning");
    expect(fake.state.lastCaseRef).toMatch(/^FU-[A-F0-9]{12}$/);
    expect(fake.events).toHaveLength(1);
    expect(fake.events[0]).toMatchObject({
      action: "warning",
      previousStage: "none",
      newStage: "warning",
    });
    expect(fake.notifications).toHaveLength(1);
    expect(fake.notifications[0].data).toMatchObject({
      type: "fair_use",
      action: "warning",
      case_ref: fake.state.lastCaseRef,
    });

    expect(
      await evaluateFairUseCandidate(env as never, candidate, 2_000_000_001),
    ).toBe(false);
    expect(fake.events).toHaveLength(1);
  });

  it("records a conservative no-action event when classifier inference fails", async () => {
    const fake = fairUseDatabase();
    const env = {
      APP_DB: fake.database,
      AI: { run: async () => Promise.reject(new Error("AI unavailable")) },
    };
    expect(
      await evaluateFairUseCandidate(env as never, candidate, 2_000_000_000),
    ).toBe(true);
    expect(fake.state.stage).toBe("none");
    expect(fake.events).toHaveLength(1);
    expect(fake.events[0]).toMatchObject({ action: "none", newStage: "none" });
    expect(fake.notifications).toHaveLength(0);
  });
});
