#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Live smoke for the hosted operator AI: one real call per capability.

--live <vendor> performs four real network calls (chat, embeddings, batch ASR,
TTS) against the vendor's public OpenAI-compatible endpoint using the same
frozen specs, credential environment variables, egress grants and profile
chain the deployed backend uses, and records sanitized evidence JSON: model
echo, embedding dimension, audio envelope and durations only. Provider keys,
transcripts and audio bytes never enter the record.

--self-check runs the identical call builders against controlled fake
transports with no network, hermetic for CI. Live runs stay outside CI and are
recorded per vendor exactly as the other acceptance probes are.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/profiles"))
sys.path.insert(0, str(ROOT / "backend"))

ASR_SAMPLE_RATE = 16000
SCHEMA_VERSION = 1


def loaded_manifest(path):
    """The validated manifest document, private overlays included."""
    import sys as sys_module

    sys_module.path.insert(0, str(ROOT / "scripts/brand"))
    from manifest import load_manifest

    if path == ROOT / "brand/eddy/manifest.yaml":
        return load_manifest("eddy", ROOT)
    return load_manifest(None, ROOT, path)


def render_profile(
    vendor: str, stage: str, manifest: str | None, out_dir: Path, *, synthesize_gateway: bool = False
) -> dict:
    """Render the hosted table through the real renderer, then point the fork
    profile loader at it. Only the self-check may synthesize the Cloudflare
    gateway identity; a live run requires the operator's real ids."""
    import json

    from render import resolve

    manifest_path = Path(manifest) if manifest else ROOT / "brand/eddy/manifest.yaml"
    if vendor == "cloudflare-gateway" and synthesize_gateway:
        manifest_doc = loaded_manifest(manifest_path)
        if not manifest_doc.get("cloudflare_ai_gateway"):
            manifest_doc = {
                **manifest_doc,
                "cloudflare_ai_gateway": {"account_id": "0" * 32, "gateway_id": "smoke-check"},
            }
            manifest_path = out_dir / "hosted-smoke-manifest.json"
            manifest_path.write_text(json.dumps(manifest_doc))
    resolved = resolve("self_hosted", None, manifest_path, stage, vendor)
    table_path = out_dir / "deployment_profiles.generated.json"
    table_path.write_text(
        json.dumps(
            {"_comment": "hosted-live-smoke rendered profile; never deployed", **resolved},
            indent=2,
            sort_keys=True,
        )
    )
    os.environ["OMI_DEPLOYMENT_PROFILES_PATH"] = str(table_path)
    os.environ["OMI_DEPLOYMENT_PROFILE"] = "self_hosted." + stage
    os.environ["OMI_DEPLOYMENT_TARGET"] = "self_hosted"
    from fork import profile

    profile.reset()
    row = profile.current()
    if row.get("target") != "self_hosted":
        raise SystemExit("hosted live smoke requires the self_hosted target")
    return row


def mono_wav_bytes(seconds=1, sample_rate=ASR_SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00\x01" * sample_rate * seconds)
    return buffer.getvalue()


def chat_call(spec, transport=None) -> dict:
    import time

    from fork import operator_ai
    from fork.egress_policy import assert_http_endpoint_allowed

    endpoint = spec.base_url + "/chat/completions"
    assert_http_endpoint_allowed(endpoint)
    started = time.monotonic()
    with httpx.Client(transport=transport, follow_redirects=False, timeout=spec.request_timeout_seconds) as client:
        response = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + operator_ai.credentials(),
                **operator_ai.gateway_headers(),
            },
            json={
                "model": spec.model,
                "messages": [{"role": "user", "content": "Reply with the single word ready."}],
                "max_tokens": 400,
                "temperature": 0,
            },
        )
    duration = time.monotonic() - started
    if response.status_code != 200:
        return {"status": f"http_{response.status_code}", "duration_seconds": round(duration, 2)}
    result = response.json()
    choice = (result.get("choices") or [{}])[0]
    return {
        "status": "ok",
        "model_echo": result.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "text_chars": len((choice.get("message") or {}).get("content") or ""),
        "duration_seconds": round(duration, 2),
    }


def embeddings_call(spec, transport=None) -> dict:
    import time

    from fork import operator_ai
    from fork.egress_policy import assert_http_endpoint_allowed

    endpoint = spec.embedding_base_url + "/embeddings"
    assert_http_endpoint_allowed(endpoint)
    started = time.monotonic()
    with httpx.Client(transport=transport, follow_redirects=False, timeout=spec.request_timeout_seconds) as client:
        response = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + operator_ai.embedding_credentials(),
                **operator_ai.gateway_headers(),
            },
            json={"model": spec.embedding_model, "input": ["hosted operator smoke"]},
        )
    duration = time.monotonic() - started
    if response.status_code != 200:
        return {"status": f"http_{response.status_code}", "duration_seconds": round(duration, 2)}
    result = response.json()
    vectors = result.get("data") or []
    dimension = len(vectors[0].get("embedding", [])) if vectors and isinstance(vectors[0], dict) else 0
    return {
        "status": "ok",
        "model_echo": result.get("model"),
        "vectors": len(vectors),
        "dimension": dimension,
        "dimension_matches_spec": dimension == spec.embedding_dimension,
        "duration_seconds": round(duration, 2),
    }


def tts_audio(spec, transport=None) -> bytes:
    from fork import hosted_speech

    return hosted_speech.synthesize("hosted operator smoke", transport=transport)


def tts_call(spec, transport=None) -> dict:
    audio = tts_audio(spec, transport)
    with wave.open(io.BytesIO(audio)) as decoded:
        envelope = {
            "channels": decoded.getnchannels(),
            "sampwidth": decoded.getsampwidth(),
            "frames": decoded.getnframes(),
        }
    return {"status": "ok", "envelope": envelope, "bytes": len(audio)}


def asr_call(spec, transport=None, audio=None) -> dict:
    from fork import hosted_speech

    probe = audio if audio is not None else mono_wav_bytes(1)
    result = hosted_speech.Client().transcribe_audio(probe, filename="smoke.wav", transport=transport)
    return {
        "status": "ok",
        "model": spec.asr_model,
        "input": "tts_output" if audio is not None else "silent_probe",
        "transcript_chars": len(result.text),
        "reported_seconds": result.duration,
    }


def _call_evidence(runner, spec, transport) -> dict:
    try:
        return runner(spec, transport)
    except Exception as error:  # noqa: BLE001 - evidence records the typed outcome only
        code = getattr(error, "code", None) or type(error).__name__
        return {"status": code, "retryable": bool(getattr(error, "retryable", False))}


def _run(
    vendor: str, stage: str, manifest: str | None, mode: str, out_path: Path | None, transport_factory=None
) -> int:
    with tempfile.TemporaryDirectory(prefix="omi-hosted-smoke-") as tmp:
        row = render_profile(vendor, stage, manifest, Path(tmp), synthesize_gateway=mode == "self-check")
        from fork import operator_ai

        spec = operator_ai.select(row)
        if spec is None or getattr(spec, "provider", "") != vendor:
            raise SystemExit(f"ERROR: rendered profile did not select {vendor}")
        credential_envs = [operator_ai.CREDENTIAL_ENV[vendor]]
        if vendor == operator_ai.CLOUDFLARE_GATEWAY:
            credential_envs.append(operator_ai.CLOUDFLARE_TOKEN_ENV)
        transport = None
        if transport_factory is not None:
            from hosted_smoke_fakes import fake_transport

            # The fake transport never leaves the process, so synthetic
            # credentials satisfy the same admission the deployment uses.
            for env in credential_envs:
                os.environ[env] = "smoke-check-credential"
            transport = fake_transport(vendor, spec)
        # TTS first: its WAV becomes the ASR probe, so the transcription check
        # exercises real audible speech rather than a silent tone.
        tts_evidence = _call_evidence(tts_call, spec, transport)
        asr_audio = None
        if tts_evidence.get("status") == "ok":
            try:
                asr_audio = tts_audio(spec, transport)
            except Exception:  # noqa: BLE001 - the ASR probe falls back to the silent wav
                asr_audio = None
        calls = {
            "chat": _call_evidence(chat_call, spec, transport),
            "embeddings": _call_evidence(embeddings_call, spec, transport),
            "asr": _call_evidence(lambda spec, transport: asr_call(spec, transport, audio=asr_audio), spec, transport),
            "tts": tts_evidence,
        }
        credential_envs = [operator_ai.CREDENTIAL_ENV[vendor]]
        if vendor == operator_ai.CLOUDFLARE_GATEWAY:
            credential_envs.append(operator_ai.CLOUDFLARE_TOKEN_ENV)
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "vendor": vendor,
            "stage": stage,
            "profile": row["name"],
            "spec": {
                "chat_model": spec.model,
                "embedding_model": spec.embedding_model,
                "embedding_dimension": spec.embedding_dimension,
                "asr_model": spec.asr_model,
                "tts_model": spec.tts_model,
                "credential_envs": credential_envs,
            },
            "calls": calls,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if out_path is not None:
            out_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
        failed = any(call.get("status") != "ok" for call in calls.values())
        if mode == "self-check":
            return 0 if not failed else 1
        if failed:
            print(f"FAIL: {vendor} live smoke had a failing call; evidence: {out_path}", file=sys.stderr)
            return 1
        print(f"OK: hosted operator {vendor} live smoke recorded: {out_path}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-check", action="store_true", help="hermetic wire-shape check; no network")
    parser.add_argument("--live", choices=("openrouter", "cloudflare-gateway", "siliconflow"))
    parser.add_argument("--stage", default="local", choices=("local", "beta", "production"))
    parser.add_argument("--manifest", help="explicit brand manifest path; cloudflare-gateway requires its ids")
    parser.add_argument("--evidence", type=Path, help="evidence JSON path (never committed by default)")
    args = parser.parse_args()
    if args.self_check and args.live:
        parser.error("--self-check and --live are mutually exclusive")
    if args.self_check:
        from hosted_smoke_fakes import fake_transport

        results = [
            _run(vendor, "local", args.manifest, "self-check", None, fake_transport)
            for vendor in ("openrouter", "cloudflare-gateway", "siliconflow")
        ]
        return 0 if all(result == 0 for result in results) else 1
    if not args.live:
        parser.error("one of --self-check or --live <vendor> is required")
    default = Path(os.environ.get("TMPDIR", "/tmp")) / f"omi-hosted-{args.live}-smoke.json"
    evidence = args.evidence or Path(os.environ.get("HOSTED_SMOKE_EVIDENCE", default))
    return _run(args.live, args.stage, args.manifest, "live", evidence)


if __name__ == "__main__":
    sys.exit(main())
