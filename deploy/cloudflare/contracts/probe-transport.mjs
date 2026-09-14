// LIFECYCLE: permanent
// Keep frozen public authorities, auth and business requests unchanged while
// selecting the release-owned private Worker graph for remote qualification.
export function probeTransport(origins, gateway, token, fetchImpl = fetch) {
  const url = new URL(gateway);
  if (url.protocol !== 'https:' || !/^eddy-ci-[bp]-[a-f0-9]{8}-probe\.[a-z0-9-]+\.workers\.dev$/.test(url.hostname) ||
      url.origin !== gateway || !/^[a-f0-9]{64}$/.test(token))
    throw new Error('invalid private release transport');
  function request(input, init = {}) {
    const publicUrl = new URL(input);
    const entry = Object.entries(origins).find(([, origin]) => origin === publicUrl.origin);
    if (!entry) throw new Error('private release request left the frozen public origins');
    const [service] = entry;
    const headers = new Headers(init.headers);
    headers.set('x-release-probe', token);
    return { url: gateway + '/__service/' + service + publicUrl.pathname + publicUrl.search,
      init: { ...init, headers, redirect: 'manual' } };
  }
  return {
    fetch: (input, init) => { const value = request(input, init); return fetchImpl(value.url, value.init); },
    socket: (input, options = {}) => {
      const value = request(input.replace(/^wss:/, 'https:'), options);
      return [value.url.replace(/^https:/, 'wss:'), { ...options, headers: Object.fromEntries(value.init.headers) }];
    },
    metadata: { probe_origin: gateway, probe_token: token },
  };
}
