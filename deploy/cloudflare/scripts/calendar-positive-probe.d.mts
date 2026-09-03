export function resolveCalendarProbeConfig(env?: Record<string, string | undefined>): {
  edgeUrl: string;
  bearer: string;
  accessToken: string;
};
export function runCalendarPositiveProbe(options: {
  config: { edgeUrl: string; bearer: string; accessToken: string };
  fetchImpl?: typeof fetch;
}): Promise<{ status: "passed"; integration_save: 200; events: 200; cleanup: 204 }>;
