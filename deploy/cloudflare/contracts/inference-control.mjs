import { randomUUID } from "node:crypto";
import { createServer } from "node:http";

// A bounded model-IO seam only. Application/auth/storage requests still traverse
// their actual Workers. The runner owns this listener and every pending wait.
export async function startInferenceControl() {
  const gates = new Map();
  function retire(id, status) {
    const gate = gates.get(id);
    if (!gate) return;
    clearTimeout(gate.timer);
    for (const response of [gate.wait, gate.ready])
      if (response && !response.writableEnded) {
        response.writeHead(status);
        response.end();
      }
    gates.delete(id);
  }
  const server = createServer((request, response) => {
    request.resume();
    if (request.method === "POST" && request.url === "/gates") {
      if (gates.size >= 8) {
        response.writeHead(429).end();
        return;
      }
      const id = randomUUID();
      gates.set(id, { timer: setTimeout(() => retire(id, 504), 30000) });
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ id }));
      return;
    }
    const match = /^\/(wait|started|release)\/([0-9a-f-]{36})$/.exec(
      request.url,
    );
    const gate = match && gates.get(match[2]);
    if (!gate) {
      response.writeHead(404).end();
      return;
    }
    if (request.method === "GET" && match[1] === "wait" && !gate.wait) {
      gate.wait = response;
      if (gate.ready) gate.ready.writeHead(200).end();
    } else if (
      request.method === "GET" &&
      match[1] === "started" &&
      !gate.ready
    ) {
      gate.ready = response;
      if (gate.wait) response.writeHead(200).end();
    } else if (
      request.method === "POST" &&
      match[1] === "release" &&
      gate.wait
    ) {
      retire(match[2], 200);
      response.writeHead(200).end();
    } else response.writeHead(409).end();
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    async close() {
      for (const id of gates.keys()) retire(id, 503);
      const done = new Promise((resolve) => server.close(resolve));
      server.closeAllConnections();
      await done;
    },
  };
}
