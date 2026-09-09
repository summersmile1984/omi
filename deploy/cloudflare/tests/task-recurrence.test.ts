/** Actual App SQL and Jobs dispatch; Core response is controlled at the binding. */
import {readFileSync,readdirSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {afterEach,expect,it,vi} from "vitest";
import type {Message,MessageBatch} from "@cloudflare/workers-types";
import jobs from "../workers/jobs/index";
import type {JobMessage,JobsEnv} from "../workers/jobs/env";
import {processTaskRecurrenceMessage,reconcileTaskRecurrence} from "../workers/jobs/task-recurrence";
import {verifyRequestAuthContext} from "../workers/shared/auth-context";
const close:Array<()=>void>=[];
afterEach(()=>{for(const fn of close.splice(0))fn();vi.restoreAllMocks();});
function setup(){
 const db=new DatabaseSync(":memory:");close.push(()=>db.close());
 const dir=fileURLToPath(new URL("../migrations/app/",import.meta.url));
 for(const file of readdirSync(dir).filter(f=>f.endsWith('.sql')).sort())db.exec(readFileSync(path.join(dir,file),'utf8'));
 const database={prepare(sql:string){const build=(args:unknown[]=[]):any=>({bind:(...values:unknown[])=>build(values),first:async()=>db.prepare(sql).get(...args as never[])??null,all:async()=>({results:db.prepare(sql).all(...args as never[])}),run:async()=>({meta:{changes:Number(db.prepare(sql).run(...args as never[]).changes)}})});return build();}};
 const sent:JobMessage[]=[];
 const env={APP_DB:database,INTERNAL_ASSERTION_SECRET:'recurrence-job-test-secret',JOBS:{send:vi.fn(async (body:JobMessage)=>{sent.push(body);})},API_CORE:{fetch:vi.fn(async()=>Response.json({status:'completed'}))}} as unknown as JobsEnv;
 const seed=(uid='owner',status='pending')=>{
  const expected=JSON.stringify([{id:'receipt-1',before:null}]);
  db.prepare('INSERT INTO cf_candidate_write_guard(uid,account_generation,recurrences_json) VALUES (?,0,?)').run(uid,expected);
  db.prepare('INSERT INTO cf_task_recurrence_inbox(uid,receipt_id,record_json) VALUES (?,?,?)').run(uid,'receipt-1',JSON.stringify({receipt_id:'receipt-1',account_generation:0,status,updated_at:'2026-09-08T00:00:00Z'}));
  db.prepare('DELETE FROM cf_candidate_write_guard WHERE uid=?').run(uid);
 };
 const message=(uid='owner')=>({id:'delivery',timestamp:new Date(),attempts:1,body:{uid,jobId:'receipt-1',kind:'task_recurrence',payload:{}},ack:vi.fn(),retry:vi.fn()} as Message<JobMessage>);
 return {db,env,sent,seed,message};
}
it('rediscovers unsent receipts and continues after a different owner queue failure',async()=>{
 const t=setup();t.seed('a');t.seed('b');
 vi.mocked(t.env.JOBS.send).mockRejectedValueOnce(new Error('controlled queue outage'));
 await expect(reconcileTaskRecurrence(t.env)).rejects.toThrow('queue unavailable');
 expect(t.sent.map(r=>r.uid)).toEqual(['b']);
 await reconcileTaskRecurrence(t.env);
 expect(t.sent.map(r=>r.uid)).toEqual(['b','a','b']);
});
it('actual Jobs queue sends an internal signed receipt to Core before acknowledging',async()=>{
 const t=setup();t.seed();
 vi.mocked(t.env.API_CORE!.fetch).mockImplementation(async request=>{
  const r=request as Request;const context=await verifyRequestAuthContext(r,'api-core',t.env.INTERNAL_ASSERTION_SECRET);
  expect(context?.uid).toBe('owner');expect(context?.authority).toBe('internal');
  expect(new URL(r.url).pathname).toBe('/internal/task-intelligence/recurrence');
  expect(await r.json()).toEqual({receipt_id:'receipt-1',account_generation:0});
  return Response.json({status:'completed'});
 });
 const m=t.message();await jobs.queue({queue:'eddy-jobs-production',messages:[m]} as unknown as MessageBatch<JobMessage>,t.env);
 expect(m.ack).toHaveBeenCalledOnce();expect(m.retry).not.toHaveBeenCalled();
});
it.each([503,200])('retains a receipt when Core returns an error or incomplete result (%s)',async status=>{
 const t=setup();t.seed();vi.mocked(t.env.API_CORE!.fetch).mockResolvedValue(Response.json({status:'pending'},{status}));
 const m=t.message();await processTaskRecurrenceMessage(m,t.env);
 expect(m.ack).not.toHaveBeenCalled();expect(m.retry).toHaveBeenCalledWith({delaySeconds:10});
 expect(t.db.prepare('SELECT status FROM cf_task_recurrence_inbox').get()).toEqual({status:'pending'});
});
it('does not deliver completed, obsolete, deleted or another owner receipts',async()=>{
 const t=setup();t.seed('completed','completed');t.seed('old');t.seed('deleted');
 t.db.exec("INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('old',1,1)");
 t.db.exec("INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('deleted',1,9999999999)");
 await reconcileTaskRecurrence(t.env);expect(t.sent).toEqual([]);
 for(const uid of ['completed','old','deleted','other']){const m=t.message(uid);await processTaskRecurrenceMessage(m,t.env);expect(m.ack).toHaveBeenCalledOnce();}
 expect(t.env.API_CORE!.fetch).not.toHaveBeenCalled();
});
it('captures recurrence deliveries in the existing replayable DLQ',async()=>{
 const t=setup();t.seed();const m=t.message();
 await jobs.queue({queue:'omi-cf-jobs-dlq-production',messages:[m]} as unknown as MessageBatch<JobMessage>,t.env);
 expect(m.ack).toHaveBeenCalledOnce();
 expect(t.db.prepare('SELECT kind,status,invalid_reason FROM cf_queue_dlq_messages').get()).toEqual({kind:'task_recurrence',status:'captured',invalid_reason:null});
});
