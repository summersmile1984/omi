export type ReadinessService = {
  fetch(request: Request): Promise<Response>;
};

export async function workerReadiness(
  edge: ReadinessService | undefined,
): Promise<Response> {
  if (!edge) {
    return Response.json(
      { status: 'degraded', service: 'web', error: 'EDGE binding unavailable' },
      { status: 503 },
    );
  }
  try {
    const response = await edge.fetch(new Request('https://edge.internal/ready'));
    return new Response(response.body, {
      status: response.status,
      headers: { 'content-type': 'application/json' },
    });
  } catch {
    return Response.json(
      { status: 'degraded', service: 'web', error: 'EDGE binding unavailable' },
      { status: 503 },
    );
  }
}
