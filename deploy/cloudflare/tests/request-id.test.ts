import { describe, expect, it } from "vitest";
import { withRequestId } from "../workers/shared/request-id";

describe("request id response decoration", () => {
  it("preserves the websocket during a 101 upgrade", () => {
    const runtime = globalThis as unknown as { Response: typeof Response };
    const originalResponse = runtime.Response;
    const socket = { marker: "accepted-server" };
    class TestResponse {
      readonly status: number;
      readonly statusText: string;
      readonly body = null;
      readonly headers: Headers;
      readonly webSocket?: object;

      constructor(
        _body: unknown,
        init: {
          status?: number;
          statusText?: string;
          headers?: Headers;
          webSocket?: object;
        } = {},
      ) {
        this.status = init.status || 200;
        this.statusText = init.statusText || "";
        this.headers = init.headers || new Headers();
        this.webSocket = init.webSocket;
      }
    }
    runtime.Response = TestResponse as unknown as typeof Response;
    try {
      const response = withRequestId(
        new TestResponse(null, {
          status: 101,
          webSocket: socket,
        }) as unknown as Response,
        "request-123",
      ) as unknown as { status: number; headers: Headers; webSocket?: object };
      expect(response.status).toBe(101);
      expect(response.webSocket).toBe(socket);
      expect(response.headers.get("x-request-id")).toBe("request-123");
    } finally {
      runtime.Response = originalResponse;
    }
  });
});
