/** Same HMAC domain as the upstream canonical privacy deletion receipt. */
export async function memoryPrivacyReceiptId(
  secret: string | undefined,
  uid: string,
  memoryId: string,
): Promise<string> {
  const encoder = new TextEncoder();
  if (typeof secret !== "string" || encoder.encode(secret).length < 32)
    throw new Error("memory privacy receipt secret is unavailable");
  if (!uid || !memoryId || uid.includes("\n") || memoryId.includes("\n"))
    throw new Error("invalid memory privacy identity");
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign(
    "HMAC",
    key,
    encoder.encode(`memory-privacy-receipt.v2\n${uid}\n${memoryId}`),
  );
  return (
    "receipt_" +
    Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("")
  );
}
