import { describe, expect, it } from "vitest";
import { assertIngressTrace, publicIngressRequests, qualifyPublicIngress } from "../scripts/release-ingress.mjs";
import { DEPLOYMENT_READINESS, WranglerReleaseAdapter } from "../scripts/release-wrangler.mjs";

const zone = { id: "a".repeat(32), name: "fixture.invalid", account: { id: "b".repeat(32) } };
const candidate = {
  stage: "beta",
  profiles: Object.fromEntries(["cloudflare", "self_hosted"].map((target) => [target, { profile: {
    target, stage: "beta", api_base_url: `https://${target}-api.fixture.invalid`,
    auth_base_url: `https://${target}-auth.fixture.invalid`, web_base_url: `https://${target}-web.fixture.invalid`,
  } }])),
};
const rule = (matched, bic) => ({ type: "rule", matched, action: "set_config", action_parameter: { bic } });
// Shape captured from the account Trace API on 2026-09-10. Trace requests skip
// the origin, so even a not-yet-published hostname returns evaluated rules.
const trace = (rules) => ({ status_code: 200, trace: [{
  type: "phase", step_name: "http_config_settings", matched: true,
  trace: [{ type: "ruleset", matched: true, trace: rules }],
}] });

describe("public ingress qualification before publication", () => {
  it("catches the Web API omission and evaluates matched configuration rules in order", () => {
    expect(() => assertIngressTrace(trace([rule(true, false)]), "on")).not.toThrow();
    expect(() => assertIngressTrace(trace([rule(false, false)]), "on")).toThrow("browser integrity");
    expect(() => assertIngressTrace(trace([rule(true, false), rule(true, true)]), "on")).toThrow("browser integrity");
    expect(() => assertIngressTrace(trace([rule(true, false), rule(false, true)]), "on")).not.toThrow();
    expect(() => assertIngressTrace(trace([]), "off")).not.toThrow();
    const inactive = trace([rule(true, false)]);
    inactive.trace[0].matched = false;
    expect(() => assertIngressTrace(inactive, "on")).toThrow("browser integrity");
  });
  it("rejects denial, redirects and incomplete evidence", () => {
    for (const action of ["block", "challenge", "js_challenge", "managed_challenge", "redirect"])
      expect(() => assertIngressTrace(trace([rule(true, false), { matched: true, action }]), "on")).toThrow("policy");
    for (const result of [{}, { status_code: 403, trace: [] }, { status_code: 200, trace: [] }, trace([rule(true, "false")])])
      expect(() => assertIngressTrace(result, "on")).toThrow();
    expect(() => assertIngressTrace(trace([]), undefined)).toThrow();
  });
  it("runs both frozen profiles without origin traffic, credentials or browser impersonation", async () => {
    const requests = [];
    const api = async (path, options) => {
      requests.push({ path, options });
      if (path.endsWith("/browser_check")) return { result: { value: "on" } };
      expect(path).toBe("request-tracer/trace");
      expect(options.body.skip_response).toBe(true);
      expect(options.body.context).toBeUndefined();
      expect(Object.keys(options.body.headers)).toEqual(["User-Agent"]);
      return { result: trace([rule(true, false)]) };
    };
    const result = await qualifyPublicIngress(candidate, [zone], DEPLOYMENT_READINESS, api);
    expect(result).toHaveLength(11);
    expect(requests.filter(({ path }) => path.endsWith("/browser_check"))).toHaveLength(1);
    const urls = requests.filter(({ options }) => options).map(({ options }) => options.body.url);
    expect(urls).toContain("https://cloudflare-web.fixture.invalid/api/worker-ready");
    expect(urls).toContain("https://self_hosted-web.fixture.invalid/api/proxy/v2/messages?limit=1");
    expect(requests.at(-1).options.body.headers["User-Agent"]).toBe("Python-urllib/3.12");
  });
  it("fails closed for existing profiles when their policy or required read permission is missing", async () => {
    const api = async (path) => path.endsWith("/browser_check")
      ? { result: { value: "on" } } : { result: trace([rule(false, false)]) };
    await expect(qualifyPublicIngress(candidate, [zone], DEPLOYMENT_READINESS, api)).rejects.toThrow("public ingress did not qualify");
    await expect(qualifyPublicIngress(candidate, [zone], DEPLOYMENT_READINESS, async () => { throw Error("permission denied"); })).rejects.toThrow("public ingress did not qualify");
    expect(() => publicIngressRequests({ stage: "beta" }, DEPLOYMENT_READINESS)).toThrow("both frozen");
  });
  it("executes ingress checks in the actual CD precondition owner even before domains exist", async () => {
    const paths = [];
    const adapter = Object.assign(Object.create(WranglerReleaseAdapter.prototype), {
      account: zone.account.id, env: {},
      candidate: { ...candidate, inventory: { routing: { mode: "custom_domains" } }, resource_plan: { secrets: {}, configs: {}, resources: [] } },
      policies: () => [],
      api: async (path) => {
        paths.push(path);
        if (path === "workers/domains") return { result: [] };
        if (path.startsWith("/zones?")) return { result: [zone], result_info: { total_pages: 1 } };
        if (path.endsWith("/browser_check")) return { result: { value: "on" } };
        return { result: trace([rule(true, false)]) };
      },
    });
    expect((await adapter.preconditions()).ingress).toHaveLength(11);
    expect(paths.filter((path) => path === "request-tracer/trace")).toHaveLength(11);
  });
});
