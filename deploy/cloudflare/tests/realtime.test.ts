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

function meterDatabase(failures = 0) {
  const writes: unknown[][] = [];
  return {
    writes,
    prepare: (_sql: string) => ({
      bind: (...values: unknown[]) => ({
        first: async () => null,
        run: async () => {
          if (failures > 0) {
            failures -= 1;
            throw new Error("simulated D1 meter failure");
          }
          writes.push(values);
          return { success: true };
        },
      }),
    }),
  };
}

function durableState(work: Promise<unknown>[]) {
  const values = new Map<string, unknown>();
  const alarms: number[] = [];
  return {
    values,
    alarms,
    state: {
      waitUntil: (promise: Promise<unknown>) => {
        work.push(promise);
      },
      storage: {
        get: async (key: string) => values.get(key),
        list: async ({ prefix }: { prefix: string }) =>
          new Map([...values].filter(([key]) => key.startsWith(prefix))),
        put: async (key: string, value: unknown) => {
          values.set(key, value);
        },
        delete: async (key: string) => values.delete(key),
        setAlarm: async (timestamp: number) => {
          alarms.push(timestamp);
        },
      },
    },
  };
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
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
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
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
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

  it("streams PCM8 through Workers AI and emits metered Omi segments", async () => {
    installFakeWebSockets();
    const upstream = new FakeSocket();
    const aiCalls: unknown[][] = [];
    const work: Promise<unknown>[] = [];
    const database = meterDatabase();
    const { state } = durableState(work);
    const session = new RealtimeSession(
      state as unknown as DurableObjectState,
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        APP_DB: database,
        AI: {
          run: async (...args: unknown[]) => {
            aiCalls.push(args);
            return { webSocket: upstream } as unknown as Response;
          },
        },
      } as never,
    );
    await session.fetch(
      new Request(
        "https://realtime.test/v4/web/listen?language=multi&sample_rate=8000&codec=pcm8&channels=1",
        { headers: { upgrade: "websocket" } },
      ),
    );
    const pair = FakeWebSocketPair.last;
    const ticket = await createRealtimeTicket(
      {
        uid: "user-1",
        authority: "better-auth",
        requestId: "req-1",
      },
      "test-secret",
    );
    await pair?.server.dispatch("message", {
      data: JSON.stringify({ type: "auth", ticket }),
    });
    await Promise.all(work);

    expect(aiCalls).toHaveLength(1);
    expect(aiCalls[0]).toEqual([
      "@cf/deepgram/nova-3",
      expect.objectContaining({
        encoding: "linear16",
        sample_rate: "8000",
        language: "multi",
        mip_opt_out: true,
      }),
      { websocket: true, tags: ["omi-realtime"] },
    ]);
    expect(pair?.server.sent).toEqual([
      JSON.stringify({ type: "auth_response", success: true }),
      JSON.stringify({ type: "ready", provider: "workers-ai" }),
    ]);

    const pcm8 = new Blob([Uint8Array.from([0, 128, 255])]);
    await pair?.server.dispatch("message", { data: pcm8 });
    await Promise.all(work);
    const converted = upstream.sent[0] as ArrayBuffer;
    const convertedView = new DataView(converted);
    expect(converted.byteLength).toBe(6);
    expect([
      convertedView.getInt16(0, true),
      convertedView.getInt16(2, true),
      convertedView.getInt16(4, true),
    ]).toEqual([-32_768, 0, 32_512]);

    let releaseSlow: () => void = () => undefined;
    let releaseFast: () => void = () => undefined;
    const slowGate = new Promise<void>((resolve) => {
      releaseSlow = resolve;
    });
    const fastGate = new Promise<void>((resolve) => {
      releaseFast = resolve;
    });
    class GatedBlob extends Blob {
      constructor(
        parts: BlobPart[],
        private readonly gate: Promise<void>,
      ) {
        super(parts);
      }

      override async arrayBuffer(): Promise<ArrayBuffer> {
        await this.gate;
        return super.arrayBuffer();
      }
    }
    await Promise.all([
      pair?.server.dispatch("message", {
        data: new GatedBlob([Uint8Array.from([1])], slowGate),
      }),
      pair?.server.dispatch("message", {
        data: new GatedBlob([Uint8Array.from([2])], fastGate),
      }),
    ]);
    releaseFast();
    await Promise.resolve();
    expect(upstream.sent).toHaveLength(1);
    releaseSlow();
    await Promise.all(work);
    expect(
      upstream.sent
        .slice(1)
        .map((value) => new DataView(value as ArrayBuffer).getInt16(0, true)),
    ).toEqual([-32_512, -32_256]);

    const interim = JSON.stringify({
      is_final: false,
      channel: { alternatives: [{ transcript: "partial", words: [] }] },
    });
    await upstream.dispatch("message", { data: interim });
    await Promise.all(work);
    expect(pair?.server.sent).toHaveLength(2);

    const final = JSON.stringify({
      is_final: true,
      speech_final: true,
      channel: {
        alternatives: [
          {
            transcript: "Hello world.",
            words: [
              {
                start: 0,
                end: 0.75,
                word: "hello",
                punctuated_word: "Hello",
                speaker: 0,
              },
              {
                start: 0.75,
                end: 1.5,
                word: "world",
                punctuated_word: "world.",
                speaker: 1,
              },
            ],
          },
        ],
      },
    });
    await upstream.dispatch("message", {
      data: new TextEncoder().encode(final).buffer,
    });
    await Promise.all(work);

    expect(JSON.parse(String(pair?.server.sent.at(-1)))).toEqual([
      {
        speaker: "SPEAKER_00",
        start: 0,
        end: 0.75,
        text: "Hello",
        is_user: false,
        person_id: null,
      },
      {
        speaker: "SPEAKER_01",
        start: 0.75,
        end: 1.5,
        text: "world.",
        is_user: false,
        person_id: null,
      },
    ]);
    expect(database.writes).toHaveLength(1);
    expect(database.writes[0]).toMatchObject({
      0: "user-1",
      1: "realtime",
      4: 1_500,
      7: 1,
    });

    await upstream.dispatch("message", {
      data: JSON.stringify({ type: "Error", description: "provider failed" }),
    });
    expect(pair?.server.sent).toContain(
      JSON.stringify({ type: "provider_unavailable" }),
    );
    expect(pair?.server.closeCode).toBe(1011);
  });

  it("falls back to the external stream when Workers AI is unavailable", async () => {
    installFakeWebSockets();
    const upstream = new FakeSocket();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ webSocket: upstream }) as unknown as Response),
    );
    const warning = vi
      .spyOn(console, "warn")
      .mockImplementation(() => undefined);
    const work: Promise<unknown>[] = [];
    const signed = await realtimeContext("/v2/voice-message/transcribe-stream");
    const session = new RealtimeSession(
      {
        waitUntil: (promise: Promise<unknown>) => {
          work.push(promise);
        },
      } as unknown as DurableObjectState,
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        ASR_WS_URL: "wss://asr.example/listen",
        AI: {
          run: async () => {
            throw new Error("simulated Workers AI outage");
          },
        },
      } as never,
    );
    await session.fetch(
      new Request(
        "https://realtime.test/v2/voice-message/transcribe-stream?language=en&sample_rate=16000&codec=linear16",
        {
          headers: {
            upgrade: "websocket",
            "x-omi-auth-context": signed?.encoded || "",
            "x-omi-internal-signature": signed?.signature || "",
          },
        },
      ),
    );
    await Promise.all(work);

    expect(FakeWebSocketPair.last?.server.sent).toEqual([
      JSON.stringify({ type: "ready", provider: "external" }),
    ]);
    expect(warning).toHaveBeenCalledWith(
      expect.stringContaining(
        '"component":"stt","from":"workers_ai_streaming","to":"deepgram_cloud"',
      ),
    );
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

  it("forwards provider segments and writes their exact interval union", async () => {
    installFakeWebSockets();
    const upstream = new FakeSocket();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ webSocket: upstream }) as unknown as Response),
    );
    const work: Promise<unknown>[] = [];
    const database = meterDatabase();
    const { state } = durableState(work);
    const signed = await realtimeContext();
    const session = new RealtimeSession(
      state as unknown as DurableObjectState,
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        ASR_WS_URL: "wss://asr.example/listen",
        APP_DB: database,
      } as never,
    );
    await session.fetch(
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
    const providerMessage = JSON.stringify({
      segments: [
        { start: 0, end: 1, text: "hello" },
        { start: 0.5, end: 1.5, text: "world" },
      ],
    });

    await upstream.dispatch("message", { data: providerMessage });
    await Promise.all(work);

    expect(pair?.server.sent).toContain(providerMessage);
    expect(database.writes).toHaveLength(1);
    expect(database.writes[0]).toMatchObject({
      0: "user-1",
      1: "realtime",
      4: 1_500,
      7: 1,
    });
    expect(String(database.writes[0][2])).toMatch(/^realtime:/);
  });

  it("retries the latest realtime meter snapshot from durable storage", async () => {
    installFakeWebSockets();
    const upstream = new FakeSocket();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ webSocket: upstream }) as unknown as Response),
    );
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const work: Promise<unknown>[] = [];
    const database = meterDatabase(1);
    const { state, values, alarms } = durableState(work);
    const signed = await realtimeContext();
    const session = new RealtimeSession(
      state as unknown as DurableObjectState,
      {
        INTERNAL_ASSERTION_SECRET: "test-secret",
        ASR_WS_URL: "wss://asr.example/listen",
        APP_DB: database,
      } as never,
    );
    await session.fetch(
      new Request("https://realtime.test/v4/listen", {
        headers: {
          upgrade: "websocket",
          "x-omi-auth-context": signed?.encoded || "",
          "x-omi-internal-signature": signed?.signature || "",
        },
      }),
    );
    await Promise.all(work);

    await upstream.dispatch("message", {
      data: JSON.stringify({
        segments: [{ start: 1, end: 2.25, text: "retry me" }],
      }),
    });
    await Promise.all(work);
    expect([...values.values()][0]).toMatchObject({ speechMs: 1_250 });
    expect(alarms).toHaveLength(1);

    await session.alarm();

    expect(database.writes).toHaveLength(1);
    expect(database.writes[0][4]).toBe(1_250);
    expect(values.size).toBe(0);
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('"outcome":"recovered"'),
    );
  });
});
