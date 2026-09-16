"""Controlled OpenAI-compatible fakes for the hosted operator smoke self-check.

These fakes verify the exact request shapes and response decoders the live
smoke runs, without any network. The live run replaces the transport with the
real vendor; no assertion here is about vendor behavior, only about what the
fork's own call builders send and accept.
"""

from __future__ import annotations

import io
import json
import wave

import httpx


def mono_wav_bytes(seconds=1, sample_rate=16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\x00\x01" * sample_rate * seconds)
    return buffer.getvalue()


def fake_transport(vendor, spec):
    def handler(request):
        url = str(request.url)
        if url.endswith("/chat/completions"):
            assert request.headers.get("authorization", "").startswith("Bearer ")
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": spec.model,
                        "choices": [{"finish_reason": "stop", "message": {"content": "ready"}}],
                    }
                ).encode(),
            )
        if url.endswith("/embeddings"):
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": spec.embedding_model,
                        "data": [{"index": 0, "embedding": [0.5] * spec.embedding_dimension}],
                    }
                ).encode(),
            )
        if url.endswith("/audio/transcriptions"):
            assert b'name="model"' in request.content
            assert spec.asr_model.encode() in request.content
            return httpx.Response(
                200,
                content=json.dumps({"text": "fake transcript", "usage": {"seconds": 1.0}}).encode(),
            )
        if url.endswith("/audio/speech"):
            return httpx.Response(200, content=mono_wav_bytes(1))
        if url.endswith("/run"):
            # The Cloudflare account REST envelope: ASR answers inside the
            # result wrapper; TTS returns the audio bytes directly.
            body = json.loads(request.content)
            assert request.headers.get("cf-aig-gateway-id") == spec.gateway_id
            if body["model"] == spec.tts_model:
                return httpx.Response(200, content=mono_wav_bytes(1))
            return httpx.Response(
                200,
                content=json.dumps({"result": {"text": "fake transcript"}, "success": True}).encode(),
            )
        if "/run/" in url and "@cf/" in url:
            # The Cloudflare direct REST endpoint: model id in the path, the
            # input object as the body.
            assert request.headers.get("cf-aig-gateway-id") == spec.gateway_id
            if url.endswith("/" + spec.tts_model):
                return httpx.Response(200, content=mono_wav_bytes(1), headers={"content-type": "audio/mpeg"})
            if url.endswith("/" + spec.asr_model):
                body = json.loads(request.content)
                assert "audio" in body
                return httpx.Response(
                    200,
                    content=json.dumps({"result": {"text": "fake transcript"}, "success": True}).encode(),
                )
            raise AssertionError("unexpected smoke model path: " + url)
        raise AssertionError("unexpected smoke URL: " + url)

    return httpx.MockTransport(handler)
