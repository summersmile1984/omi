"""One public embedding identity, shared by profile generation and runtime admission.

This module is dependency-free: profile builds import the same validator as
serving and vector migration, instead of copying dimension/model constants.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class EmbeddingContract:
    provider: str
    model: str
    manifest_digest: str
    artifact_digest: str
    dimension: int
    context_length: int

    def as_dict(self):
        from dataclasses import asdict

        return asdict(self)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(EmbeddingContract.__dataclass_fields__):
        raise ValueError('embedding contract requires exactly provider/model/digests/dimension/context_length')
    _validate_ollama_identity(value)
    for field, ceiling in (('dimension', 65536), ('context_length', 1048576)):
        if type(value[field]) is not int or not 1 <= value[field] <= ceiling:
            raise ValueError(f'embedding {field} must be a bounded positive integer')
    return EmbeddingContract(**value)


def validate_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('model identity requires full SHA-256 digests')
    return value


def _validate_ollama_identity(value):
    if value['provider'] != 'ollama':
        raise ValueError('self-host model requires the explicit Ollama provider')
    if not isinstance(value['model'], str) or not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9._/-]{0,110}:[A-Za-z0-9][A-Za-z0-9._-]{0,30}', value['model']
    ):
        raise ValueError('model must be an explicit safe model identifier')
    if any(part in ('', '.', '..') for part in value['model'].split(':')[0].split('/')):
        raise ValueError('model contains unsafe path segments')
    for field in ('manifest_digest', 'artifact_digest'):
        validate_digest(value[field])


@dataclass(frozen=True)
class LLMContract:
    provider: str
    model: str
    manifest_digest: str
    artifact_digest: str
    context_length: int
    context_window: int
    max_output_tokens: int
    runtime_version: str
    kv_cache_type: str
    cpu_threads: int
    parallel_requests: int
    request_timeout_seconds: int

    def as_dict(self):
        from dataclasses import asdict

        return asdict(self)


def validate_llm(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(LLMContract.__dataclass_fields__):
        raise ValueError('LLM requires explicit model/digests/context/output/runtime identity')
    _validate_ollama_identity(value)
    if value['kv_cache_type'] != 'q8_0':
        raise ValueError('LLM serving KV precision has not been admitted')
    if value['runtime_version'] != '0.33.3':
        raise ValueError('LLM runtime has not been admitted')
    if (value['cpu_threads'], value['parallel_requests'], value['request_timeout_seconds']) != (4, 1, 300) or any(
        type(value[name]) is not int for name in ('cpu_threads', 'parallel_requests', 'request_timeout_seconds')
    ):
        raise ValueError('LLM CPU, concurrency and deadline budget has not been admitted')
    for field in ('context_length', 'context_window', 'max_output_tokens'):
        if type(value[field]) is not int or not 1 <= value[field] <= 131072:
            raise ValueError('LLM context and output limits must be bounded positive integers')
    if not 4096 <= value['context_window'] <= value['context_length'] or not (
        128 <= value['max_output_tokens'] <= value['context_window'] // 4
    ):
        raise ValueError('LLM serving context and output exceed the model contract')
    return LLMContract(**value)


@dataclass(frozen=True)
class SpeechContract:
    runtime_version: str
    stt_model: str
    tts_model: str
    bundle_digest: str
    stt_archive_digest: str
    tts_archive_digest: str
    vad_digest: str

    def as_dict(self):
        from dataclasses import asdict

        return asdict(self)


def validate_speech(value):
    """The optional bundle enables exactly the two reviewed CPU providers."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(SpeechContract.__dataclass_fields__):
        raise ValueError('speech requires an explicit runtime, model pair and four artifact digests')
    if (
        value['runtime_version'] != '1.13.4'
        or value['stt_model'] != 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
        or value['tts_model'] != 'kokoro-multi-lang-v1_0'
    ):
        raise ValueError('speech model/runtime combination has not been admitted')
    for name in ('bundle_digest', 'stt_archive_digest', 'tts_archive_digest', 'vad_digest'):
        if not isinstance(value[name], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[name]):
            raise ValueError('speech artifact identity requires full SHA-256 digests')
    return SpeechContract(**value)
