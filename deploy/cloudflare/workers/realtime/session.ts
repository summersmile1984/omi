import { decodeAuthContext, type AuthContext } from "../shared/auth-context";
import type { RealtimeEnv } from "./env";

const AUTH_TIMEOUT_MS = 15_000;
const MAX_AUTH_MESSAGE_BYTES = 16_384;
const MAX_PENDING_AUDIO_BYTES = 1_000_000;

type PendingMessage = string | ArrayBuffer;

type FirstMessageAuth = {
  type: "auth";
  token: string;
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
      token?: unknown;
      device_id_hash?: unknown;
    };
    if (
      value.type !== "auth" ||
      typeof value.token !== "string" ||
      value.token.length < 1 ||
      value.token.length > 16_384 ||
      (value.device_id_hash !== undefined &&
        (typeof value.device_id_hash !== "string" ||
          value.device_id_hash.length > 256))
    ) {
      return null;
    }
    return {
      type: "auth",
      token: value.token,
      deviceIdHash: value.device_id_hash,
    };
  } catch {
    return null;
  }
}

export class RealtimeSession {
  private state: DurableObjectState;
  private env: RealtimeEnv;
  private client?: WebSocket;
  private upstream?: WebSocket;
  private authContext?: AuthContext;
  private requestUrl?: string;
  private requestId = "realtime";
  private firstMessageAuth = false;
  private authInFlight = false;
  private authTimeout?: ReturnType<typeof setTimeout>;
  private pending: PendingMessage[] = [];
  private pendingBytes = 0;
  private webBootstrapClaimed = false;

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
    this.requestId = request.headers.get("x-request-id") || "realtime";
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
      const context = await this.verifyBearer(auth.token);
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

  private async verifyBearer(token: string): Promise<AuthContext | null> {
    if (!this.env.AUTH || !this.env.INTERNAL_ASSERTION_SECRET) return null;
    try {
      const response = await this.env.AUTH.fetch(
        new Request("https://auth.internal/internal/verify", {
          method: "POST",
          headers: {
            authorization: `Bearer ${token}`,
            "x-internal-assertion-secret": this.env.INTERNAL_ASSERTION_SECRET,
            "x-request-id": this.requestId,
          },
        }),
      );
      if (!response.ok) return null;
      const value = (await response.json()) as Partial<AuthContext>;
      if (
        typeof value.uid !== "string" ||
        !value.uid ||
        (value.authority !== "firebase" &&
          value.authority !== "better-auth" &&
          value.authority !== "internal")
      ) {
        return null;
      }
      return {
        uid: value.uid,
        authority: value.authority,
        sessionGeneration: value.sessionGeneration,
        requestId: this.requestId,
      };
    } catch {
      return null;
    }
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
      return true;
    } catch {
      return false;
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
