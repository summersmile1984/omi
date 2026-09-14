// LIFECYCLE: permanent
// The same account-policy owner qualifies only this delivery's Server origins.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { cloudflareAccountRequest, DEPLOYMENT_READINESS } from '../../deploy/cloudflare/scripts/release-wrangler.mjs';
import { qualifyPublicIngress } from '../../deploy/cloudflare/scripts/release-ingress.mjs';

const { values } = parseArgs({ options: { delivery: { type: 'string' } } });
if (!values.delivery) throw new Error('--delivery is required');
const receipt = JSON.parse(readFileSync(resolve(values.delivery, 'delivery.json'), 'utf8'));
const account = receipt.cloudflare_account_id;
const api = (path, options) => cloudflareAccountRequest({ account, token: process.env.CLOUDFLARE_API_TOKEN }, path, options);
const zones = [];
for (let page = 1; ; page++) {
  const response = await api(`/zones?account.id=${account}&status=active&per_page=50&page=${page}`);
  if (!Array.isArray(response.result) || !Number.isInteger(response.result_info?.total_pages) || page > 1000)
    throw new Error('Server ingress requires complete zone ownership observations');
  zones.push(...response.result);
  if (page >= response.result_info.total_pages) break;
}
const cases = await qualifyPublicIngress(receipt, zones, DEPLOYMENT_READINESS, api, 'self_hosted');
console.log(JSON.stringify({ target: 'self_hosted', ingress_cases: cases.length, passed: true }));
