import { decodeAuthContext, type AuthContext } from "../shared/auth-context";
import {
  recordFairUseUsage,
  speechMsFromTranscription,
  type FairUseUsage,
} from "../shared/fair-use-meter";
import { recordFallback } from "../shared/fallback";
import { verifyRealtimeTicket } from "../shared/realtime-ticket";
import type { RealtimeEnv } from "./env";

const AUTH_TIMEOUT_MS = 15_000;
const MAX_AUTH_MESSAGE_BYTES = 16_384;
const MAX_PENDING_AUDIO_BYTES = 1_000_000;
const MAX_METERED_SEGMENTS = 20_000;
const METER_RETRY_MS = 10_000;
const PENDING_METER_PREFIX = "fair-use-pending:";

type PendingMessage = string | ArrayBuffer;
type MeterSegment = { start: number; end: number; text: string };

function pendingMeterKey(sourceId: string): string {
  return `${PENDING_METER_PREFIX}${sourceId}`;
}

type FirstMessageAuth = {
  type: "auth";
  ticket: string;
  deviceIdHash?: string;
};

function messageBytes(data: PendingMessage): number {
  return typeof data === "string"
    ? new TextEncoder().encode(data).byteLength
    : data.byteLength;
}

function parseFirstMessageAuth(data: string): FirstMessageAuth | null {
  if (messageBytes(data) > MAX_AUTH_MESSAGE_BYTES) return null;
  try {
    const value = JSON.parse(data) as {
      type?: unknown;
      ticket?: unknown;
      device_id_hash?: unknown;
    };
    if (
      value.type !== "auth" ||
      typeof value.ticket !== "string" ||
      value.ticket.length < 1 ||
      value.ticket.length > 32_768 ||
      (value.device_id_hash !== undefined &&
        (typeof value.device_id_hash !== "string" ||
          value.device_id_hash.length > 256))
    ) {
      return null;
    }
    return {
      type: "auth",
      ticket: value.ticket,
      deviceIdHash: value.device_id_hash,
    };
  } catch {
    return null;
  }
}

function providerSegments(data: unknown): MeterSegment[] {
  if (typeof data !== "string") return [];
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return [];
  }
  if (!payload || typeof payload !== "object") return [];
  const object = payload as Record<string, unknown>;
  if (object.is_final === false || object.isFinal === false) return [];
  let candidates: unknown[] = [];
  if (Array.isArray(payload)) candidates = payload;
  else if (Array.isArray(object.segments)) candidates = object.segments;
  else if (Array.isArray(object.words)) candidates = object.words;
  else if (object.segment && typeof object.segment === "object") {
    candidates = [object.segment];
  } else if (
    object.channel &&
    typeof object.channel === "object" &&
    Array.isArray((object.channel as Record<string, unknown>).alternatives)
  ) {
    const alternative = (
      (object.channel as Record<string, unknown>).alternatives as unknown[]
    )[0];
    if (alternative && typeof alternative === "object") {
      const words = (alternative as Record<string, unknown>).words;
      if (Array.isArray(words)) candidates = words;
    }
  } else if ("start" in object && "end" in object) candidates = [object];

  return candidates.flatMap((candidate) => {
    if (!candidate || typeof candidate !== "object") return [];
    const segment = candidate as Record<string, unknown>;
    const text =
      typeof segment.text === "string"
        ? segment.text
        : typeof segment.word === "string"
          ? segment.word
          : "";
    return typeof segment.start === "number" &&
      typeof segment.end === "number" &&
      Number.isFinite(segment.start) &&
      Number.isFinite(segment.end) &&
      segment.start >= 0 &&
      segment.end > segment.start &&
      text.trim()
      ? [{ start: segment.start, end: segment.end, text }]
      : [];
  });
}

export class RealtimeSession {
  private state: DurableObjectState;
  private env: RealtimeEnv;
  private client?: WebSocket;
  private upstream?: WebSocket;
  private authContext?: AuthContext;
  private requestUrl?: string;
  private firstMessageAuth = false;
  private authInFlight = false;
  private authTimeout?: ReturnType<typeof setTimeout>;
  private pending: PendingMessage[] = [];
  private pendingBytes = 0;
  private webBootstrapClaimed = false;
  private meterSegments = new Map<string, MeterSegment>();
  private meterSourceId?: string;
  private meterOccurredAt = 0;
  private meterRevision = 0;
  private meterWriteChain: Promise<void> = Promise.resolve();

  constructor(state: DurableObjectState, env: RealtimeEnv) {
    this.state = state;
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") {
      return Response.json(
        { error: "websocket upgrade required" },
        { status: 426 },
      );
    }
    const firstMessageAuth = new URL(request.url).pathname === "/v4/web/listen";
    if (
      (firstMessageAuth && this.webBootstrapClaimed) ||
      (this.client && this.client.readyState < WebSocket.CLOSING)
    ) {
      return Response.json(
        { error: "realtime session already connected" },
        { status: 409 },
      );
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    server.accept();
    this.client = server;
    this.requestUrl = request.url;
    this.firstMessageAuth = firstMessageAuth;
    if (firstMessageAuth) this.webBootstrapClaimed = true;
    this.authContext = this.firstMessageAuth
      ? undefined
      : decodeAuthContext(request.headers.get("x-omi-auth-context")) ||
        undefined;

    server.addEventListener("message", (event) => {
      this.state.waitUntil(
        this.onClientMessage(server, event.data as PendingMessage),
      );
    });
    server.addEventListener("close", () => this.closeConnection(server));
    server.addEventListener("error", () => this.closeConnection(server));

    if (this.firstMessageAuth) {
      this.authTimeout = setTimeout(() => {
        if (this.client === server && !this.authContext) {
          this.sendJson(server, {
            type: "auth_response",
            success: false,
            error: "authentication_timeout",
          });
          server.close(4001, "authentication timeout");
        }
      }, AUTH_TIMEOUT_MS);
    } else if (this.authContext) {
      this.state.waitUntil(this.startProvider(server, false));
    } else {
      server.close(4001, "missing authentication context");
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  private async onClientMessage(
    socket: WebSocket,
    data: PendingMessage,
  ): Promise<void> {
    if (this.client !== socket) return;
    if (!this.authContext) {
      if (!this.firstMessageAuth || typeof data !== "string") {
        this.failAuthentication(socket, "invalid_auth_message");
        return;
      }
      if (this.authInFlight) {
        this.failAuthentication(socket, "authentication_in_progress");
        return;
      }
      const auth = parseFirstMessageAuth(data);
      if (!auth) {
        this.failAuthentication(socket, "invalid_auth_message");
        return;
      }
      this.authInFlight = true;
      const context = await verifyRealtimeTicket(
        auth.ticket,
        this.env.INTERNAL_ASSERTION_SECRET,
      );
      this.authInFlight = false;
      if (this.client !== socket) return;
      if (!context) {
        this.failAuthentication(socket, "unauthorized");
        return;
      }
      this.authContext = context;
      if (this.authTimeout) clearTimeout(this.authTimeout);
      this.authTimeout = undefined;
      const providerReady = await this.connectUpstream(auth.deviceIdHash);
      if (this.client !== socket) return;
      if (!providerReady) {
        this.sendJson(socket, {
          type: "auth_response",
          success: false,
          error: "provider_unavailable",
        });
        socket.close(1013, "provider unavailable");
        return;
      }
      this.sendJson(socket, { type: "auth_response", success: true });
      this.sendJson(socket, { type: "ready", provider: "external" });
      this.flushPending();
      return;
    }

    if (!this.upstream || this.upstream.readyState !== WebSocket.OPEN) {
      this.bufferMessage(socket, data);
      return;
    }
    this.upstream.send(data);
  }

  private async startProvider(
    socket: WebSocket,
    sendAuthResponse: boolean,
  ): Promise<void> {
    const ready = await this.connectUpstream();
    if (this.client !== socket) return;
    if (!ready) {
      if (sendAuthResponse) {
        this.sendJson(socket, {
          type: "auth_response",
          success: false,
          error: "provider_unavailable",
        });
      } else {
        this.sendJson(socket, { type: "provider_unavailable" });
      }
      socket.close(1013, "provider unavailable");
      return;
    }
    if (sendAuthResponse) {
      this.sendJson(socket, { type: "auth_response", success: true });
    }
    this.sendJson(socket, { type: "ready", provider: "external" });
    this.flushPending();
  }

  private async connectUpstream(deviceIdHash?: string): Promise<boolean> {
    if (
      !this.env.ASR_WS_URL ||
      !this.client ||
      !this.authContext ||
      !this.requestUrl
    ) {
      return false;
    }
    try {
      const incoming = new URL(this.requestUrl);
      const target = new URL(this.env.ASR_WS_URL);
      for (const name of [
        "language",
        "sample_rate",
        "codec",
        "source",
        "include_speech_profile",
        "client_conversation_id",
      ]) {
        const value = incoming.searchParams.get(name);
        if (value) target.searchParams.set(name, value);
      }
      target.searchParams.set("uid", this.authContext.uid);
      if (deviceIdHash) {
        target.searchParams.set("device_id_hash", deviceIdHash);
      }
      const response = await fetch(target, {
        headers: {
          Upgrade: "websocket",
          Connection: "Upgrade",
          ...(this.env.ASR_API_KEY
            ? { Authorization: `Token ${this.env.ASR_API_KEY}` }
            : {}),
        },
      });
      if (!response.webSocket) return false;
      const upstream = response.webSocket;
      this.upstream = upstream;
      upstream.accept();
      upstream.addEventListener("message", (event) => {
        if (this.upstream === upstream && this.client) {
          this.client.send(event.data);
          this.captureProviderSpeech(event.data);
        }
      });
      upstream.addEventListener("close", () => {
        if (this.upstream === upstream && this.client) {
          this.client.close(1011, "provider closed");
        }
      });
      upstream.addEventListener("error", () => {
        if (this.upstream === upstream && this.client) {
          this.client.close(1011, "provider error");
        }
      });
      this.meterSegments.clear();
      this.meterSourceId = `realtime:${crypto.randomUUID()}`;
      this.meterOccurredAt = Math.floor(Date.now() / 1000);
      this.meterRevision = 0;
      return true;
    } catch {
      return false;
    }
  }

  private captureProviderSpeech(data: unknown): void {
    if (!this.authContext || !this.meterSourceId) return;
    const segments = providerSegments(data);
    if (!segments.length) return;
    for (const segment of segments) {
      const key = `${segment.start}:${segment.end}`;
      this.meterSegments.set(key, segment);
      if (this.meterSegments.size > MAX_METERED_SEGMENTS) {
        this.client?.close(1013, "realtime speech meter full");
        this.upstream?.close(1013, "realtime speech meter full");
        return;
      }
    }
    const speechMs = speechMsFromTranscription({
      segments: [...this.meterSegments.values()],
    });
    if (speechMs <= 0) return;
    const snapshot: FairUseUsage = {
      uid: this.authContext.uid,
      sourceKind: "realtime",
      sourceId: this.meterSourceId,
      occurredAt: this.meterOccurredAt,
      speechMs,
      revision: ++this.meterRevision,
    };
    this.meterWriteChain = this.meterWriteChain
      .catch(() => undefined)
      .then(() => this.persistRealtimeUsage(snapshot));
    this.state.waitUntil(this.meterWriteChain);
  }

  private async persistRealtimeUsage(snapshot: FairUseUsage): Promise<void> {
    const pendingKey = pendingMeterKey(snapshot.sourceId);
    try {
      await recordFairUseUsage(this.env.APP_DB, snapshot);
      const pending = await this.state.storage.get<FairUseUsage>(pendingKey);
      if (
        pending?.sourceId === snapshot.sourceId &&
        (pending.revision || 1) <= (snapshot.revision || 1)
      ) {
        await this.state.storage.delete(pendingKey);
        recordFallback({
          component: "other",
          from: "d1",
          to: "durable_object_retry",
          reason: "dependency_unavailable",
          outcome: "recovered",
        });
      }
    } catch {
      const pending = await this.state.storage.get<FairUseUsage>(pendingKey);
      if (
        !pending ||
        pending.sourceId !== snapshot.sourceId ||
        (pending.revision || 1) <= (snapshot.revision || 1)
      ) {
        await this.state.storage.put(pendingKey, snapshot);
      }
      await this.state.storage.setAlarm(Date.now() + METER_RETRY_MS);
      recordFallback({
        component: "other",
        from: "d1",
        to: "durable_object_retry",
        reason: "dependency_unavailable",
        outcome: "degraded",
      });
    }
  }

  async alarm(): Promise<void> {
    const pending = await this.state.storage.list<FairUseUsage>({
      prefix: PENDING_METER_PREFIX,
    });
    for (const snapshot of pending.values()) {
      await this.persistRealtimeUsage(snapshot);
    }
  }

  private bufferMessage(socket: WebSocket, data: PendingMessage): void {
    const size = messageBytes(data);
    if (size > MAX_PENDING_AUDIO_BYTES) {
      socket.close(1009, "message too large");
      return;
    }
    if (this.pendingBytes + size > MAX_PENDING_AUDIO_BYTES) {
      this.sendJson(socket, { type: "backpressure", retryable: true });
      socket.close(1013, "realtime buffer full");
      return;
    }
    this.pending.push(data);
    this.pendingBytes += size;
  }

  private flushPending(): void {
    if (!this.upstream || this.upstream.readyState !== WebSocket.OPEN) return;
    for (const data of this.pending) this.upstream.send(data);
    this.pending = [];
    this.pendingBytes = 0;
  }

  private failAuthentication(socket: WebSocket, error: string): void {
    if (this.authTimeout) clearTimeout(this.authTimeout);
    this.authTimeout = undefined;
    this.sendJson(socket, { type: "auth_response", success: false, error });
    socket.close(4001, "authentication failed");
  }

  private sendJson(socket: WebSocket, value: unknown): void {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(value));
    }
  }

  private closeConnection(socket: WebSocket): void {
    if (this.client !== socket) return;
    if (this.authTimeout) clearTimeout(this.authTimeout);
    this.authTimeout = undefined;
    if (this.upstream && this.upstream.readyState < WebSocket.CLOSING) {
      this.upstream.close(1000, "client closed");
    }
    this.upstream = undefined;
    this.client = undefined;
    this.pending = [];
    this.pendingBytes = 0;
  }
}
