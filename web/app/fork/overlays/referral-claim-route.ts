import { webProfile } from '@/lib/fork/web-profile';

export async function POST(request: Request) {
  const authorization = request.headers.get('authorization');
  if (!authorization)
    return Response.json({ error: 'Authorization header required' }, { status: 401 });
  let body;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: 'Invalid request body' }, { status: 400 });
  }
  if (typeof body?.code !== 'string' || !body.code || body.code.length > 512)
    return Response.json({ error: 'Invalid referral claim' }, { status: 400 });
  try {
    const response = await fetch(
      `${webProfile().api_base_url.replace(/\/$/, '')}/v1/users/me/referral/claim`,
      {
        method: 'POST',
        credentials: 'omit',
        redirect: 'error',
        headers: { Authorization: authorization, 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: body.code }),
        signal: AbortSignal.timeout(20000),
      },
    );
    return new Response(response.body, {
      status: response.status,
      headers: {
        'content-type': 'application/json',
        'cache-control': 'no-store',
      },
    });
  } catch {
    return Response.json({ error: 'Referral service unavailable' }, { status: 502 });
  }
}
