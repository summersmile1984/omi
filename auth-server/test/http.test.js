import assert from "node:assert/strict";
import { test } from "node:test";
import { betterAuthBridge } from "../src/http.js";

test("a rejected Better Auth handler returns a bounded retryable 503", async () => {
  const response = {
    headers: {},
    headersSent: false,
    statusCode: 0,
    body: null,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    status(value) {
      this.statusCode = value;
      return this;
    },
    json(value) {
      this.body = value;
      return this;
    },
    destroy() {
      throw new Error("response should not be destroyed before headers are sent");
    },
  };
  const handler = betterAuthBridge(async () => {
    throw new Error("controlled database outage");
  }, "https://auth.example.test");

  let timeout;
  try {
    await Promise.race([
      handler(
        {
          originalUrl: "/api/auth/token",
          headers: {},
          method: "GET",
        },
        response,
      ),
      new Promise((_, reject) => {
        timeout = setTimeout(
          () => reject(new Error("handler did not settle")),
          2_000,
        );
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }

  assert.equal(response.statusCode, 503);
  assert.deepEqual(response.headers, {
    "cache-control": "no-store",
    "retry-after": "1",
  });
  assert.deepEqual(response.body, { error: "identity_store_unavailable" });
});
