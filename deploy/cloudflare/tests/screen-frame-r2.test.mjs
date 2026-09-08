import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createHash, createHmac, randomUUID } from "node:crypto";
import { buildSync } from "esbuild";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { it, expect } from "vitest";
import { pythonWorkerInvocation } from "../scripts/python-worker.mjs";
import { runReleaseProcess } from "../scripts/release-wrangler.mjs";

// Execute via HTTP inside actual workerd: Miniflare's Node binding proxy is
// not the production execution boundary. No hosted service or network fixture.
it("R2 multipart abort prevents later completion and still requires deletion of completed objects", () => {
  const cache = mkdtempSync(resolve(tmpdir(), "screen-frame-r2-"));
  const root = resolve(import.meta.dirname, "..");
  const invocation = pythonWorkerInvocation("api-core", ["dev"], {
    root,
    env: { ...process.env, CLOUDFLARE_PYODIDE_CACHE_DIR: cache },
  });
  const worker = `export default {async fetch(request, env) {
    const result = [];
    for (const mode of ['active', 'completed', 'aborted']) {
      const key = 'screen-contract/' + mode;
      const upload = await env.SCREEN_FRAMES.createMultipartUpload(key);
      const part = await upload.uploadPart(1, new Uint8Array([255,216,1,255,217]));
      if (mode === 'completed') await upload.complete([part]);
      if (mode === 'aborted') await upload.abort();
      for (let attempt = 0; attempt < 2; attempt++) {
        try { await env.SCREEN_FRAMES.resumeMultipartUpload(key, upload.uploadId).abort(); }
        catch (error) { if (!/\\(10024\\)$/.test(error.message)) throw error; }
      }
      let lateCompletion = false;
      try { await upload.complete([part]); lateCompletion = true; } catch {}
      const beforeDelete = !!await env.SCREEN_FRAMES.head(key);
      await env.SCREEN_FRAMES.delete(key);
      result.push({mode, lateCompletion, beforeDelete, afterDelete: !!await env.SCREEN_FRAMES.head(key)});
    }
    return Response.json(result);
  }};`;
  const source = `import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
    const mf = new Miniflare(convertV4MiniflareOptions({modules:true,cf:false,script:${JSON.stringify(
      worker
    )},compatibilityDate:'2026-08-27',r2Buckets:['SCREEN_FRAMES']}));
    try {const response=await fetch(await mf.ready); console.log(JSON.stringify({status:response.status,body:await response.text()}));}
    finally {await mf.dispose();}`;
  try {
    const result = runReleaseProcess(
      process.execPath,
      ["--input-type=module", "-e", source],
      {
        cwd: root,
        env: invocation.env,
        encoding: "utf8",
        timeout: 10000,
      }
    );
    expect(result.status, result.stderr).toBe(0);
    const response = JSON.parse(result.stdout);
    expect(response.status).toBe(200);
    expect(JSON.parse(response.body)).toEqual([
      {
        mode: "active",
        lateCompletion: false,
        beforeDelete: false,
        afterDelete: false,
      },
      {
        mode: "completed",
        lateCompletion: false,
        beforeDelete: true,
        afterDelete: false,
      },
      {
        mode: "aborted",
        lateCompletion: false,
        beforeDelete: false,
        afterDelete: false,
      },
    ]);
  } finally {
    rmSync(cache, { recursive: true, force: true });
  }
}, 15000);

it("runs the actual writer through HTTP with workerd D1/R2, publishes approved bytes and revokes them on erasure", () => {
  const root = resolve(import.meta.dirname, "..");
  const directory = mkdtempSync(resolve(tmpdir(), "screen-writer-http-"));
  try {
    const sql = runReleaseProcess(
      resolve(root, "../../backend/.venv/bin/python"),
      [
        "-c",
        `
import json,sqlite3,sys
from pathlib import Path
statements=[]
for path in sorted(Path(sys.argv[1]).glob('*.sql')):
    pending=''
    for line in path.read_text().splitlines(keepends=True):
        pending+=line
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending=''
print(json.dumps(statements))
`,
        resolve(root, "migrations/app"),
      ],
      { encoding: "utf8", timeout: 5000 }
    );
    expect(sql.status, sql.stderr).toBe(0);
    const statements = JSON.parse(sql.stdout).map((statement) => [statement]);
    const now = Math.floor(Date.now() / 1000),
      uid = "native-screen-owner",
      jti = randomUUID(),
      attempt = randomUUID();
    const image = Buffer.from([255, 216, 1, 2, 255, 217]),
      key = "native-screen-fixture-secret-".repeat(2);
    const digest = createHash("sha256").update(image).digest("hex");
    const signed = (purpose, data) => {
      const body = `${purpose}.${Buffer.from(JSON.stringify(data)).toString(
        "base64url"
      )}`;
      return `${body}.${createHmac("sha256", key)
        .update(body)
        .digest("base64url")}`;
    };
    const approval = signed("screen-frame-approval-v1", {
      version: 1,
      iss: "omi-screen-frame-adjudicator",
      aud: "omi-screen-frame-writer",
      jti,
      uid,
      conversation_id: "meeting",
      attempt_id: attempt,
      epoch: 0,
      purpose: "meeting_note_v1",
      retention: "with_subject",
      decision: "approved_clean",
      model: "@cf/qwen/qwen3.8-27b",
      policy_version: "fixture",
      prompt_version: "fixture",
      issued_at: now,
      expires_at: now + 600,
      canonical_sha256: digest,
      thumbnail_sha256: digest,
      metadata: {
        captured_at: new Date().toISOString(),
        caption: "Fixture",
        labels: [],
        source_badge: null,
        banner_suitability: 0,
        width: 2,
        height: 2,
        ground: { stops: ["#111111", "#222222"], is_neutral: true },
      },
    });
    statements.push(
      [
        "INSERT INTO cf_conversations(uid,id,created_at) VALUES (?,'meeting',?)",
        uid,
        now,
      ],
      [
        "INSERT INTO cf_screen_frame_sets(uid,conversation_id) VALUES (?,'meeting')",
        uid,
      ],
      [
        "INSERT INTO cf_screen_frame_attempts(uid,conversation_id,attempt_id,fingerprint,epoch,expires_at) VALUES (?,'meeting',?,?,0,?)",
        uid,
        attempt,
        "a".repeat(64),
        now + 86400,
      ]
    );
    const payload = {
      statements,
      key,
      uid,
      image: image.toString("base64"),
      approval,
      content: signed("screen-frame-content-v1", {
        uid,
        frame_id: jti,
        variant: "content",
        access: "owner",
        expires_at: now + 3600,
      }),
      publish: [
        [
          "UPDATE cf_screen_frame_sets SET frames_json=? WHERE uid=?",
          JSON.stringify([{ id: jti }]),
          uid,
        ],
      ],
      fence: [
        [
          "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES (?,?,?)",
          uid,
          now,
          now + 86400,
        ],
      ],
    };
    writeFileSync(resolve(directory, "payload.json"), JSON.stringify(payload));
    const wrapper = `import writer from ${JSON.stringify(
      resolve(root, "workers/screen-frame-writer/index.ts")
    )};
      export default {async fetch(request,env,context){
        const path=new URL(request.url).pathname;
        if(path==='/fixture/sql'){ const rows=await request.json(); await env.APP_DB.batch(rows.map(([sql,...values])=>env.APP_DB.prepare(sql).bind(...values)));return Response.json({ok:true}); }
        if(path==='/fixture/cleanup'){await writer.scheduled({},env);return Response.json({objects:(await env.SCREEN_FRAMES.list()).objects.length,receipts:(await env.APP_DB.prepare('SELECT count(*) AS count FROM cf_screen_frame_writes').first()).count});}
        return writer.fetch(request,env,context);
      }};`;
    const bundle = buildSync({
      stdin: {
        contents: wrapper,
        resolveDir: root,
        sourcefile: "fixture.ts",
        loader: "ts",
      },
      bundle: true,
      write: false,
      format: "esm",
      platform: "browser",
    }).outputFiles[0].text;
    writeFileSync(resolve(directory, "worker.mjs"), bundle);
    const invocation = pythonWorkerInvocation("api-core", ["dev"], {
      root,
      env: { ...process.env, CLOUDFLARE_PYODIDE_CACHE_DIR: directory },
    });
    const source = `import {readFileSync} from 'node:fs';import assert from 'node:assert/strict';import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
      const data=JSON.parse(readFileSync(${JSON.stringify(
        resolve(directory, "payload.json")
      )},'utf8'));
      const mf=new Miniflare(convertV4MiniflareOptions({modules:true,cf:false,script:readFileSync(${JSON.stringify(
        resolve(directory, "worker.mjs")
      )},'utf8'),compatibilityDate:'2026-08-27',r2Buckets:['SCREEN_FRAMES'],d1Databases:['APP_DB'],bindings:{SCREEN_FRAME_SIGNING_SECRET:data.key,INTERNAL_ASSERTION_SECRET:data.key+"-internal"}}));
      try{const base=await mf.ready;
        const sql=async rows=>{const r=await fetch(new URL('/fixture/sql',base),{method:'POST',body:JSON.stringify(rows)});assert.equal(r.status,200,await r.text());};
        for(let i=0;i<data.statements.length;i+=40)await sql(data.statements.slice(i,i+40));
        const put=await fetch(new URL('/internal/screen-frames/write',base),{method:'POST',body:JSON.stringify({approval:data.approval,jpeg_base64:data.image,thumbnail_base64:data.image})});assert.equal(put.status,201,await put.text());
        await sql(data.publish);
        const url=new URL('/v1/screen-frame-content?token='+data.content,base);
        const got=await fetch(url);assert.equal(got.status,200);assert.equal(Buffer.from(await got.arrayBuffer()).toString('base64'),data.image);
        await sql(data.fence);assert.equal((await fetch(url)).status,404);
        const erased=await fetch(new URL('/fixture/cleanup',base));assert.equal(erased.status,200);assert.deepEqual(await erased.json(),{objects:0,receipts:0});
        console.log(JSON.stringify({write:201,content:200,revoked:404,objects:0,receipts:0}));
      }finally{await mf.dispose();}`;
    const result = runReleaseProcess(
      process.execPath,
      ["--input-type=module", "-e", source],
      { cwd: root, env: invocation.env, encoding: "utf8", timeout: 20000 }
    );
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({
      write: 201,
      content: 200,
      revoked: 404,
      objects: 0,
      receipts: 0,
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}, 30000);
