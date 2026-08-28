import { describe, expect, it } from "vitest";
import { drainFairUseNotifications } from "../workers/jobs/firebase-messaging";

type Outbox = {
  notification_id: string;
  uid: string;
  title: string;
  body: string;
  data_json: string;
  status: string;
  attempts: number;
  not_before: number;
  lease_until: number | null;
  created_at: number;
  last_error: string | null;
};

function messagingDatabase(now: number) {
  const row: Outbox = {
    notification_id: "notification-1",
    uid: "user-1",
    title: "Fair Use Notice",
    body: "Usage notice",
    data_json: JSON.stringify({ type: "fair_use", action: "warning" }),
    status: "pending",
    attempts: 0,
    not_before: now,
    lease_until: null,
    created_at: now,
    last_error: null,
  };
  const tokens = new Set(["fcm-token-1"]);
  const statement = (sql: string) => ({
    args: [] as unknown[],
    bind(...args: unknown[]) {
      this.args = args;
      return this;
    },
    async all() {
      if (sql.startsWith("SELECT notification_id")) {
        return {
          results:
            (row.status === "pending" &&
              row.not_before <= Number(this.args[0])) ||
            (row.status === "sending" &&
              (row.lease_until || 0) <= Number(this.args[1]))
              ? [{ ...row }]
              : [],
        };
      }
      if (sql.startsWith("SELECT token")) {
        return { results: [...tokens].map((token) => ({ token })) };
      }
      return { results: [] };
    },
    async run() {
      if (sql.includes("SET status = 'sending'")) {
        row.status = "sending";
        row.attempts += 1;
        row.lease_until = Number(this.args[0]);
        return { meta: { changes: 1 } };
      }
      if (sql.includes("SET status = 'sent'")) {
        row.status = "sent";
        row.lease_until = null;
        row.last_error = null;
        return { meta: { changes: 1 } };
      }
      if (sql.startsWith("DELETE FROM cf_user_fcm_tokens")) {
        tokens.delete(String(this.args[1]));
        return { meta: { changes: 1 } };
      }
      if (sql.includes("SET status = ?, not_before")) {
        row.status = String(this.args[0]);
        row.not_before = Number(this.args[1]);
        row.lease_until = null;
        row.last_error = String(this.args[2]);
        return { meta: { changes: 1 } };
      }
      return { meta: { changes: 0 } };
    },
  });
  return { row, tokens, database: { prepare: statement } };
}

const credentials = JSON.stringify({
  project_id: "test-project",
  client_email: "sender@test-project.iam.gserviceaccount.com",
  private_key: "-----BEGIN PRIVATE KEY-----\nAA==\n-----END PRIVATE KEY-----\n",
});

describe("fair-use Firebase outbox", () => {
  it("completes a notification with no registered devices without Firebase credentials", async () => {
    const now = 2_000_000_000;
    const fake = messagingDatabase(now);
    fake.tokens.clear();

    const delivered = await drainFairUseNotifications(
      { APP_DB: fake.database } as never,
      now,
    );

    expect(delivered).toBe(1);
    expect(fake.row.status).toBe("sent");
    expect(fake.row.attempts).toBe(1);
  });

  it("claims, sends, and marks one notification exactly once", async () => {
    const now = 2_000_000_000;
    const fake = messagingDatabase(now);
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const delivered = await drainFairUseNotifications(
      {
        APP_DB: fake.database,
        FIREBASE_SERVICE_ACCOUNT_JSON: credentials,
      } as never,
      now,
      {
        accessToken: async () => "access-token",
        fetcher: (async (input: RequestInfo | URL, init?: RequestInit) => {
          calls.push({
            url: String(input),
            body: JSON.parse(String(init?.body)),
          });
          return new Response(JSON.stringify({ name: "messages/1" }), {
            status: 200,
          });
        }) as typeof fetch,
      },
    );
    expect(delivered).toBe(1);
    expect(fake.row.status).toBe("sent");
    expect(fake.row.attempts).toBe(1);
    expect(calls[0].url).toContain("/projects/test-project/messages:send");
    expect(calls[0].body).toMatchObject({
      message: {
        token: "fcm-token-1",
        notification: { title: "Fair Use Notice", body: "Usage notice" },
        data: { type: "fair_use", action: "warning" },
      },
    });
  });

  it("removes an unregistered token without retrying the durable notification", async () => {
    const now = 2_000_000_000;
    const fake = messagingDatabase(now);
    const delivered = await drainFairUseNotifications(
      {
        APP_DB: fake.database,
        FIREBASE_SERVICE_ACCOUNT_JSON: credentials,
      } as never,
      now,
      {
        accessToken: async () => "access-token",
        fetcher: (async () =>
          new Response(JSON.stringify({ error: { status: "UNREGISTERED" } }), {
            status: 404,
          })) as typeof fetch,
      },
    );
    expect(delivered).toBe(1);
    expect(fake.row.status).toBe("sent");
    expect(fake.tokens.size).toBe(0);
  });

  it("returns a transient provider failure to the pending outbox with backoff", async () => {
    const now = 2_000_000_000;
    const fake = messagingDatabase(now);
    const delivered = await drainFairUseNotifications(
      {
        APP_DB: fake.database,
        FIREBASE_SERVICE_ACCOUNT_JSON: credentials,
      } as never,
      now,
      {
        accessToken: async () => "access-token",
        fetcher: (async () =>
          new Response("unavailable", { status: 503 })) as typeof fetch,
      },
    );
    expect(delivered).toBe(0);
    expect(fake.row.status).toBe("pending");
    expect(fake.row.not_before).toBeGreaterThan(now);
    expect(fake.row.last_error).toBe("firebase delivery unavailable");
  });
});
