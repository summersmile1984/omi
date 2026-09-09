"""Explicit hosted AI selection frozen into Server profiles; no credentials."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class MiMo:
    provider: str = 'mimo'
    model: str = 'mimo-v2.5'
    asr_model: str = 'mimo-v2.5-asr'
    tts_model: str = 'mimo-v2.5-tts'
    base_url: str = 'https://token-plan-cn.xiaomimimo.com/v1'
    request_timeout_seconds: int = 120
    max_output_tokens: int = 8192


def select(row):
    value = row.get('operator_ai')
    if value is None:
        return None
    if (
        row.get('target') != 'self_hosted'
        or row.get('stage') not in {'local', 'beta', 'production'}
        or value != asdict(MiMo())
        or row.get('llm') is not None
        or row.get('speech') is not None
    ):
        raise ValueError('operator AI requires an explicit Server MiMo CN profile')
    return MiMo()


def configure(row, name):
    if name != 'mimo-cn':
        raise ValueError('unknown operator AI selection')
    result = dict(row)
    result.pop('llm', None)
    result.pop('speech', None)
    result['operator_ai'] = asdict(MiMo())
    select(result)
    result['capabilities'] = {
        **result['capabilities'],
        'llm_provider': 'mimo',
        'stt_providers': ['mimo'],
        'tts_provider': 'mimo',
    }
    return result


def current():
    from .profile import current as profile

    selected = select(profile())
    if selected is None:
        raise ValueError('MiMo is not selected by this deployment')
    return selected


def credentials():
    selected = current()
    key = os.environ.get('MIMO_API_KEY', '').strip()
    filename = os.environ.get('MIMO_SECRET_FILE', '')
    if filename:
        try:
            value = json.loads(Path(filename).read_text())
            if value.get('MIMO_BASE_URL') != selected.base_url:
                raise ValueError('MiMo secret endpoint conflicts with the selected profile')
            stored = value.get('MIMO_API_KEY', '').strip()
            if key and key != stored:
                raise ValueError('MiMo credential sources conflict')
            key = stored
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError('MiMo secret file is unavailable') from error
    if not key or any(character.isspace() for character in key):
        raise ValueError('MiMo API credential is required')
    return key


def allows(url):
    # This is an exact API endpoint grant, not a vendor-wide host allowlist.
    from .profile import current as profile, ProfileError

    try:
        selected = select(profile())
    except (ValueError, ProfileError):
        return False
    return selected is not None and url == selected.base_url + '/chat/completions'
