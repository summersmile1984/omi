import { decodeAuthContext, type AuthContext } from "../shared/auth-context";
import {
  recordFairUseUsage,
  speechMsFromTranscription,
  type FairUseUsage,
} from "../shared/fair-use-meter";
import { readFairUseRestriction } from "../shared/fair-use-enforcement";
import { recordFallback } from "../shared/fallback";
import { defaultStreamingPolicy } from "../shared/provider-policy";
import { verifyRealtimeTicket } from "../shared/realtime-ticket";
import type { RealtimeEnv } from "./env";

const AUTH_TIMEOUT_MS = 15_000;
const MAX_AUTH_MESSAGE_BYTES = 16_384;
const MAX_PENDING_AUDIO_BYTES = 1_000_000;
const MAX_METERED_SEGMENTS = 20_000;
const METER_RETRY_MS = 10_000;
const POLICY_CHECK_INTERVAL_MS = 5 * 60 * 1_000;
const PENDING_METER_PREFIX = "fair-use-pending:";

type PendingMessage = string | ArrayBuffer;
type ClientMessage = PendingMessage | ArrayBufferView | Blob;
type MeterSegment = { start: number; end: number; text: string };
type AudioTransform = "none" | "pcm8-to-linear16";
type UpstreamProvider = "workers-ai" | "external";
type NativeStreamingConfig = {
  inputs: Record<string, unknown>;
  audioTransform: AudioTransform;
};

function pendingMeterKey(sourceId: string): string {
  return `${PENDING_METER_PREFIX}${sourceId}`;
}

type FirstMessageAuth = {
  type: "auth";
  ticket: string;
  deviceIdHash?: string;
};

function messageBytes(data: ClientMessage): number {
  if (typeof data === "string") {
    return new TextEncoder().encode(data).byteLength;
  }
  if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
    return data.byteLength;
  }
  return data.size;
}

async function normalizeClientMessage(
  data: ClientMessage,
): Promise<PendingMessage | null> {
  if (typeof data === "string" || data instanceof ArrayBuffer) return data;
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength).slice()
      .buffer;
  }
  try {
    return await data.arrayBuffer();
  } catch {
    return null;
  }
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

function providerText(data: unknown): string | null {
  if (typeof data === "string") return data;
  if (data instanceof ArrayBuffer) {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(data);
    } catch {
      return null;
    }
  }
  return null;
}

function providerReportedError(data: unknown): boolean {
  const text = providerText(data);
  if (text === null) return false;
  try {
    const payload = JSON.parse(text) as { type?: unknown };
    return payload?.type === "Error";
  } catch {
    return false;
  }
}

function providerSegments(data: unknown): MeterSegment[] {
  const text = providerText(data);
  if (text === null) return [];
  let payload: unknown;
  try {
    payload = JSON.parse(text);
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
        : typeof segment.punctuated_word === "string"
          ? segment.punctuated_word
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

function boundedInteger(
  raw: string | null,
  fallback: number,
  minimum: number,
  maximum: number,
): number | null {
  if (raw === null || raw === "") return fallback;
  if (!/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : null;
}

function nativeStreamingConfig(
  requestUrl: string,
): NativeStreamingConfig | null {
  const incoming = new URL(requestUrl);
  const isPtt = incoming.pathname === "/v2/voice-message/transcribe-stream";
  const codec = (
    incoming.searchParams.get("codec") || (isPtt ? "linear16" : "pcm8")
  ).toLowerCase();
  const sampleRate = boundedInteger(
    incoming.searchParams.get("sample_rate"),
    isPtt ? 16_000 : 8_000,
    8_000,
    48_000,
  );
  const channels = boundedInteger(
    incoming.searchParams.get("channels"),
    1,
    1,
    2,
  );
  // The Nova-3 binding currently rejects its documented `channels` and
  // `multichannel` WebSocket parameters. Keep stereo on the compatibility
  // provider instead of opening a native stream with ambiguous channel audio.
  if (sampleRate === null || channels !== 1) return null;

  let encoding: "linear16" | "mulaw";
  let audioTransform: AudioTransform = "none";
  if (codec === "linear16" || codec === "pcm16") {
    encoding = "linear16";
  } else if (codec === "pcm8") {
    encoding = "linear16";
    audioTransform = "pcm8-to-linear16";
  } else if (codec === "mulaw" || codec === "mulaw8" || codec === "mulaw16") {
    encoding = "mulaw";
  } else {
    // AAC, LC3 and device-frame Opus require the legacy decode/framing path.
    return null;
  }

  const language = incoming.searchParams.get("language") || "en";
  if (language.length > 35 || !/^[A-Za-z0-9-]+$/.test(language)) return null;
  return {
    inputs: {
      encoding,
      // Cloudflare's WebSocket binding accepts the Deepgram query value as a
      // string (matching its official Flux binding example), while a number is
      // rejected before the socket upgrade.
      sample_rate: String(sampleRate),
      language,
      endpointing: "300",
      mip_opt_out: true,
    },
    audioTransform,
  };
}

function pcm8ToLinear16(data: ArrayBuffer): ArrayBuffer {
  const source = new Uint8Array(data);
  const target = new ArrayBuffer(source.byteLength * 2);
  const view = new DataView(target);
  for (let index = 0; index < source.byteLength; index += 1) {
    view.setInt16(index * 2, (source[index] - 128) << 8, true);
  }
  return target;
}

function recordProviderConnectionFailure(detail: {
  status?: number;
  error?: unknown;
}): void {
  const error = detail.error;
  const message =
    error instanceof Error
      ? error.message.slice(0, 240).replace(/[^A-Za-z0-9 _.:/@-]/g, "?")
      : undefined;
  console.warn(
    JSON.stringify({
      event: "stt_provider_connection",
      provider: "workers-ai",
      model: defaultStreamingPolicy.model,
      outcome: "failed",
      ...(detail.status ? { status: detail.status } : {}),
      ...(error instanceof Error ? { error_type: error.name } : {}),
      ...(message ? { message } : {}),
    }),
  );
}

function nativeClientMessage(data: unknown): string | null {
  const text = providerText(data);
  if (text === null) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object") return null;
  const object = payload as Record<string, unknown>;
  // Workers AI's direct Nova-3 binding omits Deepgram's `type: "Results"`
  // discriminator even though the AI Gateway WebSocket surface includes it.
  // The final flag plus the channel/alternatives shape is the shared contract.
  if (object.is_final !== true) return null;
  if (!object.channel || typeof object.channel !== "object") return null;
  const alternatives = (object.channel as Record<string, unknown>).alternatives;
  if (
    !Array.isArray(alternatives) ||
    !alternatives[0] ||
    typeof alternatives[0] !== "object"
  ) {
    return null;
  }
  const alternative = alternatives[0] as Record<string, unknown>;
  const words = Array.isArray(alternative.words) ? alternative.words : [];
  const segments: Array<{
    speaker: string;
    start: number;
    end: number;
    text: string;
    is_user: false;
    person_id: null;
  }> = [];
  for (const value of words) {
    if (!value || typeof value !== "object") continue;
    const word = value as Record<string, unknown>;
    if (
      typeof word.start !== "number" ||
      typeof word.end !== "number" ||
      !Number.isFinite(word.start) ||
      !Number.isFinite(word.end) ||
      word.start < 0 ||
      word.end <= word.start
    ) {
      continue;
    }
    const text =
      typeof word.punctuated_word === "string"
        ? word.punctuated_word
        : typeof word.word === "string"
          ? word.word
          : "";
    if (!text.trim()) continue;
    const speaker =
      typeof word.speaker === "number" && Number.isInteger(word.speaker)
        ? Math.max(0, word.speaker)
        : 0;
    const speakerLabel = `SPEAKER_${String(speaker).padStart(2, "0")}`;
    const previous = segments.at(-1);
    if (previous?.speaker === speakerLabel) {
      previous.end = word.end;
      previous.text = `${previous.text} ${text}`;
    } else {
      segments.push({
        speaker: speakerLabel,
        start: word.start,
        end: word.end,
        text,
        is_user: false,
        person_id: null,
      });
    }
  }
  if (!segments.length) {
    const transcript =
      typeof alternative.transcript === "string"
        ? alternative.transcript.trim()
        : "";
    const start = typeof object.start === "number" ? object.start : 0;
    const duration = typeof object.duration === "number" ? object.duration : 0;
    if (!transcript || duration <= 0) return null;
    segments.push({
      speaker: "SPEAKER_00",
      start,
      end: start + duration,
      text: transcript,
      is_user: false,
      person_id: null,
    });
  }
  return JSON.stringify(segments);
}

export class RealtimeSession {
  private state: DurableObjectState;
  private env: RealtimeEnv;
  private client?: WebSocket;
  private upstream?: WebSocket;
  private upstreamAudioTransform: AudioTransform = "none";
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
  private clientMessageChain: Promise<void> = Promise.resolve();
  private nextPolicyCheckAt = 0;

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
    this.clientMessageChain = Promise.resolve();
    this.requestUrl = request.url;
    this.firstMessageAuth = firstMessageAuth;
    if (firstMessageAuth) this.webBootstrapClaimed = true;
    this.authContext = this.firstMessageAuth
      ? undefined
      : decodeAuthContext(request.headers.get("x-omi-auth-context")) ||
        undefined;

    server.addEventListener("message", (event) => {
      this.clientMessageChain = this.clientMessageChain
        .catch(() => undefined)
        .then(() => this.onClientMessage(server, event.data as ClientMessage));
      this.state.waitUntil(this.clientMessageChain);
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
    data: ClientMessage,
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
      if (!(await this.enforceFairUse(socket, true))) return;
      const provider = await this.connectUpstream(auth.deviceIdHash);
      if (this.client !== socket) return;
      if (!provider) {
        this.sendJson(socket, {
          type: "auth_response",
          success: false,
          error: "provider_unavailable",
        });
        socket.close(1013, "provider unavailable");
        return;
      }
      this.sendJson(socket, { type: "auth_response", success: true });
      this.sendJson(socket, { type: "ready", provider });
      this.flushPending();
      return;
    }

    if (!(await this.enforceFairUse(socket, false))) return;
    const size = messageBytes(data);
    if (size > MAX_PENDING_AUDIO_BYTES) {
      socket.close(1009, "message too large");
      return;
    }
    const normalized = await normalizeClientMessage(data);
    if (normalized === null) {
      socket.close(1003, "unsupported message data");
      return;
    }
    if (!this.upstream || this.upstream.readyState !== WebSocket.OPEN) {
      this.bufferMessage(socket, normalized);
      return;
    }
    this.sendUpstream(normalized);
  }

  private async startProvider(
    socket: WebSocket,
    sendAuthResponse: boolean,
  ): Promise<void> {
    if (!(await this.enforceFairUse(socket, sendAuthResponse))) return;
    const provider = await this.connectUpstream();
    if (this.client !== socket) return;
    if (!provider) {
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
    this.sendJson(socket, { type: "ready", provider });
    this.flushPending();
  }

  private async enforceFairUse(
    socket: WebSocket,
    sendAuthResponse: boolean,
  ): Promise<boolean> {
    if (!this.authContext || Date.now() < this.nextPolicyCheckAt) return true;
    this.nextPolicyCheckAt = Date.now() + POLICY_CHECK_INTERVAL_MS;
    const restriction = await readFairUseRestriction(
      this.env.APP_DB,
      this.authContext.uid,
    );
    if (!restriction || this.client !== socket) return true;
    if (sendAuthResponse) {
      this.sendJson(socket, {
        type: "auth_response",
        success: false,
        error: "fair_use_restricted",
        retry_after: restriction.retryAfter,
      });
    } else {
      this.sendJson(socket, {
        type: "fair_use_restricted",
        retry_after: restriction.retryAfter,
      });
    }
    socket.close(4008, "fair use restricted");
    return false;
  }

  private async connectUpstream(
    deviceIdHash?: string,
  ): Promise<UpstreamProvider | null> {
    if (!this.client || !this.authContext || !this.requestUrl) return null;
    const nativeConfig = nativeStreamingConfig(this.requestUrl);
    if (nativeConfig && this.env.AI) {
      try {
        const response = await this.env.AI.run(
          defaultStreamingPolicy.model,
          nativeConfig.inputs,
          { websocket: true, tags: ["omi-realtime"] },
        );
        if (response.webSocket) {
          this.attachUpstream(
            response.webSocket,
            "workers-ai",
            nativeConfig.audioTransform,
          );
          return "workers-ai";
        }
        recordProviderConnectionFailure({ status: response.status });
      } catch (error) {
        // Connection setup contains no audio or user identifier, so a bounded,
        // character-sanitized provider error is safe enough to diagnose model
        // contract drift without exposing request content.
        recordProviderConnectionFailure({ error });
      }
    }

    const externalReady = await this.connectExternalUpstream(deviceIdHash);
    if (nativeConfig) {
      recordFallback({
        component: "stt",
        from: "workers_ai_streaming",
        to: externalReady ? "deepgram_cloud" : "none",
        reason: "dependency_unavailable",
        outcome: externalReady ? "recovered" : "exhausted",
      });
    }
    return externalReady ? "external" : null;
  }

  private async connectExternalUpstream(
    deviceIdHash?: string,
  ): Promise<boolean> {
    if (!this.env.ASR_WS_URL || !this.authContext || !this.requestUrl) {
      return false;
    }
    try {
      const incoming = new URL(this.requestUrl);
      const target = new URL(this.env.ASR_WS_URL);
      for (const name of [
        "language",
        "sample_rate",
        "codec",
        "channels",
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
      this.attachUpstream(response.webSocket, "external", "none");
      return true;
    } catch {
      return false;
    }
  }

  private attachUpstream(
    upstream: WebSocket,
    provider: UpstreamProvider,
    audioTransform: AudioTransform,
  ): void {
    this.upstream = upstream;
    this.upstreamAudioTransform = audioTransform;
    upstream.accept();
    upstream.addEventListener("message", (event) => {
      if (this.upstream === upstream && this.client) {
        if (provider === "workers-ai" && providerReportedError(event.data)) {
          this.sendJson(this.client, { type: "provider_unavailable" });
          this.client.close(1011, "provider error");
          upstream.close(1011, "provider error");
          return;
        }
        this.captureProviderSpeech(event.data);
        const clientMessage =
          provider === "workers-ai"
            ? nativeClientMessage(event.data)
            : event.data;
        if (clientMessage !== null) this.client.send(clientMessage);
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
  }

  private sendUpstream(data: PendingMessage): void {
    if (!this.upstream || this.upstream.readyState !== WebSocket.OPEN) return;
    this.upstream.send(
      this.upstreamAudioTransform === "pcm8-to-linear16" &&
        data instanceof ArrayBuffer
        ? pcm8ToLinear16(data)
        : data,
    );
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
    for (const data of this.pending) this.sendUpstream(data);
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
    this.upstreamAudioTransform = "none";
    this.client = undefined;
    this.pending = [];
    this.pendingBytes = 0;
    this.clientMessageChain = Promise.resolve();
    this.nextPolicyCheckAt = 0;
  }
}
