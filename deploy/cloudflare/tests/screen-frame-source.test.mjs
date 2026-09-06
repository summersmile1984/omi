import { expect, it } from "vitest";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

const repository = resolve(import.meta.dirname, "../../..");
const python = resolve(repository, "backend/.venv/bin/python");
const generator = resolve(
  repository,
  "deploy/cloudflare/scripts/screen_frame_sources.py"
);

it("executes the staged screenshot wire, codec, prompt and survivor policy from upstream sources", () => {
  const stage = mkdtempSync(resolve(tmpdir(), "screen-frame-source-"));
  try {
    const generated = spawnSync(python, [generator, "--output", stage], {
      encoding: "utf8",
    });
    expect(generated.status, generated.stderr).toBe(0);
    expect(readFileSync(resolve(stage, "screen_frames_contract.py"))).toEqual(
      readFileSync(resolve(repository, "backend/models/screen_frame.py"))
    );
    expect(readFileSync(resolve(stage, "screen_frames_canonical.py"))).toEqual(
      readFileSync(
        resolve(repository, "backend/utils/screen_frames/canonicalize.py")
      )
    );
    const result = spawnSync(
      python,
      [
        "-c",
        `
import ast,hashlib,io,json,logging,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from PIL import Image
from screen_frames_canonical import canonicalize_candidate,ScreenFrameCanonicalizationError
from screen_frames_contract import ScreenFrameJudgement,ScreenFrameCandidateIn
from screen_frames_palette import compute_ground
from screen_frames_prompt import _PRIVACY_PROMPT
from screen_frames_selection import _apply_cap_and_roles
from screen_frames_transport import decode_and_verify_transport_digest,ScreenFrameDigestMismatch
from screen_frames_admission import _validate_capture_window,CAPTURE_WINDOW_SLACK_SECONDS
from datetime import datetime,timezone,timedelta
import base64
raw=io.BytesIO();Image.new('RGB',(1920,1080),'#24405A').save(raw,format='PNG')
frame=canonicalize_candidate(raw.getvalue())
assert (frame.width,frame.height)==(1600,900)
assert frame.sha256_hex==hashlib.sha256(frame.jpeg_bytes).hexdigest()
assert compute_ground(frame.jpeg_bytes).model_dump()['stops']
stamp=datetime(2026,9,6,tzinfo=timezone.utc)
candidate=ScreenFrameCandidateIn(client_frame_id='fixture',captured_at=stamp,mime_type='image/png',declared_width=1920,declared_height=1080,sha256_base64=base64.b64encode(hashlib.sha256(raw.getvalue()).digest()).decode(),bytes_base64=base64.b64encode(raw.getvalue()).decode())
assert decode_and_verify_transport_digest(candidate)==raw.getvalue()
assert CAPTURE_WINDOW_SLACK_SECONDS==120
_validate_capture_window({'started_at':stamp+timedelta(seconds=120),'finished_at':stamp},[candidate])
candidate.bytes_base64='invalid!'
try:
 decode_and_verify_transport_digest(candidate)
 raise AssertionError('invalid transport admitted')
except ScreenFrameDigestMismatch: pass
try:
 canonicalize_candidate(b'corrupt')
 raise AssertionError('corrupt image admitted')
except ScreenFrameCanonicalizationError: pass
# Evaluate the original module's constant assignments independently, with its
# actual policy constant; provider imports and judge calls are never executed.
policy_tree=ast.parse((Path(sys.argv[2])/'backend/utils/screen_frames/policy.py').read_text())
scope={'logging':logging}
for node in policy_tree.body:
 if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='REJECT_IDENTIFIABLE_PERSONS' for t in node.targets):
  exec(compile(ast.Module(body=[node],type_ignores=[]),'upstream-policy','exec'),scope)
tree=ast.parse((Path(sys.argv[2])/'backend/utils/screen_frames/judge.py').read_text())
for node in tree.body:
 if isinstance(node,ast.Assign):
  exec(compile(ast.Module(body=[node],type_ignores=[]),'upstream-judge','exec'),scope)
assert _PRIVACY_PROMPT==scope['_PRIVACY_PROMPT']
verdict=ScreenFrameJudgement(outcome='approved_clean',caption='a'*200,labels=list('abcdefghij'),banner_suitability=.5)
assert len(verdict.caption)==160 and len(verdict.labels)==8
rows=[dict(id=str(i),captured_at=i,banner_suitability=i/10) for i in range(10)]
survivors,evicted=_apply_cap_and_roles([],rows,7)
assert [r['id'] for r in survivors]==list('3456789')
assert [r['id'] for r in evicted]==list('012')
assert [r['id'] for r in survivors if r['role']=='banner']==['9']
print(json.dumps({'codec':'pass','prompt':'unchanged','selection':'pass','wire':'pass'}))
`,
        stage,
        repository,
      ],
      { encoding: "utf8" }
    );
    expect(result.status, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({
      codec: "pass",
      prompt: "unchanged",
      selection: "pass",
      wire: "pass",
    });
    const collision = spawnSync(python, [generator, "--output", stage], {
      encoding: "utf8",
    });
    expect(collision.status).not.toBe(0);
    expect(collision.stderr).toContain("collides with a staged source owner");
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
});

it("builds executable upstream frame-request types and decorated JIT policy", () => {
  const stage = mkdtempSync(resolve(tmpdir(), "frame-request-source-"));
  const projector = resolve(
    repository,
    "deploy/cloudflare/scripts/frame_request_sources.py"
  );
  try {
    const generated = spawnSync(python, [projector, "--output", stage], {
      encoding: "utf8",
    });
    expect(generated.status, generated.stderr).toBe(0);
    expect(readFileSync(resolve(stage, "frame_request_contract.py"))).toEqual(
      readFileSync(resolve(repository, "backend/models/frame_request.py"))
    );
    expect(
      readFileSync(resolve(stage, "frame_request_policy.py"), "utf8")
    ).toBe(
      readFileSync(
        resolve(repository, "backend/utils/retrieval/frame_request_policy.py"),
        "utf8"
      ).replace(
        "from models.frame_request import ",
        "from frame_request_contract import "
      )
    );
    const executed = spawnSync(
      python,
      [
        "-c",
        `
import sys
from dataclasses import asdict,is_dataclass
from datetime import datetime,timezone,timedelta
sys.path.insert(0,sys.argv[1])
from frame_request_policy import request_expiry
from jit_policy import JITFlagEvaluation,JITDecisionReason,JITErrorClass,TriState,_effective_decision
assert is_dataclass(JITFlagEvaluation)
evaluation=JITFlagEvaluation(TriState.ENABLED,TriState.ENABLED,JITDecisionReason.EVALUATED,JITErrorClass.NONE)
decision=_effective_decision(evaluation,allowlisted=True,cache_hit=False,cache_ttl_seconds=0)
assert not decision.permits_work and asdict(decision)['effective']=='disabled'
stamp=datetime(2026,9,6,tzinfo=timezone.utc)
assert request_expiry(created_at=stamp,requested_ttl_seconds=999999,device_retention_seconds=None)==stamp+timedelta(days=6)
assert request_expiry(created_at=stamp,requested_ttl_seconds=1000,device_retention_seconds=60)==stamp+timedelta(seconds=60)
`,
        stage,
      ],
      { encoding: "utf8" }
    );
    expect(executed.status, executed.stderr).toBe(0);
    const collision = spawnSync(python, [projector, "--output", stage], {
      encoding: "utf8",
    });
    expect(collision.status).not.toBe(0);
    expect(collision.stderr).toContain("collides with a staged source owner");
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
});
