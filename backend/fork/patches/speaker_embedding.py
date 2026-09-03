"""Speaker embedding: honour the fork's provider selection.

Upstream's `utils.stt.speaker_embedding` has exactly one implementation -- POST
the audio to `{HOSTED_SPEAKER_EMBEDDING_API_URL}/v2/embedding`. The fork adds two more
choices (`sherpa_onnx`, running a mounted ONNX model in-process, and `disabled`)
via `SPEAKER_EMBEDDING_PROVIDER`, and `fork.speaker_embedding` implements them.

Without this patch the selection is read but never acted on: a self-hosted
operator who mounts a local model passes configuration validation and then has
every second of audio posted off the machine anyway. That is the opposite of
what selecting a local provider means, and it is silent -- no error, no log, the
transcript still comes back. So the four extraction entry points are wrapped to
dispatch on the provider, with `disabled` and unknown values failing closed
before any request is built.

The `http` provider keeps upstream's implementation; it is called through
`original`, so this patch adds no second copy of the network path. The two
synchronous entry points additionally assert the egress policy, because they
build their own httpx call instead of going through the shared async client.
"""

from __future__ import annotations

from typing import Any, Callable, List

from ..registry import Patch, PatchError

UPSTREAM_MODULE = "utils.stt.speaker_embedding"


def _runs_this_backend(profile: dict) -> bool:
    # Only the self-hosted target runs this Python backend; the Cloudflare
    # target serves its data plane from Workers, so a patch here would never
    # execute and would misdescribe where the seam lives.
    return profile.get("target") == "self_hosted"


def _upstream(name: str) -> Any:
    """Resolve an upstream private helper, failing at boot rather than at call.

    These are private names, so an upstream rename would otherwise surface as an
    AttributeError on the first speaker-embedding request in production.
    """
    import importlib

    module = importlib.import_module(UPSTREAM_MODULE)
    if not hasattr(module, name):
        raise PatchError(
            f"patch 'speaker-embedding': {UPSTREAM_MODULE} has no '{name}'. "
            f"Upstream renamed a helper this dispatch reuses; update the patch, "
            f"do not edit {UPSTREAM_MODULE}."
        )
    return getattr(module, name)


def _local_path_extractor(original: Callable[..., Any]) -> Callable[..., Any]:
    read_file = _upstream("_read_file")
    get_api_url = _upstream("_get_api_url")

    def extract_embedding(audio_path: str) -> Any:
        from ..egress_policy import assert_http_endpoint_allowed
        from ..speaker_embedding import extract_local_embedding, validate_speaker_embedding_configuration

        if validate_speaker_embedding_configuration() == "sherpa_onnx":
            return extract_local_embedding(read_file(audio_path))
        assert_http_endpoint_allowed(get_api_url())
        return original(audio_path)

    return extract_embedding


def _local_bytes_extractor(original: Callable[..., Any]) -> Callable[..., Any]:
    get_api_url = _upstream("_get_api_url")
    wav_duration = _upstream("_get_wav_duration")
    minimum = _upstream("MIN_EMBEDDING_AUDIO_DURATION")

    def extract_embedding_from_bytes(audio_data: bytes, filename: str = "audio.wav") -> Any:
        from ..egress_policy import assert_http_endpoint_allowed
        from ..speaker_embedding import extract_local_embedding, validate_speaker_embedding_configuration

        if validate_speaker_embedding_configuration() == "sherpa_onnx":
            # Upstream applies this guard inside the branch we are replacing, so
            # the local path has to apply it itself.
            duration = wav_duration(audio_data)
            if duration < minimum:
                raise ValueError(f"Audio too short for speaker embedding: {duration:.3f}s < {minimum}s")
            return extract_local_embedding(audio_data)
        assert_http_endpoint_allowed(get_api_url())
        return original(audio_data, filename)

    return extract_embedding_from_bytes


def _async_local_path_extractor(original: Callable[..., Any]) -> Callable[..., Any]:
    async def async_extract_embedding(audio_path: str) -> Any:
        from utils.executors import run_blocking, sync_executor

        from ..speaker_embedding import validate_speaker_embedding_configuration

        if validate_speaker_embedding_configuration() == "sherpa_onnx":
            # Re-enter the patched synchronous entry point so file reading and
            # extraction stay on the blocking executor, as upstream does.
            return await run_blocking(sync_executor, _upstream("extract_embedding"), audio_path)
        return await original(audio_path)

    return async_extract_embedding


def _async_local_bytes_extractor(original: Callable[..., Any]) -> Callable[..., Any]:
    wav_duration = _upstream("_get_wav_duration")
    minimum = _upstream("MIN_EMBEDDING_AUDIO_DURATION")

    async def async_extract_embedding_from_bytes(audio_data: bytes, filename: str = "audio.wav") -> Any:
        from utils.executors import run_blocking, sync_executor

        from ..speaker_embedding import extract_local_embedding, validate_speaker_embedding_configuration

        if validate_speaker_embedding_configuration() == "sherpa_onnx":
            duration = wav_duration(audio_data)
            if duration < minimum:
                raise ValueError(f"Audio too short for speaker embedding: {duration:.3f}s < {minimum}s")
            return await run_blocking(sync_executor, extract_local_embedding, audio_data)
        return await original(audio_data, filename)

    return async_extract_embedding_from_bytes


def patches() -> List[Patch]:
    reason = "SPEAKER_EMBEDDING_PROVIDER selects a local or disabled boundary that upstream does not implement"
    return [
        Patch(
            name="speaker-embedding.extract-path",
            module=UPSTREAM_MODULE,
            attribute="extract_embedding",
            build=_local_path_extractor,
            applies_to=_runs_this_backend,
            reason=reason,
        ),
        Patch(
            name="speaker-embedding.extract-bytes",
            module=UPSTREAM_MODULE,
            attribute="extract_embedding_from_bytes",
            build=_local_bytes_extractor,
            applies_to=_runs_this_backend,
            reason=reason,
        ),
        Patch(
            name="speaker-embedding.async-extract-path",
            module=UPSTREAM_MODULE,
            attribute="async_extract_embedding",
            build=_async_local_path_extractor,
            applies_to=_runs_this_backend,
            reason=reason,
        ),
        Patch(
            name="speaker-embedding.async-extract-bytes",
            module=UPSTREAM_MODULE,
            attribute="async_extract_embedding_from_bytes",
            build=_async_local_bytes_extractor,
            applies_to=_runs_this_backend,
            reason=reason,
        ),
    ]
