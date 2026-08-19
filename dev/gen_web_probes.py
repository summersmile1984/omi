#!/usr/bin/env python3
"""Generate a probe manifest from the backend OpenAPI spec.

Reads /tmp/omi-openapi.json and emits a JSON list of probe definitions:
    {tag, method, path, probe_path (path params filled), body (minimal JSON)}

GET/HEAD probes send no body; write probes get a minimal body built from the
requestBody schema (or {} when none / not JSON object). Auth is added by the
runner. The manifest is consumed by an ego-browser script that issues each
probe and records {method, path, status, note}.
"""
import json
import re
import sys

SPEC = "/tmp/omi-openapi.json"
OUT = "/tmp/web-probes.json"

PATH_PARAM_PLACEHOLDER = {
    # common path params -> values that route to "not found" cleanly
    "uid": "probe-user",
    "conversation_id": "probe-conv",
    "memory_id": "probe-memory",
    "review_id": "probe-review",
    "action_item_id": "probe-action",
    "goal_id": "probe-goal",
    "folder_id": "probe-folder",
    "person_id": "probe-person",
    "persona_id": "probe-persona",
    "app_id": "probe-app",
    "key_id": "probe-key",
    "summary_id": "probe-summary",
    "task_id": "probe-task",
    "wtype": "probe-webhook",
    "job_id": "probe-job",
    "session_id": "probe-session",
    "review_id": "probe-review",
}


def path_param_value(name: str) -> str:
    return PATH_PARAM_PLACEHOLDER.get(name, f"probe-{name}")


def minimal_body(schema: dict) -> dict:
    """Build a minimal JSON object from a requestBody schema (best-effort)."""
    if not schema:
        return {}
    ref = schema.get("$ref", "")
    if ref:
        return {}
    content = schema.get("content") or {}
    js = content.get("application/json") or {}
    sch = js.get("schema") or {}
    props = sch.get("properties") or {}
    required = sch.get("required") or []
    body = {}
    for name in required:
        prop = props.get(name, {})
        ptype = prop.get("type")
        if ptype == "string":
            body[name] = "probe-value"
        elif ptype == "integer":
            body[name] = 1
        elif ptype == "number":
            body[name] = 1.0
        elif ptype == "boolean":
            body[name] = True
        elif ptype == "array":
            body[name] = []
        elif ptype == "object":
            body[name] = {}
        else:
            body[name] = None
    return body


def main() -> None:
    spec = json.load(open(SPEC))
    probes = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            m = method.upper()
            if m == "HEAD":
                continue
            # fill path params
            probe_path = path
            for param in re.findall(r"\{([^}]+)\}", path):
                probe_path = probe_path.replace("{" + param + "}", path_param_value(param))
            tag = (op.get("tags") or ["untagged"])[0]
            body = None
            if m in ("POST", "PUT", "PATCH"):
                body = minimal_body(op.get("requestBody"))
            probes.append({
                "tag": tag,
                "method": m,
                "path": path,
                "probe_path": probe_path,
                "body": body,
            })
    json.dump(probes, open(OUT, "w"), indent=2)
    print(f"wrote {len(probes)} probes to {OUT}")
    # summary by tag
    from collections import Counter
    c = Counter(p["tag"] for p in probes)
    for tag, n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {n}")


if __name__ == "__main__":
    main()
