import { env } from 'cloudflare:workers';

import { workerReadiness, type ReadinessService } from '@/lib/worker-readiness';

export async function GET() {
  return workerReadiness((env as unknown as { EDGE?: ReadinessService }).EDGE);
}
