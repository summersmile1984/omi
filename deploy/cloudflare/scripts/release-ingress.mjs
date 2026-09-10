// The account Trace API evaluates ingress before a Worker/domain is published.
// skip_response never calls the origin; this is not application health evidence.
// Contract: https://developers.cloudflare.com/rules/trace-request/
export function publicIngressRequests(candidate, readiness) {
  const requests = [];
  for (const target of ["cloudflare", "self_hosted"]) {
    const profile = candidate.profiles?.[target]?.profile;
    if (profile?.target !== target || profile.stage !== candidate.stage)
      throw new Error("ingress qualification requires both frozen target profiles");
    const add = (id, base, path, userAgent = "node") => {
      const origin = new URL(base);
      if (origin.protocol !== "https:" || origin.username || origin.password || origin.search || origin.hash)
        throw new Error("ingress qualification requires public HTTPS profiles");
      requests.push({ id: `${target}.${id}`, url: `${base.replace(/\/+$/, "")}${path}`, userAgent });
    };
    add("api", profile.api_base_url, "/v2/messages?limit=1");
    add("auth", profile.auth_base_url, "/api/auth/get-session");
    add("web-api", profile.web_base_url, "/api/proxy/v2/messages?limit=1");
    const origins = { api: profile.api_base_url, auth: profile.auth_base_url, web: profile.web_base_url };
    for (const [role, { origin, path }] of Object.entries(readiness[target]))
      // Match each CD client's actual UA; never disguise probes as a browser.
      add(`ready-${role}`, origins[origin], path, target === "cloudflare" ? "node" : "Python-urllib/3.12");
  }
  return requests;
}

export function assertIngressTrace(result, browserCheck) {
  if (!["on", "off"].includes(browserCheck) || result?.status_code !== 200 ||
      !Array.isArray(result.trace) || !result.trace.length)
    throw new Error("ingress trace is incomplete or blocked");
  let bic = browserCheck === "on", count = 0;
  const visit = (rows, depth) => {
    if (!Array.isArray(rows) || depth > 12) throw new Error("invalid ingress trace");
    for (const row of rows) {
      if (++count > 10000 || typeof row?.matched !== "boolean") throw new Error("invalid ingress trace");
      if (!row.matched) continue;
      if (["block", "challenge", "js_challenge", "managed_challenge", "redirect"].includes(row.action))
        throw new Error("ingress policy blocks or redirects the deployment client");
      if (row.action === "set_config" && Object.hasOwn(row.action_parameter ?? {}, "bic")) {
        if (typeof row.action_parameter.bic !== "boolean") throw new Error("invalid browser integrity policy");
        bic = row.action_parameter.bic;
      }
      if (row.trace !== undefined) visit(row.trace, depth + 1);
    }
  };
  visit(result.trace, 0);
  if (bic) throw new Error("browser integrity policy does not admit the API/deploy client");
}

export async function qualifyPublicIngress(candidate, zones, readiness, api) {
  const settings = new Map(), cases = [];
  for (const request of publicIngressRequests(candidate, readiness)) {
    const host = new URL(request.url).hostname;
    const zone = zones.filter(({ name }) => host === name || host.endsWith(`.${name}`))
      .sort((a, b) => b.name.length - a.name.length)[0];
    if (!zone || !/^[0-9a-f]{32}$/.test(zone.id)) throw new Error(`ingress zone is not owned: ${request.id}`);
    try {
      if (!settings.has(zone.id)) settings.set(zone.id,
        (await api(`/zones/${zone.id}/settings/browser_check`)).result?.value);
      const response = await api("request-tracer/trace", { method: "POST", body: {
        url: request.url, method: "GET", headers: { "User-Agent": request.userAgent }, skip_response: true,
      } });
      assertIngressTrace(response.result, settings.get(zone.id));
    } catch (error) {
      // The API adapter sanitizes transport errors; raw trace/policy payloads
      // never enter the public journal. Name the failed profile boundary only.
      throw new Error(`public ingress did not qualify: ${request.id}`, { cause: error });
    }
    cases.push({ id: `cloud.ingress.${request.id}`, result: "pass" });
  }
  return cases;
}
