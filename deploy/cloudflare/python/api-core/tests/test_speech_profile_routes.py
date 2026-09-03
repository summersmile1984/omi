import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from speech_profile_routes import (  # noqa: E402
    delete_extra_speech_profile_sample,
    download_speech_profile_audio,
    get_extra_speech_profile_samples,
    get_speech_profile,
    get_speech_profile_status,
    has_speech_profile,
    upload_profile,
)

SECRET = "speech-profile-test-secret"
UID = "speech-profile-user"


def signed_headers(uid=UID):
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"uid": uid, "authority": "better-auth", "requestId": "speech-profile-test"}, separators=(",", ":")
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = (
        base64.urlsafe_b64encode(hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )
    return {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature}


def wav(*, seconds=5, sample_rate=16_000, channels=1, bits_per_sample=16):
    sample_width = bits_per_sample // 8
    data = b"\0" * (seconds * sample_rate * channels * sample_width)
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


def multipart(audio, boundary="speech-profile-boundary"):
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="speaker_profile.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


class FakeStored:
    def __init__(self, value):
        self.value = value

    async def arrayBuffer(self):
        return self.value


class RawRpcValue:
    def __init__(self, value):
        self.value = value

    def to_py(self):
        return self.value


class RpcWrappedValue:
    def __init__(self, value):
        self._binding = RawRpcValue(value)

    def __getattr__(self, name):
        if name == "to_py":
            return lambda: RpcWrappedValue(self._binding.value)
        value = self._binding.value
        if isinstance(value, dict) and name in value:
            return value[name]
        raise AttributeError(name)


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.deleted = []
        self.fail_put = False
        self.rpc_wrapped = False

    def result(self, value):
        return RpcWrappedValue(value) if self.rpc_wrapped else value

    async def head(self, key):
        if key not in self.objects:
            return None
        return self.result(
            {
                "key": key,
                "size": len(self.objects[key]),
                "customMetadata": self.metadata.get(key, {}),
            }
        )

    async def put(self, key, value, *, httpMetadata=None, customMetadata=None):
        if self.fail_put:
            raise RuntimeError("R2 unavailable")
        self.objects[key] = bytes(value)
        self.metadata[key] = dict(customMetadata or {})
        return {"key": key, "httpMetadata": httpMetadata}

    async def get(self, key, options=None):
        value = self.objects.get(key)
        if value is None:
            return None
        if options:
            byte_range = options["range"]
            start = int(byte_range["offset"])
            value = value[start : start + int(byte_range["length"])]
        return FakeStored(value)

    async def list(self, options):
        prefix = options["prefix"]
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return self.result({"objects": [{"key": key} for key in keys], "truncated": False})

    async def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)
        self.metadata.pop(key, None)


class FakeAi:
    def __init__(self, response=None):
        self.response = {"text": "My voice profile", "segments": []} if response is None else response
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeRequest:
    def __init__(self, env, *, body=b"", headers=None, query=None, authenticated=True, path="/v3/upload-audio"):
        self.scope = {"env": env}
        self._body = body
        self.headers = (signed_headers() if authenticated else {}) | (headers or {})
        self.query_params = query or {}
        self.url = f"https://omi.example.test{path}"

    async def body(self):
        return self._body


def environment(*, ai_response=None):
    return SimpleNamespace(
        SPEECH_PROFILES=FakeBucket(),
        AI=FakeAi(ai_response),
        INTERNAL_ASSERTION_SECRET=SECRET,
        WORKERS_AI_ASR_MODEL="@cf/openai/whisper-test",
    )


async def response_bytes(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def test_profile_upload_read_status_list_delete_and_signed_playback_round_trip():
    env = environment()
    audio = wav()
    body, content_type = multipart(audio)
    request = FakeRequest(
        env,
        body=body,
        headers={"content-type": content_type, "content-length": str(len(body))},
    )

    uploaded = asyncio.run(upload_profile(request))
    assert uploaded["url"].startswith("https://omi.example.test/v3/speech-profile/audio?token=")
    assert env.SPEECH_PROFILES.objects[f"{UID}/speech_profile.wav"] == audio
    assert env.SPEECH_PROFILES.metadata[f"{UID}/speech_profile.wav"]["duration_seconds"] == "10.000000"
    assert env.AI.calls[0][0] == "@cf/openai/whisper-test"
    assert base64.b64decode(env.AI.calls[0][1]["audio"]) == audio
    assert env.AI.calls[0][1] | {"audio": "redacted"} == {
        "audio": "redacted",
        "vad_filter": True,
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.5,
        "hallucination_silence_threshold": 1.0,
    }

    read_request = FakeRequest(env, path="/v4/speech-profile")
    assert asyncio.run(has_speech_profile(read_request)) == {"has_profile": True}
    profile = asyncio.run(get_speech_profile(read_request))
    assert profile["url"].startswith("https://omi.example.test/v3/speech-profile/audio?token=")

    additional_key = f"{UID}/additional_profile_recordings/memory-1_segment_2.wav"
    person_key = f"{UID}/people_profiles/person-1/memory-2_segment_3.wav"
    env.SPEECH_PROFILES.objects[additional_key] = b"additional"
    env.SPEECH_PROFILES.objects[person_key] = b"person"
    env.SPEECH_PROFILES.rpc_wrapped = True
    status = asyncio.run(get_speech_profile_status(read_request))
    assert status["has_profile"] is True
    assert status["duration_seconds"] == 10.0
    assert status["sample_count"] == 1
    assert status["url"].startswith("https://omi.example.test/v3/speech-profile/audio?token=")

    additional = asyncio.run(get_extra_speech_profile_samples(read_request))
    person = asyncio.run(get_extra_speech_profile_samples(read_request, person_id="person-1"))
    assert len(additional) == 1
    assert len(person) == 1

    token = parse_qs(urlsplit(profile["url"]).query)["token"][0]
    download_request = FakeRequest(
        env,
        authenticated=False,
        query={"token": token},
        headers={"range": "bytes=0-11"},
        path="/v3/speech-profile/audio",
    )
    downloaded = asyncio.run(download_speech_profile_audio(download_request))
    assert downloaded.status_code == 206
    assert downloaded.headers["content-range"] == f"bytes 0-11/{len(audio)}"
    assert asyncio.run(response_bytes(downloaded)) == audio[:12]

    deleted = asyncio.run(delete_extra_speech_profile_sample(read_request, "memory-1", 2, person_id="null"))
    assert deleted == {"status": "ok"}
    assert additional_key not in env.SPEECH_PROFILES.objects
    deleted_person = asyncio.run(delete_extra_speech_profile_sample(read_request, "memory-2", 3, person_id="person-1"))
    assert deleted_person == {"status": "ok"}
    assert person_key not in env.SPEECH_PROFILES.objects


def test_profile_upload_rejects_invalid_audio_and_fails_closed_before_storage():
    env = environment()

    wrong_rate_body, wrong_rate_type = multipart(wav(sample_rate=8_000))
    wrong_rate = asyncio.run(
        upload_profile(FakeRequest(env, body=wrong_rate_body, headers={"content-type": wrong_rate_type}))
    )
    assert wrong_rate.status_code == 400
    assert json.loads(wrong_rate.body)["detail"] == "Invalid codec, must be opus 16khz."

    short_body, short_type = multipart(wav(seconds=4))
    too_short = asyncio.run(upload_profile(FakeRequest(env, body=short_body, headers={"content-type": short_type})))
    assert too_short.status_code == 400
    assert "5-120" in json.loads(too_short.body)["detail"]

    env.AI.response = {"text": "", "segments": []}
    valid_body, valid_type = multipart(wav())
    empty = asyncio.run(upload_profile(FakeRequest(env, body=valid_body, headers={"content-type": valid_type})))
    assert empty.status_code == 400
    assert json.loads(empty.body) == {"detail": "Audio is empty"}
    assert env.SPEECH_PROFILES.objects == {}

    env.AI.response = RuntimeError("Workers AI unavailable")
    unavailable = asyncio.run(upload_profile(FakeRequest(env, body=valid_body, headers={"content-type": valid_type})))
    assert unavailable.status_code == 502
    assert env.SPEECH_PROFILES.objects == {}


def test_profile_routes_enforce_auth_tenant_keys_and_signed_token_integrity():
    env = environment()
    unauthenticated = FakeRequest(env, authenticated=False)
    assert asyncio.run(has_speech_profile(unauthenticated)).status_code == 401
    assert asyncio.run(get_speech_profile(unauthenticated)).status_code == 401
    assert asyncio.run(get_speech_profile_status(unauthenticated)).status_code == 401
    assert asyncio.run(get_extra_speech_profile_samples(unauthenticated)).status_code == 401

    invalid_id = asyncio.run(delete_extra_speech_profile_sample(FakeRequest(env), "../other-user", 0))
    assert invalid_id.status_code == 400
    assert env.SPEECH_PROFILES.deleted == []

    audio = wav()
    env.SPEECH_PROFILES.objects[f"{UID}/speech_profile.wav"] = audio
    profile = asyncio.run(get_speech_profile(FakeRequest(env, path="/v4/speech-profile")))
    token = parse_qs(urlsplit(profile["url"]).query)["token"][0]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    rejected = asyncio.run(
        download_speech_profile_audio(
            FakeRequest(
                env,
                authenticated=False,
                query={"token": tampered},
                path="/v3/speech-profile/audio",
            )
        )
    )
    assert rejected.status_code == 401
