import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { expect, it } from "vitest";
import { pythonWorkerInvocation } from "../scripts/python-worker.mjs";
import { runReleaseProcess } from "../scripts/release-wrangler.mjs";

// Real incident: PR #14 Fork Checks 34331397206 failed after an unread-body 401
// because Wrangler's separate ProxyWorker dropped the next request and exited.
// This runtime contract complements the unchanged full Python product suite.
// Preparation 34347494195 then exposed the release projection's absolute paths;
// the same real runtime behavior must hold for both producer representations.
it.each(["relative", "absolute"])("serves frozen multiworker HTTP, D1, SSE and WebSocket with %s module paths", (paths) => {
  const root = resolve(import.meta.dirname, "..");
  const directory = mkdtempSync(resolve(tmpdir(), "cf-direct-runtime-"));
  const worker = `export default { async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/unauthorized') return new Response(null,{status:401});
    if (path === '/invalid') return new Response(null,{status:404});
    if (path === '/socket') {
      const [client, server] = Object.values(new WebSocketPair());
      server.accept(); server.addEventListener('message',event=>server.send(event.data));
      return new Response(null,{status:101,webSocket:client});
    }
    if (path === '/events') return new Response('data: first\\n\\ndata: done\\n\\n',{headers:{'content-type':'text/event-stream'}});
    await env.DB.prepare('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY)').run();
    if (request.method === 'POST') await env.DB.prepare('INSERT INTO evidence VALUES (?)').bind(await request.text()).run();
    const rows = await env.DB.prepare('SELECT id FROM evidence ORDER BY id').all();
    return Response.json(rows.results,{headers:[['set-cookie','first=one'],['set-cookie','second=two']]});
  }};`;
  try {
    for (const [name, contents] of [
      [
        "edge",
        "export default {fetch(request,env){return env.API.fetch(request)}}",
      ],
      ["api", worker],
    ]) {
      const bundle = resolve(directory, name);
      mkdirSync(resolve(bundle, "modules"), { recursive: true });
      writeFileSync(resolve(bundle, "modules/index.js"), contents);
      writeFileSync(
        resolve(bundle, "modules/README.md"),
        "Wrangler build metadata"
      );
      writeFileSync(
        resolve(bundle, "wrangler.json"),
        JSON.stringify({
          name,
          main:
            paths === "absolute"
              ? resolve(bundle, "modules/index.js")
              : "modules/index.js",
          base_dir:
            paths === "absolute" ? resolve(bundle, "modules") : "modules",
          no_bundle: true,
          find_additional_modules: true,
          compatibility_date: "2026-08-27",
          ...(name === "edge"
            ? { services: [{ binding: "API", service: "api" }] }
            : {
                d1_databases: [
                  { binding: "DB", database_name: "test", database_id: "test" },
                ],
              }),
        })
      );
    }
    mkdirSync(resolve(directory, "web/modules"), { recursive: true });
    mkdirSync(resolve(directory, "web/assets"));
    writeFileSync(
      resolve(directory, "web/modules/index.js"),
      "export default {fetch(request,env){return env.EDGE.fetch(request)}}"
    );
    writeFileSync(
      resolve(directory, "web/assets/marker.txt"),
      "frozen public asset"
    );
    writeFileSync(
      resolve(directory, "web/wrangler.json"),
      JSON.stringify({
        name: "web",
        main:
          paths === "absolute"
            ? resolve(directory, "web/modules/index.js")
            : "modules/index.js",
        base_dir:
          paths === "absolute"
            ? resolve(directory, "web/modules")
            : "modules",
        no_bundle: true,
        find_additional_modules: true,
        compatibility_date: "2026-08-27",
        assets: { directory: "assets", binding: "ASSETS" },
        services: [{ binding: "EDGE", service: "edge" }],
      })
    );
    const source = `import assert from 'node:assert/strict';
      import {readFileSync,writeFileSync} from 'node:fs'; import {once} from 'node:events'; import {WebSocket} from 'ws';
      import {startFrozenRuntime,frozenRuntimeWorker} from './contracts/local-runtime.mjs';
      const directory=${JSON.stringify(directory)};
      const configs=['edge','api'].map(role=>directory+'/'+role+'/wrangler.json');
      const options={configs,port:0,state:directory+'/state',web:{config:directory+'/web/wrangler.json',port:0}};
      let runtime=await startFrozenRuntime(options);
      try {
        let origin=await runtime.ready;
        for(let i=0;i<24;i++) for(const [path,status] of [['/unauthorized',401],['/invalid',404]]) {
          const response=await fetch(new URL(path,origin),{method:'POST',body:'synthetic-body'});
          assert.equal(response.status,status);await response.arrayBuffer();
        }
        const stored=await fetch(origin,{method:'POST',body:'retained'});
        assert.deepEqual(await stored.json(),[{id:'retained'}]);
        assert.deepEqual(stored.headers.getSetCookie(),['first=one','second=two']);
        const events=await fetch(new URL('/events',origin));
        assert.equal(events.headers.get('content-type'),'text/event-stream');
        assert.equal(await events.text(),'data: first\\n\\ndata: done\\n\\n');
        const url=new URL('/socket',origin);url.protocol='ws:';
        const socket=new WebSocket(url);await once(socket,'open');
        const message=once(socket,'message');socket.send('same socket');
        assert.equal(String((await message)[0]),'same socket');
        const closed=once(socket,'close');socket.close();await closed;
        await runtime.dispose();runtime=await startFrozenRuntime(options);
        origin=await runtime.ready;
        assert.deepEqual(await (await fetch(origin)).json(),[{id:'retained'}]);
        const web=await runtime.unsafeGetDirectURL('web');
        assert.equal(await (await fetch(new URL('/marker.txt',web))).text(),'frozen public asset');
        assert.deepEqual(await (await fetch(web)).json(),[{id:'retained'}]);
        writeFileSync(directory+'/edge/modules/unknown.extension','not a declared module');
        assert.throws(()=>frozenRuntimeWorker(configs[0]),/no declared runtime type/);
        const invalid=JSON.parse(readFileSync(configs[0],'utf8'));
        invalid.base_dir=directory+'/api/modules';
        writeFileSync(configs[0],JSON.stringify(invalid));
        assert.throws(()=>frozenRuntimeWorker(configs[0]),/requires a frozen upload configuration/);
        console.log('RESULT direct runtime passed');
      } finally { await runtime.dispose(); }`;
    const invocation = pythonWorkerInvocation("api-core", ["dev"], {
      root,
      env: { ...process.env, CLOUDFLARE_PYODIDE_CACHE_DIR: directory },
    });
    const result = runReleaseProcess(
      process.execPath,
      ["--input-type=module", "-e", source],
      {
        cwd: root,
        env: invocation.env,
        encoding: "utf8",
        timeout: 25000,
      }
    );
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain("RESULT direct runtime passed");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}, 30000);
