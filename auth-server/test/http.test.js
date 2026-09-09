import assert from "node:assert/strict";
import { test } from "node:test";
import { betterAuthBridge } from "../src/http.js";

function responseCapture() {
  return {
    headers: {},
    headersSent: false,
    statusCode: 200,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    vary(value) {
      this.headers.vary = value;
    },
    status(value) {
      this.statusCode = value;
      return this;
    },
    send(value) {
      this.body = value;
      return this;
    },
    json(value) {
      this.body = value;
      return this;
    },
    end() {
      return this;
    },
  };
}

test("independent cookies remain separate through the HTTP bridge", async () => {
  // Set-Cookie is not a comma-separated header; Expires itself contains a comma.
  // https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie
  const cookies = [
    "session=synthetic; Path=/; HttpOnly",
    "state=synthetic; Path=/; Expires=Wed, 09 Jun 2027 10:18:14 GMT; HttpOnly",
  ];
  const headers = new Headers();
  for (const cookie of cookies) headers.append("set-cookie", cookie);
  const handler = betterAuthBridge(
    async () => new Response("{}", { headers }),
    "https://auth.fixture.invalid",
  );
  const response = responseCapture();
  await handler(
    { method: "GET", originalUrl: "/api/auth/get-session", headers: {} },
    response,
  );
  assert.deepEqual(response.headers["set-cookie"], cookies);
});

test("a trusted Web origin can preflight and read the bearer session header", async () => {
  let calls = 0;
  const handler = betterAuthBridge(
    async () => {
      calls++;
      return new Response("{}", {
        headers: {
          "set-auth-token": "synthetic-session",
          "access-control-expose-headers": "plugin-header",
        },
      });
    },
    "https://auth.fixture.invalid",
    ["https://web.fixture.invalid"],
  );
  const headers = { origin: "https://web.fixture.invalid" };
  const preflight = responseCapture();
  await handler({ method: "OPTIONS", headers }, preflight);
  assert.equal(calls, 0);
  assert.equal(preflight.statusCode, 204);
  assert.equal(
    preflight.headers["access-control-allow-origin"],
    headers.origin,
  );
  assert.equal(
    preflight.headers["access-control-allow-headers"],
    "authorization, content-type",
  );
  const response = responseCapture();
  await handler(
    {
      method: "POST",
      headers,
      originalUrl: "/api/auth/sign-in/email",
      body: {},
    },
    response,
  );
  assert.equal(calls, 1);
  assert.equal(
    response.headers["access-control-expose-headers"],
    "set-auth-token, set-auth-jwt",
  );
  assert.equal(response.headers["set-auth-token"], "synthetic-session");
  assert.equal(response.headers["access-control-allow-credentials"], "true");
});

test("an untrusted origin receives no cross-origin permission", async () => {
  const handler = betterAuthBridge(
    async () => new Response("{}"),
    "https://auth.fixture.invalid",
    ["https://web.fixture.invalid"],
  );
  for (const method of ["OPTIONS", "GET"]) {
    const response = responseCapture();
    await handler(
      {
        method,
        originalUrl: "/api/auth/get-session",
        headers: {
          origin: "https://web.fixture.invalid.attacker.invalid",
        },
      },
      response,
    );
    assert.equal(response.headers["access-control-allow-origin"], undefined);
    assert.equal(response.headers["access-control-allow-headers"], undefined);
    assert.equal(response.headers.vary, "Origin");
  }
});

test("a retryable auth outage remains readable to the trusted browser", async () => {
  const handler = betterAuthBridge(
    async () => {
      throw new Error("controlled outage");
    },
    "https://auth.fixture.invalid",
    ["https://web.fixture.invalid"],
  );
  const response = responseCapture();
  await handler(
    {
      method: "GET",
      originalUrl: "/api/auth/token",
      headers: {
        origin: "https://web.fixture.invalid",
      },
    },
    response,
  );
  assert.equal(response.statusCode, 503);
  assert.equal(
    response.headers["access-control-allow-origin"],
    "https://web.fixture.invalid",
  );
  assert.equal(response.headers["retry-after"], "1");
});

test("a rejected Better Auth handler returns a bounded retryable 503", async () => {
  const response = {
    headers: {},
    headersSent: false,
    statusCode: 0,
    body: null,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    vary(value) {
      this.headers.vary = value;
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
      throw new Error(
        "response should not be destroyed before headers are sent",
      );
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
    vary: "Origin",
    "cache-control": "no-store",
    "retry-after": "1",
  });
  assert.deepEqual(response.body, { error: "identity_store_unavailable" });
});
