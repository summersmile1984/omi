import { describe, expect, it } from "vitest";
import { startInferenceControl } from "../contracts/inference-control.mjs";

describe("owned inference IO barrier", () => {
  it("holds actual HTTP inference until an explicitly observed release", async () => {
    const control = await startInferenceControl();
    try {
      const { id } = await (
        await fetch(control.origin + "/gates", { method: "POST" })
      ).json();
      const waiting = fetch(`${control.origin}/wait/${id}`);
      expect((await fetch(`${control.origin}/started/${id}`)).status).toBe(200);
      expect((await fetch(`${control.origin}/wait/${id}`)).status).toBe(409);
      expect(
        (await fetch(`${control.origin}/release/${id}`, { method: "POST" }))
          .status,
      ).toBe(200);
      expect((await waiting).status).toBe(200);
      expect((await fetch(`${control.origin}/wait/${id}`)).status).toBe(404);
    } finally {
      await control.close();
    }
  });
  it("closes pending real HTTP waits and bounds admission to eight owned gates", async () => {
    const control = await startInferenceControl();
    try {
      let id;
      for (let index = 0; index < 8; index++)
        ({ id } = await (
          await fetch(control.origin + "/gates", { method: "POST" })
        ).json());
      expect(
        (await fetch(control.origin + "/gates", { method: "POST" })).status,
      ).toBe(429);
      const waiting = fetch(`${control.origin}/wait/${id}`).then(
        (response) => response.status,
        () => "connection-closed",
      );
      expect((await fetch(`${control.origin}/started/${id}`)).status).toBe(200);
      await control.close();
      expect([503, "connection-closed"]).toContain(await waiting);
      await expect(
        fetch(control.origin + "/gates", { method: "POST" }),
      ).rejects.toThrow();
    } finally {
      await control.close();
    }
  });
});
