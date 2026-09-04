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
    if value['provider'] != 'ollama':
        raise ValueError('self-host embedding currently requires the explicit Ollama provider')
    if not isinstance(value['model'], str) or not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9._/-]{0,110}:[A-Za-z0-9][A-Za-z0-9._-]{0,30}', value['model']
    ):
        raise ValueError('embedding model must be an explicit safe model identifier')
    if any(part in ('', '.', '..') for part in value['model'].split(':')[0].split('/')):
        raise ValueError('embedding model contains unsafe path segments')
    for field in ('manifest_digest', 'artifact_digest'):
        if not isinstance(value[field], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[field]):
            raise ValueError('embedding model identity requires full SHA-256 digests')
    for field, ceiling in (('dimension', 65536), ('context_length', 1048576)):
        if type(value[field]) is not int or not 1 <= value[field] <= ceiling:
            raise ValueError(f'embedding {field} must be a bounded positive integer')
    return EmbeddingContract(**value)


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
