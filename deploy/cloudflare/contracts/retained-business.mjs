// LIFECYCLE: permanent
// Seed through retained code, then exercise the same identity and rows after
// replacing the private Worker graph. No serving authority is mutated.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { hostedOrigins } from './hosted-product.mjs';

export async function beginRetainedBusiness(context, transport) {
  const origins = hostedOrigins(context.candidate);
  async function request(service, path, { method = 'GET', body, token, status = 200 } = {}) {
    const response = await transport.fetch(origins[service] + path, {
      method, headers: { Origin: origins.web, 'X-App-Platform': 'web',
        ...(body === undefined ? {} : {'Content-Type':'application/json'}),
        ...(token ? {Authorization: 'Bearer ' + token} : {}) },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(60000),
    });
    assert.equal(response.status, status, 'retained business HTTP contract');
    return response;
  }
  const signed = await request('auth', '/api/auth/sign-up/email', { method:'POST', body: {
    name:'Retained release fixture', email:`retained-${randomUUID()}@example.invalid`, password:randomUUID()+randomUUID(),
  }});
  const session = signed.headers.get('set-auth-token');
  const identity = (await signed.json()).user?.id;
  assert.ok(session && identity, 'retained identity/session');
  const jwt = (await (await request('auth','/api/auth/token',{token:session})).json()).token;
  assert.ok(jwt, 'retained JWT');
  const memory = await (await request('api','/v3/memories',{method:'POST',token:jwt,body:{content:'Retained synthetic memory',category:'manual'}})).json();
  const task = await (await request('api','/v1/action-items',{method:'POST',token:jwt,body:{description:'Retained synthetic task'}})).json();
  assert.ok(memory.id && task.id, 'retained rows have stable identities');
  return async () => {
    const token = (await (await request('auth','/api/auth/token',{token:session})).json()).token;
    assert.ok(token, 'new Auth must accept the retained session');
    const memories = await (await request('api','/v3/memories',{token})).json();
    assert.ok(memories.some(row => row.id === memory.id && row.content === memory.content), 'retained memory survived the code update');
    const tasks = await (await request('api','/v1/action-items',{token})).json();
    assert.ok(tasks.action_items.some(row => row.id === task.id && row.description === task.description), 'retained task survived the code update');
    await request('api',`/v3/memories/${encodeURIComponent(memory.id)}`,{method:'PATCH',token,body:{value:'Updated retained memory'}});
    await request('api',`/v1/action-items/${encodeURIComponent(task.id)}/completed?completed=true`,{method:'PATCH',token});
    const changed = await (await request('api','/v3/memories',{token})).json();
    assert.ok(changed.some(row => row.id === memory.id && row.content === 'Updated retained memory'));
    const completed = await (await request('api','/v1/action-items?completed=true',{token})).json();
    assert.ok(completed.action_items.some(row => row.id === task.id && row.completed === true));
    await request('api','/v1/users/delete-account',{method:'DELETE',token,body:{}});
    return ['cloud.upgrade.retained-session-memory-and-task'];
  };
}
