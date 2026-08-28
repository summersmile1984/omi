import { afterEach, describe, expect, it, vi } from "vitest";
import { createSignedAuthContext } from "../workers/shared/auth-context";
import { createRealtimeBootstrap } from "../workers/shared/realtime-bootstrap";
import { createRealtimeTicket } from "../workers/shared/realtime-ticket";
import realtime, { RealtimeSession } from "../workers/realtime/index";

async function realtimeContext(path = "/v4/listen") {
  return createSignedAuthContext(
    { uid: "user-1", authority: "better-auth", requestId: "req-1" },
    "realtime",
    "GET",
    path,
    "test-secret",
  );
}

class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeSocket.OPEN;
  accepted = false;
  sent: unknown[] = [];
  closeCode?: number;
  closeReason?: string;
  private listeners = new Map<
    string,
    Array<(event: { data?: unknown }) => void | Promise<void>>
  >();

  accept() {
    this.accepted = true;
  }

  addEventListener(
    type: string,
    listener: (event: { data?: unknown }) => void | Promise<void>,
  ) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close(code?: number, reason?: string) {
    this.closeCode = code;
    this.closeReason = reason;
    this.readyState = FakeSocket.CLOSED;
  }

  async dispatch(type: string, event: { data?: unknown } = {}) {
    await Promise.all(
      (this.listeners.get(type) || []).map((listener) => listener(event)),
    );
  }
}

class FakeWebSocketPair {
  static last?: FakeWebSocketPair;
  readonly client = new FakeSocket();
  readonly server = new FakeSocket();

  constructor() {
    FakeWebSocketPair.last = this;
  }
}

class TestResponse {
  readonly status: number;
  readonly webSocket?: FakeSocket;

  constructor(
    _body: unknown,
    init?: { status?: number; webSocket?: FakeSocket },
  ) {
    this.status = init?.status || 200;
    this.webSocket = init?.webSocket;
  }

  static json(_body: unknown, init?: { status?: number }) {
    return new TestResponse(null, init);
  }
}

function installFakeWebSockets() {
  const runtime = globalThis as unknown as {
    WebSocketPair?: unknown;
    WebSocket?: unknown;
  };
  runtime.WebSocketPair = FakeWebSocketPair;
  runtime.WebSocket = FakeSocket;
  globalThis.Response = TestResponse as unknown as typeof Response;
}

describe("realtime gateway", () => {
  const originalResponse = globalThis.Response;
  const runtime = globalThis as unknown as {
    WebSocketPair?: unknown;
    WebSocket?: unknown;
  };
  const originalPair = runtime.WebSocketPair;
  const originalWebSocket = runtime.WebSocket;

  afterEach(() => {
    runtime.WebSocketPair = originalPair;
    runtime.WebSocket = originalWebSocket;
    globalThis.Response = originalResponse;
    vi.unstubAllGlobals();
  });

  it("rejects a forged internal context before the websocket upgrade", async () => {
    const signed = await realtimeContext();
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": "forged",
        },
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(401);
  });

  it("requires an upgrade before checking native authentication", async () => {
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen"),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(426);
  });

  it("accepts only a signed short-lived bootstrap for browser sessions", async () => {
    const bootstrap = await createRealtimeBootstrap("request-1", "test-secret");
    const names: string[] = [];
    const env = {
      INTERNAL_ASSERTION_SECRET: "test-secret",
      REALTIME_SESSIONS: {
        idFromName(name: string) {
          names.push(name);
          return name;
        },
        get() {
          return {
            fetch: () => Response.json({ status: "forwarded" }),
          };
        },
      },
    };
    const valid = await realtime.fetch(
      new Request("https://realtime.test/v4/web/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-realtime-bootstrap": bootstrap?.encoded || "",
          "x-omi-realtime-bootstrap-signature": bootstrap?.signature || "",
        },
      }),
      env as never,
    );
    const forged = await realtime.fetch(
      new Request("https://realtime.test/v4/web/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-realtime-bootstrap": bootstrap?.encoded || "",
          "x-omi-realtime-bootstrap-signature": "forged",
        },
      }),
      env as never,
    );

    expect(valid.status).toBe(200);
    expect(forged.status).toBe(401);
    expect(names).toHaveLength(1);
    expect(names[0]).toMatch(/^web:/);
    expect(names[0]).not.toContain("default");
  });

  it("uses the accepted server socket and waits for provider readiness", async () => {
    installFakeWebSockets();
    const signed = await realtimeContext();
    const work: Promise<unknown>[] = [];
    const session = new RealtimeSession(
      {
        waitUntil: (promise: Promise<unknown>) => {
          work.push(promise);
        },
      } as unknown as DurableObjectState,
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    const response = await session.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": signed?.signature || "",
        },
      }),
    );
    await Promise.all(work);
    const pair = FakeWebSocketPair.last;
    expect(response.status).toBe(101);
    expect(response.webSocket).toBe(pair?.client);
    expect(pair?.server.accepted).toBe(true);
    expect(pair?.server.sent).toEqual([
      JSON.stringify({ type: "provider_unavailable" }),
    ]);
    expect(pair?.server.closeCode).toBe(1013);
    expect(pair?.client.sent).toEqual([]);
  });

  it("authenticates the browser first message before opening ASR", async () => {
    installFakeWebSockets();
    const upstream = new FakeSocket();
    const work: Promise<unknown>[] = [];
    let providerUrl = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request | URL | string) => {
        providerUrl = String(
          request instanceof Request ? request.url : request,
        );
        return { webSocket: upstream } as unknown as Response;
      }),
    );
    const session = new RealtimeSession(
      {
        waitUntil: (promise: Promise<unknown>) => {
          work.push(promise);
        },
      } as unknown as DurableObjectState,
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        ASR_WS_URL: "wss://asr.example/listen",
        ASR_API_KEY: "provider-key",
      } as never,
    );
    const response = await session.fetch(
      new Request(
        "https://realtime.test/v4/web/listen?language=en&sample_rate=16000&client_conversation_id=conversation-1",
        { headers: { upgrade: "websocket", "x-request-id": "req-1" } },
      ),
    );
    const pair = FakeWebSocketPair.last;
    expect(response.status).toBe(101);
    expect(pair?.server.sent).toEqual([]);
    const replay = await session.fetch(
      new Request("https://realtime.test/v4/web/listen", {
        headers: { upgrade: "websocket" },
      }),
    );
    expect(replay.status).toBe(409);

    const ticket = await createRealtimeTicket(
      {
        uid: "user-1",
        authority: "better-auth",
        requestId: "req-1",
      },
      "test-secret",
    );
    await pair?.server.dispatch("message", {
      data: JSON.stringify({
        type: "auth",
        ticket,
        device_id_hash: "device-1",
      }),
    });
    await Promise.all(work);

    expect(pair?.server.sent).toEqual([
      JSON.stringify({ type: "auth_response", success: true }),
      JSON.stringify({ type: "ready", provider: "external" }),
    ]);
    const target = new URL(providerUrl);
    expect(target.searchParams.get("uid")).toBe("user-1");
    expect(target.searchParams.get("device_id_hash")).toBe("device-1");
    expect(target.searchParams.get("language")).toBe("en");
    expect(target.searchParams.get("sample_rate")).toBe("16000");
    const audio = new ArrayBuffer(4);
    await pair?.server.dispatch("message", { data: audio });
    await Promise.all(work);
    expect(upstream.sent).toEqual([audio]);
  });

  it("closes a browser socket that sends audio before authentication", async () => {
    installFakeWebSockets();
    const work: Promise<unknown>[] = [];
    const session = new RealtimeSession(
      {
        waitUntil: (promise: Promise<unknown>) => {
          work.push(promise);
        },
      } as unknown as DurableObjectState,
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    await session.fetch(
      new Request("https://realtime.test/v4/web/listen", {
        headers: { upgrade: "websocket" },
      }),
    );
    const pair = FakeWebSocketPair.last;
    await pair?.server.dispatch("message", { data: new ArrayBuffer(4) });
    await Promise.all(work);
    expect(pair?.server.closeCode).toBe(4001);
    expect(pair?.server.sent).toEqual([
      JSON.stringify({
        type: "auth_response",
        success: false,
        error: "invalid_auth_message",
      }),
    ]);
  });
});
