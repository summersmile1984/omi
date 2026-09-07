// Bound intake before the Python ASGI bridge receives the request body.
export const MAX_MEMORY_BATCH_BYTES = 1_000_000;

function tooLarge(): Response {
  return Response.json(
    {
      error: "memory_batch_too_large",
      max_bytes: MAX_MEMORY_BATCH_BYTES,
      max_memories: 100,
    },
    { status: 413 }
  );
}

export async function memoryBatchRequest(
  request: Request
): Promise<Request | Response> {
  if (Number(request.headers.get("content-length")) > MAX_MEMORY_BATCH_BYTES) {
    await request.body?.cancel();
    return tooLarge();
  }
  if (!request.body) return request;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.byteLength;
      if (length > MAX_MEMORY_BATCH_BYTES) {
        await reader.cancel();
        return tooLarge();
      }
      chunks.push(next.value);
    }
  } catch {
    return Response.json(
      { error: "invalid_memory_batch_body" },
      { status: 400 }
    );
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const headers = new Headers(request.headers);
  headers.set("content-length", String(length));
  return new Request(request.url, { method: request.method, headers, body });
}
