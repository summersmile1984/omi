import { describe, expect, it } from "vitest";
import {
  DEFAULT_FAIR_USE_CAPS,
  UNLIMITED_FAIR_USE_CAPS,
  capsForPlan,
  chooseFairUseTransition,
  fairUseClassifierInput,
  parseFairUseClassifierResponse,
  triggeredFairUseCaps,
} from "../workers/shared/fair-use-policy";

describe("fair-use policy", () => {
  it("uses strict soft-cap comparisons and the raised unlimited-family caps", () => {
    expect(capsForPlan("basic")).toEqual(DEFAULT_FAIR_USE_CAPS);
    expect(capsForPlan("plus")).toEqual(DEFAULT_FAIR_USE_CAPS);
    expect(capsForPlan("architect")).toEqual(UNLIMITED_FAIR_USE_CAPS);
    expect(
      triggeredFairUseCaps(
        {
          daily: DEFAULT_FAIR_USE_CAPS.daily,
          threeDay: DEFAULT_FAIR_USE_CAPS.threeDay,
          weekly: DEFAULT_FAIR_USE_CAPS.weekly,
        },
        DEFAULT_FAIR_USE_CAPS,
      ),
    ).toEqual([]);
    expect(
      triggeredFairUseCaps(
        {
          daily: DEFAULT_FAIR_USE_CAPS.daily + 1,
          threeDay: DEFAULT_FAIR_USE_CAPS.threeDay + 1,
          weekly: DEFAULT_FAIR_USE_CAPS.weekly + 1,
        },
        DEFAULT_FAIR_USE_CAPS,
      ),
    ).toEqual(["daily", "3day", "weekly"]);
  });

  it("preserves the graduated classifier-gated transition contract", () => {
    expect(chooseFairUseTransition("none", 0, 0.69)).toEqual({
      action: "none",
      nextStage: "none",
    });
    expect(chooseFairUseTransition("none", 0, 0.7)).toEqual({
      action: "warning",
      nextStage: "warning",
    });
    expect(chooseFairUseTransition("warning", 1, 1)).toEqual({
      action: "none",
      nextStage: "warning",
    });
    expect(chooseFairUseTransition("warning", 2, 1)).toEqual({
      action: "throttle",
      nextStage: "throttle",
    });
    expect(chooseFairUseTransition("throttle", 3, 1)).toEqual({
      action: "restrict",
      nextStage: "restrict",
    });
    expect(chooseFairUseTransition("restrict", 99, 1)).toEqual({
      action: "none",
      nextStage: "restrict",
    });
  });

  it("clamps a valid model result and fails closed toward no enforcement on malformed output", () => {
    const valid = parseFairUseClassifierResponse({
      response:
        '```json\n{"misuse_score": 9, "usage_type": "podcast", "confidence": -2, "evidence": [{"conversation_id":"1"}]}\n```',
    });
    expect(valid).toMatchObject({
      misuse_score: 1,
      usage_type: "podcast",
      confidence: 0,
      evidence: [{ conversation_id: "1" }],
      prompt_version: "v2",
    });
    expect(
      parseFairUseClassifierResponse({ response: "not-json" }),
    ).toMatchObject({
      misuse_score: 0,
      usage_type: "none",
      evidence: [],
    });
  });

  it("keeps the classifier conservative and metadata-only", () => {
    const input = fairUseClassifierInput([
      {
        conversation_id: "conversation-1",
        title: "Daily standup",
        overview: "Team planning",
        category: "work",
        duration_minutes: 35,
        source: "omi",
        created_at: "2026-08-28T00:00:00.000Z",
      },
    ]);
    expect(input.messages[0].content).toContain(
      "False positives restrict real users",
    );
    expect(input.messages[1].content).toContain("Daily standup");
    expect(input.messages[1].content).not.toContain("transcript_segments");
  });
});
