import { describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import { registerShareEmailRoutes } from "../workers/jobs/share-email";
import {
  createSignedAuthContext,
  verifyRequestAuthContext,
} from "../workers/shared/auth-context";
import type { JobsEnv } from "../workers/jobs/env";

const secret = "synthetic-share-email-secret";
const path = "/v1/conversations/meeting/share-email";
const payload = {
  to: ["guest@example.invalid"],
  subject: "Meeting notes",
  html: "<p>Synthetic notes</p>",
  sender_name: "Owner",
  brand_name: "Eddy",
  reply_to: "owner@example.invalid",
};

function fixture() {
  const calls: Array<{ path: string; body: unknown }> = [];
  const state = {
    prepare: {
      dispatch_id: "attempt",
      already_sent: ["previous@example.invalid"],
    } as object,
    claimStatus: 200,
    finishStatus: 200,
  };
  const send = vi.fn(async () => ({ messageId: "native-message-id" }));
  const env = {
    INTERNAL_ASSERTION_SECRET: secret,
    SHARE_EMAIL_FROM_ADDRESS: "notes@eddy.example.invalid",
    SHARE_EMAIL: { send },
    API_CORE: {
      async fetch(request: Request) {
        const context = await verifyRequestAuthContext(
          request,
          "api-core",
          secret
        );
        expect(context?.uid).toBe("owner");
        expect(context?.authority).toBe("internal");
        expect(request.headers.has("authorization")).toBe(false);
        expect(request.headers.has("cookie")).toBe(false);
        const url = new URL(request.url),
          body = await request.text();
        calls.push({
          path: url.pathname,
          body: body ? JSON.parse(body) : null,
        });
        if (url.pathname.endsWith("prepare"))
          return Response.json(state.prepare);
        if (url.pathname.endsWith("claim"))
          return Response.json(payload, { status: state.claimStatus });
        if (url.pathname.endsWith("finish"))
          return Response.json(
            { phase: JSON.parse(body).phase },
            { status: state.finishStatus }
          );
        throw new Error("Unexpected private request");
      },
    },
  } as unknown as JobsEnv;
  const app = new Hono<{ Bindings: JobsEnv }>();
  registerShareEmailRoutes(app, (c) =>
    verifyRequestAuthContext(c.req.raw, "jobs", secret)
  );
  async function request(authenticated = true) {
    const signed = await createSignedAuthContext(
      { uid: "owner", authority: "better-auth", requestId: "test" },
      "jobs",
      "POST",
      path,
      secret
    );
    return app.fetch(
      new Request("https://jobs.test" + path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: "Bearer private-session",
          cookie: "private-cookie",
          ...(authenticated
            ? {
                "x-omi-auth-context": signed!.encoded,
                "x-omi-internal-signature": signed!.signature,
              }
            : {}),
        },
        body: JSON.stringify({ recipient_emails: ["guest@example.invalid"] }),
      }),
      env
    );
  }
  return { calls, state, send, env, request };
}

describe("native summary email dispatch", () => {
  it("claims before exactly one provider call and confirms before responding", async () => {
    const f = fixture();
    f.send.mockImplementation(async () => {
      expect(f.calls.map((x) => x.path.split("/").at(-1))).toEqual([
        "prepare",
        "claim",
      ]);
      return { messageId: "native-message-id" };
    });
    const response = await f.request();
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    expect(await response.json()).toEqual({
      sent_to: ["guest@example.invalid", "previous@example.invalid"],
    });
    expect(f.send).toHaveBeenCalledExactlyOnceWith({
      from: { email: "notes@eddy.example.invalid", name: "Owner via Eddy" },
      to: payload.to,
      subject: payload.subject,
      html: payload.html,
      replyTo: payload.reply_to,
    });
    expect(f.calls.at(-1)?.body).toEqual({
      phase: "sent",
      message_id: "native-message-id",
    });
  });

  it.each([
    "E_SENDER_NOT_VERIFIED",
    "E_RATE_LIMIT_EXCEEDED",
    "E_VALIDATION_ERROR",
    "E_FIELD_MISSING",
  ])("rejects %s definitively", async (code) => {
    const f = fixture();
    f.send.mockRejectedValue({ code, message: "PRIVATE provider detail" });
    const response = await f.request();
    expect(response.status).toBe(502);
    expect(f.calls.at(-1)?.body).toEqual({ phase: "rejected" });
    expect(await response.text()).not.toContain("PRIVATE");
    expect(f.send).toHaveBeenCalledTimes(1);
  });

  it.each([
    new Error("PRIVATE socket reset"),
    { code: "E_INTERNAL_SERVER_ERROR" },
    { code: "E_DELIVERY_FAILED" },
  ])("persists unknown delivery without a retry", async (error) => {
    const f = fixture();
    f.send.mockRejectedValue(error);
    const response = await f.request();
    expect(response.status).toBe(504);
    expect(f.calls.at(-1)?.body).toEqual({ phase: "ambiguous" });
    expect(f.send).toHaveBeenCalledTimes(1);
    expect(await response.text()).not.toContain("PRIVATE");
  });

  it("does not resend after delivery confirmation persistence fails", async () => {
    const f = fixture();
    f.state.finishStatus = 503;
    expect((await f.request()).status).toBe(504);
    expect(f.send).toHaveBeenCalledTimes(1);
  });

  it("returns an already-sent result without crossing the provider boundary", async () => {
    const f = fixture();
    f.state.prepare = { sent_to: payload.to };
    expect(await (await f.request()).json()).toEqual({ sent_to: payload.to });
    expect(f.send).not.toHaveBeenCalled();
    expect(f.calls).toHaveLength(1);
  });

  it("does not send when the claim was revoked or another request owns it", async () => {
    const f = fixture();
    f.state.claimStatus = 409;
    expect((await f.request()).status).toBe(409);
    expect(f.send).not.toHaveBeenCalled();
  });

  it("requires both the trusted identity and a configured native sender", async () => {
    const f = fixture();
    expect((await f.request(false)).status).toBe(401);
    f.env.SHARE_EMAIL = undefined;
    expect((await f.request()).status).toBe(503);
    expect(f.calls).toHaveLength(0);
    expect(f.send).not.toHaveBeenCalled();
  });
});
