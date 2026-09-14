// Required cases belong to the reviewed business contract, not a runner report.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const bytes = readFileSync(new URL('./product-cases.json', import.meta.url));
const contract = JSON.parse(bytes);
export const PRODUCT_CONTRACT = createHash('sha256').update(bytes).digest('hex');

export function requiredProductCases(suite, { surface, remote = false }) {
  if (!['source', 'frozen'].includes(surface) || !Object.hasOwn(contract.suites, suite))
    throw new Error('product qualification requires an explicit suite and surface');
  return [...contract.suites[suite],
    ...(surface === 'frozen' && suite === 'share' ? contract.frozen_web : []),
    ...(remote ? (contract.remote_cleanup[suite] ?? []) : [])];
}

export function assertProductCases(ids, suite, options) {
  const required = requiredProductCases(suite, options);
  if (!Array.isArray(ids) || new Set(ids).size !== ids.length ||
      JSON.stringify([...ids].sort()) !== JSON.stringify([...required].sort()))
    throw new Error(`product regression returned incomplete evidence: ${suite}`);
}

export function completeProductReport(report, suite, options) {
  if (report.passed) assertProductCases(report.cases.map(row => row.id), suite, options);
  return { ...report, contract_sha256: PRODUCT_CONTRACT };
}

export function requiredCloudflareCases() {
  return ['core', 'recording', 'chat', 'share'].flatMap(suite =>
    requiredProductCases(suite, { surface: 'frozen' }).map(id => `${suite}:${id}`));
}

export function assertCloudflareCases(ids) {
  const expected = requiredCloudflareCases();
  if (new Set(ids).size !== ids.length || JSON.stringify([...ids].sort()) !== JSON.stringify(expected.sort()))
    throw new Error('Cloudflare product returned incomplete evidence');
}

export function requiredHostedCloudflareCases() { return [...contract.hosted_cloudflare]; }

export function requiredQualificationCases(id, observations) {
  const deployed = observations.release_phase === 'deployed';
  if (!['candidate', 'deployed'].includes(observations.release_phase))
    throw new Error('qualification has no admitted execution phase');
  if (id === 'CF-4') return deployed ? requiredHostedCloudflareCases() : requiredCloudflareCases();
  const core = requiredProductCases('core', { surface: 'source' });
  if (id === 'CI-1') return ['ci.exact-source-platform-brand-contracts',
    ...core.map(id => `server:${id}`),
    ...(deployed ? core : requiredCloudflareCases()).map(id => `cloudflare:${id}`)];
  if (id === 'prior-schema') return [observations.continuation
    ? 'first-release.observed-owned-continuation' : 'first-release.observed-prior-worker-absence',
    ...['app', 'auth'].flatMap(authority => [
      `first-release.${deployed ? 'deployed-frozen-schema' : observations.continuation ? 'retained-frozen-schema' : 'empty-authority'}.${authority}`,
      `first-release.frozen-sql-and-legacy-fixture.${authority}`])];
  throw new Error('unknown qualification owner');
}

export function assertQualificationCases(ids, id, observations) {
  const expected = requiredQualificationCases(id, observations);
  if (!Array.isArray(ids) || new Set(ids).size !== ids.length ||
      JSON.stringify([...ids].sort()) !== JSON.stringify(expected.sort()))
    throw new Error(`qualification returned incomplete evidence: ${id}`);
}
