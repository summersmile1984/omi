export type FairUseStage = "none" | "warning" | "throttle" | "restrict";
export type FairUseTrigger = "daily" | "3day" | "weekly";
export type FairUseAction = FairUseStage;
export type FairUseUsageType =
  | "none"
  | "audiobook"
  | "podcast"
  | "prerecorded"
  | "tv_movie"
  | "commercial"
  | "unknown"
  | "free_exhausted";

export type FairUseCaps = {
  daily: number;
  threeDay: number;
  weekly: number;
};

export type FairUseUsageWindow = {
  daily: number;
  threeDay: number;
  weekly: number;
};

export type FairUseClassifierResult = {
  misuse_score: number;
  usage_type: FairUseUsageType;
  confidence: number;
  evidence: Array<Record<string, unknown>>;
  reasoning?: string;
  model: string;
  prompt_version: "v2";
};

export type FairUseConversationSummary = {
  conversation_id: string;
  title: string;
  overview: string;
  category: string;
  duration_minutes: number;
  source: string;
  created_at: string;
};

export const DEFAULT_FAIR_USE_CAPS: FairUseCaps = {
  daily: 7_200_000,
  threeDay: 28_800_000,
  weekly: 36_000_000,
};
export const UNLIMITED_FAIR_USE_CAPS: FairUseCaps = {
  daily: 14_400_000,
  threeDay: 57_600_000,
  weekly: 72_000_000,
};
export const BASIC_MONTHLY_TRANSCRIPTION_SECONDS = 72_000;
export const FAIR_USE_CLASSIFIER_THRESHOLD = 0.7;
export const FAIR_USE_CLASSIFIER_MODEL = "@cf/meta/llama-3.2-3b-instruct";

const UNLIMITED_PLANS = new Set([
  "unlimited",
  "unlimited_v2",
  "operator",
  "architect",
]);
const USAGE_TYPES = new Set<FairUseUsageType>([
  "none",
  "audiobook",
  "podcast",
  "prerecorded",
  "tv_movie",
  "commercial",
  "unknown",
  "free_exhausted",
]);

const SYSTEM_PROMPT = `You are a fair-use cost-protection analyst for Omi, a personal AI wearable device.

This classifier runs only after the user exceeded a speech-hour soft cap. Decide whether the high usage is legitimate personal use or abusive bulk transcription.

Be extremely conservative. False positives restrict real users. A single suspicious conversation is not enough; require a clear pattern across many sessions. High-volume personal conversations, work meetings, live lectures, phone or video calls, conferences, workshops, interviews, therapy, and other live human interaction are legitimate regardless of volume.

Only score 0.7 or higher when BOTH high volume and a strong repeated wrong-purpose pattern are present: audiobook chapters, podcast feeds, TV or movie transcription, uniform pre-recorded content farms, or a commercial transcription service. Occasional non-personal use, mixed use, or uncertain evidence must score below 0.7.

Return only strict JSON with this shape:
{"misuse_score":0.0,"usage_type":"none|audiobook|podcast|prerecorded|tv_movie|commercial|unknown","confidence":0.0,"evidence":[{"conversation_id":"","title":"","reason":""}],"reasoning":"brief explanation"}

Scoring: 0.0-0.3 legitimate; 0.4-0.6 mixed or uncertain; 0.6-0.7 borderline; 0.7-0.85 strong bulk misuse; 0.85-1.0 unambiguous bulk abuse.`;

function numberBetween(
  value: unknown,
  minimum: number,
  maximum: number,
): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed)
    ? Math.max(minimum, Math.min(maximum, parsed))
    : minimum;
}

export function capsForPlan(plan: string | null | undefined): FairUseCaps {
  return UNLIMITED_PLANS.has(plan || "")
    ? UNLIMITED_FAIR_USE_CAPS
    : DEFAULT_FAIR_USE_CAPS;
}

export function triggeredFairUseCaps(
  usage: FairUseUsageWindow,
  caps: FairUseCaps,
): FairUseTrigger[] {
  const result: FairUseTrigger[] = [];
  if (usage.daily > caps.daily) result.push("daily");
  if (usage.threeDay > caps.threeDay) result.push("3day");
  if (usage.weekly > caps.weekly) result.push("weekly");
  return result;
}

export function chooseFairUseTransition(
  currentStage: FairUseStage,
  priorViolationCount7d: number,
  misuseScore: number,
): { action: FairUseAction; nextStage: FairUseStage } {
  if (misuseScore < FAIR_USE_CLASSIFIER_THRESHOLD) {
    return { action: "none", nextStage: currentStage };
  }
  if (currentStage === "none") {
    return { action: "warning", nextStage: "warning" };
  }
  if (currentStage === "warning" && priorViolationCount7d >= 2) {
    return { action: "throttle", nextStage: "throttle" };
  }
  if (currentStage === "throttle" && priorViolationCount7d >= 3) {
    return { action: "restrict", nextStage: "restrict" };
  }
  return { action: "none", nextStage: currentStage };
}

export function defaultClassifierResult(
  model = FAIR_USE_CLASSIFIER_MODEL,
): FairUseClassifierResult {
  return {
    misuse_score: 0,
    usage_type: "none",
    confidence: 0,
    evidence: [],
    model,
    prompt_version: "v2",
  };
}

export function parseFairUseClassifierResponse(
  value: unknown,
  model = FAIR_USE_CLASSIFIER_MODEL,
): FairUseClassifierResult {
  const fallback = defaultClassifierResult(model);
  if (!value || typeof value !== "object" || Array.isArray(value))
    return fallback;
  const response = (value as Record<string, unknown>).response;
  if (typeof response !== "string") return fallback;
  const fenced = response.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  let parsed: unknown;
  try {
    parsed = JSON.parse((fenced || response).trim());
  } catch {
    return fallback;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    return fallback;
  const object = parsed as Record<string, unknown>;
  const rawUsageType =
    typeof object.usage_type === "string" ? object.usage_type : "none";
  const usageType = USAGE_TYPES.has(rawUsageType as FairUseUsageType)
    ? (rawUsageType as FairUseUsageType)
    : "unknown";
  const evidence = Array.isArray(object.evidence)
    ? object.evidence
        .filter(
          (item): item is Record<string, unknown> =>
            !!item && typeof item === "object" && !Array.isArray(item),
        )
        .slice(0, 10)
    : [];
  return {
    misuse_score: numberBetween(object.misuse_score, 0, 1),
    usage_type: usageType,
    confidence: numberBetween(object.confidence, 0, 1),
    evidence,
    ...(typeof object.reasoning === "string"
      ? { reasoning: object.reasoning.slice(0, 2_000) }
      : {}),
    model,
    prompt_version: "v2",
  };
}

function additionalRecipes(summaries: FairUseConversationSummary[]): string {
  if (!summaries.length) return "";
  const durations = summaries.map((item) => item.duration_minutes);
  const recipes: string[] = [];
  if (durations.filter((duration) => duration > 60).length >= 3) {
    recipes.push(
      "Check for sequential audiobook chapters or long single-speaker literary sessions.",
    );
  }
  if (durations.length >= 5) {
    const average =
      durations.reduce((sum, value) => sum + value, 0) / durations.length;
    const variance =
      durations.reduce((sum, value) => sum + (value - average) ** 2, 0) /
      durations.length;
    if (average > 0 && Math.sqrt(variance) / average < 0.3) {
      recipes.push(
        "Check whether unusually uniform durations indicate pre-recorded content.",
      );
    }
  }
  if (
    summaries.length >= 20 &&
    new Set(summaries.map((item) => item.category)).size <= 3
  ) {
    recipes.push(
      "Check for a repeated commercial transcription-service pattern.",
    );
  }
  if (
    durations.filter((duration) => duration >= 25 && duration <= 90).length >= 5
  ) {
    recipes.push(
      "Check for repeated podcast episode formats, without flagging isolated episodes.",
    );
  }
  return recipes.join("\n");
}

export function fairUseClassifierInput(
  summaries: FairUseConversationSummary[],
): { messages: Array<{ role: "system" | "user"; content: string }> } {
  const recipes = additionalRecipes(summaries);
  return {
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      {
        role: "user",
        content:
          `Analyze these ${summaries.length} recent conversations.\n` +
          `${recipes ? `${recipes}\n` : ""}` +
          `CONVERSATIONS:\n${JSON.stringify(summaries)}\nReturn only JSON.`,
      },
    ],
  };
}

export function fairUseNotification(
  action: Exclude<FairUseAction, "none">,
  caseRef: string,
): { title: string; body: string; data: Record<string, string> } {
  const titles = {
    warning: "Fair Use Notice",
    throttle: "Transcription Quality Reduced",
    restrict: "Transcription Limit Reached",
  };
  const suffix = caseRef ? ` Reference: ${caseRef}` : "";
  const bodies = {
    warning:
      "Your speech usage is unusually high. Omi is designed for personal conversations. If this continues, transcription quality may be reduced. Check Settings > Plan & Usage for details." +
      suffix,
    throttle:
      "Due to high non-conversational usage, your transcription quality has been temporarily reduced. This will reset automatically. Contact team@basedhardware.com if you believe this is an error. Quote your case reference when contacting support." +
      suffix,
    restrict:
      "Your cloud transcription has been temporarily limited due to repeated fair-use violations. On-device transcription continues normally. Contact team@basedhardware.com to resolve. Quote your case reference when contacting support." +
      suffix,
  };
  return {
    title: titles[action],
    body: bodies[action],
    data: {
      type: "fair_use",
      action,
      ...(caseRef ? { case_ref: caseRef } : {}),
    },
  };
}
