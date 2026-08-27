import { describe, expect, it } from "vitest";
import {
  encodeAuthContext,
  signAuthContext,
} from "../workers/shared/auth-context";
import realtime, { RealtimeSession } from "../workers/realtime/index";

const context = encodeAuthContext({
  uid: "user-1",
  authority: "better-auth",
  requestId: "req-1",
});

class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeSocket.OPEN;
  accepted = false;
  sent: unknown[] = [];
  private listeners = new Map<
    string,
    Array<(event: { data?: unknown }) => void>
  >();

  accept() {
    this.accepted = true;
  }

  addEventListener(
    type: string,
    listener: (event: { data?: unknown }) => void,
  ) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeSocket.CLOSED;
  }

  dispatch(type: string, event: { data?: unknown } = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
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

describe("realtime gateway", () => {
  it("rejects a forged internal context before the websocket upgrade", async () => {
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-auth-context": context,
          "x-omi-internal-signature": "forged",
        },
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(401);
  });

  it("checks the signature before requiring a websocket upgrade", async () => {
    const signature = await signAuthContext(context, "test-secret");
    const response = await realtime.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          "x-omi-auth-context": context,
          "x-omi-internal-signature": signature || "",
        },
      }),
      { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
    );
    expect(response.status).toBe(426);
  });

  it("uses the accepted server socket for client events and responses", async () => {
    const runtime = globalThis as unknown as {
      WebSocketPair?: unknown;
      WebSocket?: unknown;
      Response: typeof Response;
    };
    const originalPair = runtime.WebSocketPair;
    const originalWebSocket = runtime.WebSocket;
    const originalResponse = globalThis.Response;
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
    }
    runtime.WebSocketPair = FakeWebSocketPair;
    runtime.WebSocket = FakeSocket;
    globalThis.Response = TestResponse as unknown as typeof Response;
    try {
      const session = new RealtimeSession(
        {
          waitUntil: (promise: Promise<unknown>) => void promise,
        } as DurableObjectState,
        { INTERNAL_ASSERTION_SECRET: "test-secret" } as never,
      );
      const response = await session.fetch(
        new Request("https://realtime.test/v4/listen", {
          headers: {
            upgrade: "websocket",
            "x-omi-auth-context": context,
            "x-omi-internal-signature":
              (await signAuthContext(context, "test-secret")) || "",
          },
        }),
      );
      const pair = FakeWebSocketPair.last;
      expect(response.status).toBe(101);
      expect(response.webSocket).toBe(pair?.client);
      expect(pair?.server.accepted).toBe(true);
      pair?.server.dispatch("message", { data: new ArrayBuffer(0) });
      expect(pair?.server.sent).toEqual([
        JSON.stringify({ type: "ready", provider: "unconfigured" }),
        JSON.stringify({ type: "provider_unavailable" }),
      ]);
      expect(pair?.client.sent).toEqual([]);
    } finally {
      runtime.WebSocketPair = originalPair;
      runtime.WebSocket = originalWebSocket;
      globalThis.Response = originalResponse;
    }
  });
});
