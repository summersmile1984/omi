import { describe, expect, it } from "vitest";
import { createHmac } from "node:crypto";
import {
  resolveStagingEdgeUrl,
  runDlqPositiveProbe,
} from "../scripts/dlq-positive-probe.mjs";

const ADMIN_KEY = "admin-key-used-only-by-the-staging-probe";
const SIGNING_SECRET = "dlq-positive-probe-signing-secret-at-least-32";

describe("staging DLQ positive probe", () => {
  it("signs the exact bounded body and requires one queued message", async () => {
    let requestUrl = "";
    let requestInit: RequestInit | undefined;
    const result = await runDlqPositiveProbe({
      edgeUrl: "http://127.0.0.1:8787",
      messageId: "dlq-message-1",
      adminKey: ADMIN_KEY,
      signingSecret: SIGNING_SECRET,
      idempotencyKey: "cf-dlq-probe-test-1",
      nowSeconds: 1_756_000_000,
      fetchImpl: async (url, init) => {
        requestUrl = url;
        requestInit = init;
        return Response.json(
          {
            replayId: "replay-1",
            status: "completed",
            requestedCount: 1,
            queuedCount: 1,
            skippedCount: 0,
            failedCount: 0,
          },
          { status: 202 },
        );
      },
    });

    expect(result).toMatchObject({
      endpoint: "http://127.0.0.1:8787/internal/cf/jobs/dlq/replay",
      messageId: "dlq-message-1",
      status: "completed",
      queuedCount: 1,
    });
    expect(requestUrl).toBe(
      "http://127.0.0.1:8787/internal/cf/jobs/dlq/replay",
    );
    expect(requestInit?.method).toBe("POST");
    expect(requestInit?.body).toBe('{"message_ids":["dlq-message-1"]}');
    const headers = new Headers(requestInit?.headers);
    expect(headers.get("secret-key")).toBe(ADMIN_KEY);
    expect(headers.get("idempotency-key")).toBe("cf-dlq-probe-test-1");
    expect(headers.get("x-dlq-replay-timestamp")).toBe("1756000000");
    expect(headers.get("x-dlq-replay-signature")).toBe(
      createHmac("sha256", SIGNING_SECRET)
        .update(
          '1756000000\ncf-dlq-probe-test-1\n{"message_ids":["dlq-message-1"]}',
        )
        .digest("base64url"),
    );
  });

  it("accepts a 200 idempotent receipt but rejects an unknown-id result", async () => {
    await expect(
      runDlqPositiveProbe({
        edgeUrl: "http://localhost:8787",
        messageId: "dlq-message-1",
        adminKey: ADMIN_KEY,
        signingSecret: SIGNING_SECRET,
        idempotencyKey: "cf-dlq-probe-test-2",
        fetchImpl: async () =>
          Response.json(
            {
              status: "completed",
              requestedCount: 1,
              queuedCount: 1,
              skippedCount: 0,
              failedCount: 0,
            },
            { status: 200 },
          ),
      }),
    ).resolves.toMatchObject({ queuedCount: 1 });

    await expect(
      runDlqPositiveProbe({
        edgeUrl: "http://localhost:8787",
        messageId: "missing-message",
        adminKey: ADMIN_KEY,
        signingSecret: SIGNING_SECRET,
        idempotencyKey: "cf-dlq-probe-test-3",
        fetchImpl: async () =>
          Response.json(
            {
              status: "partial",
              requestedCount: 1,
              queuedCount: 0,
              skippedCount: 1,
              failedCount: 0,
            },
            { status: 200 },
          ),
      }),
    ).rejects.toThrow("did not queue one message");
  });

  it("rejects production-looking or malformed endpoints before a request", async () => {
    expect(() => resolveStagingEdgeUrl("https://omi-edge.workers.dev")).toThrow(
      "staging-only",
    );
    expect(() => resolveStagingEdgeUrl("http://staging.example.test")).toThrow(
      "remote DLQ probe endpoints must use https",
    );
    expect(() => resolveStagingEdgeUrl("http://localhost:8787/path")).toThrow(
      "origin without a path",
    );
  });

  it("validates message, key, and signing-secret inputs without network access", async () => {
    const fetchImpl = async () => {
      throw new Error("network must not be reached");
    };
    await expect(
      runDlqPositiveProbe({
        edgeUrl: "http://localhost:8787",
        messageId: "../production",
        adminKey: ADMIN_KEY,
        signingSecret: SIGNING_SECRET,
        fetchImpl,
      }),
    ).rejects.toThrow("valid captured message id");
    await expect(
      runDlqPositiveProbe({
        edgeUrl: "http://localhost:8787",
        messageId: "dlq-message-1",
        adminKey: ADMIN_KEY,
        signingSecret: "short",
        fetchImpl,
      }),
    ).rejects.toThrow("at least 32 bytes");
  });
});
