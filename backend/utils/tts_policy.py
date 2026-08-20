"""Deployment-selected TTS availability policy shared by mobile and desktop routes."""

from __future__ import annotations

import os

TTS_DISABLED_DETAIL = {
    'code': 'model_capability_unavailable',
    'capability': 'tts',
    'reason': 'disabled',
    'retryable': False,
}


def tts_explicitly_disabled() -> bool:
    return os.getenv('TTS_PROVIDER', '').strip().lower() == 'disabled'
