"""The speaker-embedding provider selection must actually take effect.

Before this patch existed the fork read `SPEAKER_EMBEDDING_PROVIDER`, validated
it, and then ignored it: upstream's implementation posts to
`{HOSTED_SPEAKER_EMBEDDING_API_URL}/v2/embedding` unconditionally. An operator who
mounted a local ONNX model got a passing configuration check and every second of
audio sent off the machine anyway, with no error and no log line. These tests
assert the dispatch by observing which side actually ran.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

import utils.stt.speaker_embedding as upstream
from fork.patches.speaker_embedding import patches
from fork.registry import build_registry
from fork.speaker_embedding import SpeakerEmbeddingUnavailable

SELF_HOSTED = {
    "name": "self_hosted.production",
    "target": "self_hosted",
    "data_plane": {"object_store": "minio", "queue": "redis", "store": "firestore_pg"},
}

PATCHED_ATTRIBUTES = (
    "extract_embedding",
    "extract_embedding_from_bytes",
    "async_extract_embedding",
    "async_extract_embedding_from_bytes",
)

# Long enough to clear MIN_EMBEDDING_AUDIO_DURATION so the duration guard is not
# what these tests are measuring.
_SAMPLE_RATE = 16000


def _wav_bytes(seconds: float = 3.0) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * int(_SAMPLE_RATE * seconds))
    return buffer.getvalue()


class SpeakerEmbeddingDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        # The patch replaces attributes on the real upstream module, so restore
        # them rather than leaving a patched module for later tests.
        originals = {name: getattr(upstream, name) for name in PATCHED_ATTRIBUTES}

        def restore() -> None:
            for name, value in originals.items():
                setattr(upstream, name, value)

        self.addCleanup(restore)
        self.audio = _wav_bytes()
        build_registry(patches()).apply(SELF_HOSTED)

    def _select(self, provider: str) -> None:
        os.environ["SPEAKER_EMBEDDING_PROVIDER"] = provider
        self.addCleanup(os.environ.pop, "SPEAKER_EMBEDDING_PROVIDER", None)

    def test_local_provider_never_reaches_the_network(self):
        self._select("sherpa_onnx")
        with mock.patch("fork.speaker_embedding._local_model_identity", return_value=("/models/x.onnx", 1)), mock.patch(
            "fork.speaker_embedding.extract_local_embedding", return_value="local-vector"
        ) as local, mock.patch.object(upstream, "get_stt_client") as client:
            self.assertEqual(upstream.extract_embedding_from_bytes(self.audio), "local-vector")
        local.assert_called_once()
        client.assert_not_called()

    def test_async_local_provider_never_reaches_the_network(self):
        self._select("sherpa_onnx")
        with mock.patch("fork.speaker_embedding._local_model_identity", return_value=("/models/x.onnx", 1)), mock.patch(
            "fork.speaker_embedding.extract_local_embedding", return_value="local-vector"
        ) as local, mock.patch.object(upstream, "get_stt_client") as client:
            result = asyncio.run(upstream.async_extract_embedding_from_bytes(self.audio))
        self.assertEqual(result, "local-vector")
        local.assert_called_once()
        client.assert_not_called()

    def test_disabled_provider_fails_before_building_a_request(self):
        self._select("disabled")
        with mock.patch.object(upstream, "get_stt_client") as client:
            with self.assertRaises(SpeakerEmbeddingUnavailable):
                upstream.extract_embedding_from_bytes(self.audio)
        client.assert_not_called()

    def test_unknown_provider_fails_closed(self):
        self._select("whatever-the-operator-typed")
        with mock.patch.object(upstream, "get_stt_client") as client:
            with self.assertRaises(SpeakerEmbeddingUnavailable):
                upstream.extract_embedding_from_bytes(self.audio)
        client.assert_not_called()

    def test_http_provider_still_runs_upstreams_implementation(self):
        self._select("http")
        os.environ["HOSTED_SPEAKER_EMBEDDING_API_URL"] = "http://embeddings.internal:8080"
        self.addCleanup(os.environ.pop, "HOSTED_SPEAKER_EMBEDDING_API_URL", None)
        sentinel = object()
        with mock.patch("fork.egress_policy.assert_http_endpoint_allowed") as allowed, mock.patch.object(
            upstream, "extract_embedding_from_bytes"
        ):
            # Re-apply so the patch wraps the stand-in, letting the test observe
            # that the fork delegates instead of reimplementing the HTTP path.
            original = mock.Mock(return_value=sentinel)
            setattr(upstream, "extract_embedding_from_bytes", original)
            build_registry(patches()).apply(SELF_HOSTED)
            self.assertIs(upstream.extract_embedding_from_bytes(self.audio), sentinel)
        original.assert_called_once()
        allowed.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
