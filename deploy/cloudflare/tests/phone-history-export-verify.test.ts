import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import {
  applyReviewedPhoneHistoryPlan,
  verifyPhoneHistoryExport,
} from "../scripts/phone-history-export-verify.mjs";

const sourceExportSha256 = "b".repeat(64);
const phoneNumberHash = "a".repeat(64);
const ciphertext = `${btoa(String.fromCharCode(...new Uint8Array(12)))}.${btoa(
  String.fromCharCode(...new Uint8Array(32)),
)}`
  .replaceAll("+", "-")
  .replaceAll("/", "_")
  .replaceAll("=", "");

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, nested]) => `${JSON.stringify(key)}:${stableJson(nested)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function exportObject() {
  const row: Record<string, unknown> = {
    uid: "phone-export-user",
    source_record_id: "phone-1",
    phone_number_id: "phone-1",
    phone_number_hash: phoneNumberHash,
    phone_number_ciphertext: ciphertext,
    twilio_sid: "PN1234567890",
    friendly_name: "home",
    verified_at: 1_700_000_000,
    is_primary: true,
    account_generation: 3,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_001,
    status: "verified",
  };
  const source = {
    kind: "firestore",
    collection: "users/{uid}/phone_numbers",
    ciphertext_scheme: "cloudflare-phone-aes-gcm-v1",
    proof_scheme: "sha256-v1",
    export_sha256: sourceExportSha256,
  };
  const sourceFingerprint = sha256(
    stableJson({
      collection: source.collection,
      export_sha256: source.export_sha256,
      uid: row.uid,
      source_record_id: row.source_record_id,
      phone_number_hash: row.phone_number_hash,
      twilio_sid: row.twilio_sid,
      friendly_name: row.friendly_name,
      verified_at: row.verified_at,
      is_primary: 1,
      account_generation: row.account_generation,
      created_at: row.created_at,
      updated_at: row.updated_at,
    }),
  );
  row.source_fingerprint = sourceFingerprint;
  row.proof = {
    kind: "verified-e164",
    method: "twilio-outgoing-caller-id",
    canonicalization: "E.164",
    verified: true,
    value_sha256: phoneNumberHash,
    source_fingerprint: sourceFingerprint,
    proof_sha256: sha256(
      stableJson({
        kind: "verified-e164",
        method: "twilio-outgoing-caller-id",
        canonicalization: "E.164",
        verified: true,
        value_sha256: phoneNumberHash,
        source_fingerprint: sourceFingerprint,
        attested_at: 1_700_000_002,
      }),
    ),
    attested_at: 1_700_000_002,
  };
  return { schema_version: 1, source, rows: [row] };
}

function bytes(): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(exportObject()));
}

function checksum(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

describe("Phone history export verification and execution tool", () => {
  it("binds the plan to the exact manifest bytes while retaining source attestation", () => {
    const input = bytes();
    const fileSha256 = checksum(input);
    const result = verifyPhoneHistoryExport(input, {
      expectedSha256: fileSha256,
    });
    expect(result).toMatchObject({
      verified: true,
      export_bytes: input.byteLength,
      export_sha256: fileSha256,
      source_export_sha256: sourceExportSha256,
    });
    expect(result.plan).toMatchObject({
      source: { export_sha256: sourceExportSha256 },
      total: 1,
      stage: 1,
      blocked: 0,
    });
  });

  it("rejects checksum mismatches, malformed UTF-8, and missing source attestation", () => {
    const input = bytes();
    expect(() =>
      verifyPhoneHistoryExport(input, { expectedSha256: "c".repeat(64) }),
    ).toThrow("does not match");
    expect(() =>
      verifyPhoneHistoryExport(new Uint8Array([0xff, 0xfe])),
    ).toThrow("UTF-8 JSON");
    const withoutSource = exportObject();
    delete (withoutSource.source as Record<string, unknown>).export_sha256;
    expect(() =>
      verifyPhoneHistoryExport(
        new TextEncoder().encode(JSON.stringify(withoutSource)),
      ),
    ).toThrow("attested phone export checksum");
  });

  it("sends only opaque identifiers through review/apply and requires an attested plan", async () => {
    const input = bytes();
    const plan = verifyPhoneHistoryExport(input, {
      expectedSha256: checksum(input),
    }).plan;
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = vi.fn(
      async (url: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ url: String(url), init });
        if (requests.length === 1)
          return new Response(
            JSON.stringify({
              review_id: "00000000-0000-4000-8000-000000000000",
            }),
            { status: 201 },
          );
        return new Response(
          JSON.stringify({
            status: "applied",
            manifest_sha256: plan.manifest_sha256,
            entry_count: 1,
            applied_count: 1,
            already_applied_count: 0,
          }),
          { status: 200 },
        );
      },
    ) as unknown as typeof fetch;

    await expect(
      applyReviewedPhoneHistoryPlan(
        { ...plan, blocked: 1 },
        {
          endpoint: "https://jobs.test/internal/phone-history/reviews",
          adminKey: "secret",
          fetchImpl,
        },
      ),
    ).rejects.toThrow("all-stage");

    const result = await applyReviewedPhoneHistoryPlan(plan, {
      endpoint: "https://jobs.test/internal/phone-history/reviews/",
      adminKey: "secret",
      fetchImpl,
    });
    expect(result).toMatchObject({ status: "applied", entry_count: 1 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(requests[0].url).toBe(
      "https://jobs.test/internal/phone-history/reviews",
    );
    const body = JSON.parse(String(requests[0].init?.body)) as Record<
      string,
      unknown
    >;
    expect(body).toEqual({
      manifest_sha256: plan.manifest_sha256,
      entries: [
        {
          uid: "phone-export-user",
          import_id: plan.entries[0].importId,
          plan_hash: plan.entries[0].planHash,
        },
      ],
    });
    expect(JSON.stringify(body)).not.toContain(ciphertext);
    expect(requests[0].init?.headers).toMatchObject({
      "secret-key": "secret",
    });
    expect(JSON.stringify(result)).not.toContain("secret");
  });
});
