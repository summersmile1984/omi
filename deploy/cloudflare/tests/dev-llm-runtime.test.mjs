import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { buildSync } from "esbuild";
import { expect, it } from "vitest";
import { pythonWorkerInvocation } from "../scripts/python-worker.mjs";
import { runReleaseProcess } from "../scripts/release-wrangler.mjs";
import { pcm16Wav, bytesBase64 } from "../contracts/dev-audio.mjs";

// The 2026-09-08 live run passed in Node but failed in pinned workerd because
// fetch rejected redirect:error. Exercise the real RPC adapter/runtime with
// an internal outbound Worker; no external API, credential, model or network.
it("executes dev AI.run in workerd and rejects redirects without forwarding credentials", () => {
  const root = resolve(import.meta.dirname, "..");
  const cache = mkdtempSync(resolve(tmpdir(), "dev-llm-workerd-"));
  try {
    const provider = buildSync({
      entryPoints: [resolve(root, "contracts/provider-dev.ts")],
      bundle: true,
      format: "esm",
      write: false,
      external: ["cloudflare:workers"],
    }).outputFiles[0].text;
    const invocation = pythonWorkerInvocation("api-core", ["dev"], {
      root,
      env: { ...process.env, CLOUDFLARE_PYODIDE_CACHE_DIR: cache },
    });
    const caller = `export default {async fetch(request,env){try{
      const path=new URL(request.url).pathname;
      if(path==='/tts'){
        const audio=await env.AI.run('@cf/deepgram/aura-1',{text:'hello',encoding:'mp3'},{returnRawResponse:true});
        return Response.json({bytes:(await audio.arrayBuffer()).byteLength,type:audio.headers.get('content-type')});
      }
      if(path==='/embedding')return Response.json(await env.AI.run('@cf/baai/bge-m3',{text:['hello']}));
      if(path==='/asr')return Response.json(await env.AI.run('@cf/openai/whisper-large-v3-turbo',{audio:${JSON.stringify(
        bytesBase64(pcm16Wav(new Uint8Array(320), 16000))
      )}}));
      return Response.json(await env.AI.run('@cf/qwen/qwen3.8-27b',{messages:[{role:'user',content:path}],max_tokens:64}));
    }catch(e){return Response.json({error:e.message},{status:502});}}}`;
    const upstream = `export default {async fetch(request){
      if(!['model.test','127.0.0.1'].includes(new URL(request.url).hostname))throw new Error('unexpected redirect');
      const body=await request.json();
      if(body.model==='bge-m3'){
        if(request.headers.has('authorization'))throw new Error('MiMo key leaked to Ollama');
        return Response.json({model:'bge-m3',embeddings:[Array(1024).fill(0.1)]});
      }
      if(request.headers.get('authorization')!=='Bearer fixture-key')return new Response(null,{status:401});
      if(body.model==='mimo-v2.5-tts')return Response.json({model:body.model,choices:[{finish_reason:'stop',message:{audio:{data:'SUQzBAAAAAAAAA=='}}}]});
      if(body.model==='mimo-v2.5-asr')return Response.json({model:body.model,choices:[{finish_reason:'stop',message:{content:'recognized audio'}}],usage:{seconds:1}});
      if(body.messages[0].content==='/redirect')return new Response(null,{status:302,headers:{location:'https://outside.test/steal'}});
      return Response.json({choices:[{message:{content:'runtime answer'},finish_reason:'stop'}],usage:{prompt_tokens:3,completion_tokens:2,total_tokens:5}});
    }}`;
    const source = `import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
      const mf=new Miniflare(convertV4MiniflareOptions({cf:false,workers:[
        {name:'caller',modules:true,compatibilityDate:'2026-08-27',
          serviceBindings:{AI:{name:'adapter',entrypoint:'Provider'}},
          script:${JSON.stringify(caller)}},
        {name:'adapter',modules:true,compatibilityDate:'2026-08-27',script:${JSON.stringify(
          provider
        )},outboundService:'upstream',
          bindings:{DEV_LLM_URL:'https://model.test/v1/chat/completions',DEV_LLM_MODEL:'mimo-v2.5',DEV_LLM_PROTOCOL:'mimo',DEV_LLM_API_KEY:'fixture-key',DEV_OLLAMA_URL:'http://127.0.0.1:11434/api/embed',DEV_EMBEDDING_MODEL:'bge-m3'}},
        {name:'upstream',modules:true,compatibilityDate:'2026-08-27',script:${JSON.stringify(
          upstream
        )}}
      ]}));
      try {const origin=await mf.ready;const results=[];for(const path of ['/chat','/redirect','/tts','/embedding','/asr']){const response=await fetch(new URL(path,origin));results.push({status:response.status,body:await response.json()});}console.log('RESULT '+JSON.stringify(results));}
      finally {await mf.dispose();}`;
    const result = runReleaseProcess(
      process.execPath,
      ["--input-type=module", "-e", source],
      {
        cwd: root,
        env: invocation.env,
        encoding: "utf8",
        timeout: 15000,
      }
    );
    expect(result.status, result.stderr).toBe(0);
    const line = result.stdout
      .split("\n")
      .find((line) => line.startsWith("RESULT "));
    const [success, redirect, tts, embedding, asr] = JSON.parse(line.slice(7));
    expect(success.status).toBe(200);
    expect(success.body).toMatchObject({
      response: "runtime answer",
      usage: { total_tokens: 5 },
    });
    expect(redirect).toEqual({
      status: 502,
      body: { error: "development LLM HTTP 302" },
    });
    expect(tts).toEqual({
      status: 200,
      body: { bytes: 10, type: "audio/mpeg" },
    });
    expect(embedding.status).toBe(200);
    expect(embedding.body.shape).toEqual([1, 1024]);
    expect(asr).toMatchObject({
      status: 200,
      body: { text: "recognized audio" },
    });
  } finally {
    rmSync(cache, { recursive: true, force: true });
  }
}, 20000);
