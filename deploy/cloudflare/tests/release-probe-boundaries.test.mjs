import { describe, expect, it, vi } from 'vitest';
import { privateResourceBindings, cleanupProbeResource } from '../scripts/release-probe-resources.mjs';
import { gatewaySource } from '../scripts/release-cloud-probe.mjs';
import { probeTransport } from '../contracts/probe-transport.mjs';
import { releaseFailure } from '../scripts/release-transaction.mjs';
import { PRODUCT_CONTRACT, requiredQualificationCases } from '../../../contracts/deployment/product-cases.mjs';
import { runReleaseQualifiers, RELEASE_QUALIFIERS } from '../scripts/release-qualification.mjs';
import { digest } from '../scripts/resource-input.mjs';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';

const origins = {api:'https://api.example.com',auth:'https://auth.example.com',web:'https://web.example.com'};
const gateway = 'https://eddy-ci-b-12345678-probe.fixture.workers.dev';
const token = 'a'.repeat(64);

describe('release execution boundaries', () => {
  it('preserves method, query, body and user auth through the actual private gateway handler', async () => {
    const code = gatewaySource(origins, ['BUCKET_0']);
    const {default: handler} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
    const received = [];
    const binding = {fetch: async request => {
      received.push({url:request.url,method:request.method,auth:request.headers.get('authorization'),probe:request.headers.get('x-release-probe'),body:await request.text()});
      return Response.json({forwarded:true});
    }};
    const transport = probeTransport(origins,gateway,token,(url,init) => handler.fetch(new Request(url,init),{PROBE_TOKEN:token,edge:binding}));
    await transport.fetch(origins.api+'/v2/messages?stream=true',{method:'POST',headers:{Authorization:'Bearer synthetic-user'},body:'synthetic-message'});
    expect(received).toEqual([{url:origins.api+'/v2/messages?stream=true',method:'POST',auth:'Bearer synthetic-user',probe:null,body:'synthetic-message'}]);
    expect((await handler.fetch(new Request(gateway+'/__service/api/v2/messages'),{PROBE_TOKEN:token})).status).toBe(404);
    expect(() => transport.fetch('https://unowned.example.com')).toThrow('left the frozen');
    const [socketUrl, options] = transport.socket('wss://api.example.com/v4/listen',{headers:{Authorization:'Bearer synthetic-user'}});
    expect(socketUrl).toBe(gateway.replace('https:','wss:')+'/__service/api/v4/listen');
    expect(options.headers.authorization).toBe('Bearer synthetic-user');
    expect(options.headers['x-release-probe']).toBe(token);
    const bucket = {list:vi.fn(async () => ({objects:[{key:'owned'}],truncated:false})),delete:vi.fn(async () => {})};
    const clean = await handler.fetch(new Request(gateway+'/__cleanup/0',{method:'POST',headers:{'x-release-probe':token}}),{PROBE_TOKEN:token,BUCKET_0:bucket});
    expect(await clean.json()).toEqual({done:true});
    expect(bucket.delete).toHaveBeenCalledWith(['owned']);
  });
  it('refuses shared storage and external consumers before deleting any resource', async () => {
    expect(() => privateResourceBindings({queues:{producers:[{binding:'JOBS',queue:'serving'}]}},[])).toThrow('serving queue');
    expect(() => privateResourceBindings({kv_namespaces:[{id:'serving'}]},[])).toThrow('no isolated');
    const event = {resource:{kind:'queue',name:'owned-queue'},created_id:'owned-id'};
    const adapter = {observeResource:vi.fn(async () => ({status:'present',id:'owned-id'})),api:vi.fn(async () => ({result:[{script_name:'external',consumer_id:'one'}]}))};
    await expect(cleanupProbeResource(adapter,event,{workerNames:['owned-worker']})).rejects.toThrow('external consumer');
    expect(adapter.api).toHaveBeenCalledTimes(1);
    adapter.observeResource.mockResolvedValue({status:'present',id:'different-owner'});
    await expect(cleanupProbeResource(adapter,event,{workerNames:[]})).rejects.toThrow('ownership');
    expect(adapter.api).toHaveBeenCalledTimes(1);
  });
  it('records only known readiness diagnostics and strips arbitrary exception content', () => {
    expect(releaseFailure({failure:{operation:'readiness',service:'web',path:'/api/worker-ready',status:404,reason:'http_status',body:'secret'}}))
      .toEqual({phase:'deployed',operation:'readiness',service:'web',path:'/api/worker-ready',status:404,reason:'http_status'});
    expect(JSON.stringify(releaseFailure({failure:{operation:'readiness',service:'secret',path:'/secret',status:'secret',reason:'secret'}}))).not.toContain('secret');
  });
  it.each(['candidate','deployed'])('requires every owner case in %s proof, not a passing nonempty report', phase => {
    const root = mkdtempSync(resolve(tmpdir(),'qualifier-proof-'));
    try {
      for (const row of RELEASE_QUALIFIERS) { const path = resolve(root,row.path); mkdirSync(dirname(path),{recursive:true});writeFileSync(path,'// synthetic owner'); }
      const candidate = {candidate_digest:'b'.repeat(64)}, observations = {release_phase:phase};
      let omit = false;
      const spawn = (_command,args) => {
        const entry = RELEASE_QUALIFIERS.find(row => args[0].endsWith(row.path));
        let cases = requiredQualificationCases(entry.id,observations);
        if (omit) cases = cases.slice(1);
        return {status:0,stdout:JSON.stringify({schema_version:1,contract_sha256:PRODUCT_CONTRACT,candidate_digest:candidate.candidate_digest,
          observation_digest:digest(observations),cases:cases.map(id => ({id,result:'pass'}))})};
      };
      expect(runReleaseQualifiers(root,candidate,observations,{directory:root,spawn})).toHaveLength(3);
      omit = true;
      expect(() => runReleaseQualifiers(root,candidate,observations,{directory:root,spawn})).toThrow('incomplete evidence');
    } finally {rmSync(root,{recursive:true,force:true});}
  });
});
