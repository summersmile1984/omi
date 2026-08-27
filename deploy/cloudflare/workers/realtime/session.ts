import type { RealtimeEnv } from "./env";

export class RealtimeSession {
  private state: DurableObjectState;
  private env: RealtimeEnv;
  private client?: WebSocket;
  private upstream?: WebSocket;

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
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    server.accept();
    // `server` is the accepted side inside the Durable Object. The `client`
    // side is returned to the caller and must never be used for sends from
    // this Worker; keeping the wrong side here makes provider events vanish
    // or throw in production runtimes.
    this.client = server;
    server.addEventListener("message", (event) =>
      this.onClientMessage(event.data),
    );
    server.addEventListener("close", () => this.closeUpstream());
    server.addEventListener("error", () => this.closeUpstream());
    this.state.waitUntil(this.connectUpstream());
    server.send(
      JSON.stringify({
        type: "ready",
        provider: this.env.ASR_WS_URL ? "external" : "unconfigured",
      }),
    );
    return new Response(null, { status: 101, webSocket: client });
  }

  private async connectUpstream(): Promise<void> {
    if (!this.env.ASR_WS_URL || !this.client) return;
    const response = await fetch(this.env.ASR_WS_URL, {
      headers: {
        Upgrade: "websocket",
        Connection: "Upgrade",
        ...(this.env.ASR_API_KEY
          ? { Authorization: `Token ${this.env.ASR_API_KEY}` }
          : {}),
      },
    });
    if (!response.webSocket)
      throw new Error("ASR provider did not return a WebSocket");
    this.upstream = response.webSocket;
    this.upstream.accept();
    this.upstream.addEventListener("message", (event) =>
      this.client?.send(event.data),
    );
    this.upstream.addEventListener("close", () =>
      this.client?.close(1011, "provider closed"),
    );
    this.upstream.addEventListener("error", () =>
      this.client?.close(1011, "provider error"),
    );
  }

  private onClientMessage(data: string | ArrayBuffer): void {
    if (!this.upstream || this.upstream.readyState !== WebSocket.OPEN) {
      this.client?.send(JSON.stringify({ type: "provider_unavailable" }));
      return;
    }
    this.upstream.send(data);
  }

  private closeUpstream(): void {
    if (this.upstream && this.upstream.readyState < WebSocket.CLOSING)
      this.upstream.close(1000, "client closed");
    this.upstream = undefined;
  }
}
