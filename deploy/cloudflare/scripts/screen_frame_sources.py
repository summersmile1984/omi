#!/usr/bin/env python3
"""Compile the upstream screenshot wire, image and policy sources into a Worker stage."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = {
    'screen_frames_contract.py': 'backend/models/screen_frame.py',
    'screen_frames_canonical.py': 'backend/utils/screen_frames/canonicalize.py',
    'screen_frames_palette.py': 'backend/utils/screen_frames/palette.py',
    'screen_frames_policy.py': 'backend/utils/screen_frames/policy.py',
}


def source(relative: str) -> str:
    path = ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError('screenshot source must be an ordinary repository file')
    return path.read_text()


def selected_nodes(relative: str, names: set[str]) -> str:
    original = source(relative)
    result = []
    found = set()
    for node in ast.parse(original).body:
        owned = set()
        if isinstance(node, ast.Assign):
            owned = {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AnnAssign)):
            owned = (
                {node.name} if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else {getattr(node.target, 'id', '')}
            )
        if owned & names:
            decorators = getattr(node, 'decorator_list', [])
            start = min([node.lineno, *(item.lineno for item in decorators)])
            result.append('\n'.join(original.splitlines()[start - 1 : node.end_lineno]))
            found |= owned & names
    if found != names:
        raise ValueError('upstream screenshot policy source changed; review the Worker projection')
    return '\n\n'.join(result) + '\n'


def generate(output: Path) -> None:
    outputs = {}
    for name, relative in SOURCES.items():
        content = source(relative)
        if name in ('screen_frames_palette.py', 'screen_frames_policy.py'):
            # One import points the unchanged domain behavior at the same staged wire types.
            if content.count('from models.screen_frame import ') != 1:
                raise ValueError('upstream screenshot model import changed')
            content = content.replace('from models.screen_frame import ', 'from screen_frames_contract import ')
        outputs[name] = content
    outputs['screen_frames_prompt.py'] = (
        'from screen_frames_policy import REJECT_IDENTIFIABLE_PERSONS\n\n'
        + selected_nodes(
            'backend/utils/screen_frames/judge.py',
            {'_IDENTIFIABLE_PERSON_RULE', '_PEOPLE_GUIDANCE', '_PRIVACY_PROMPT'},
        )
    )
    outputs['screen_frames_selection.py'] = (
        'from __future__ import annotations\nfrom typing import Any, Dict, List, Optional\n\n'
        + selected_nodes(
            'backend/utils/screen_frames/enforcement.py',
            {'BANNER_SUITABILITY_THRESHOLD', 'STRIP_MAX', '_apply_cap_and_roles'},
        )
    )
    outputs['screen_frames_transport.py'] = (
        'import base64, binascii, hashlib\nfrom screen_frames_contract import ScreenFrameCandidateIn\n\n'
        + selected_nodes(
            'backend/utils/screen_frames/pipeline.py',
            {'ScreenFrameDigestMismatch', 'decode_and_verify_transport_digest'},
        )
    )
    outputs['screen_frames_admission.py'] = (
        'from datetime import datetime, timedelta, timezone\nimport hashlib, json\n'
        'from typing import Any, Dict, List\nfrom fastapi import HTTPException\n'
        'from screen_frames_contract import ScreenFrameCandidateIn, ScreenFrameAdjudicationRequest\n\n'
        + selected_nodes(
            'backend/routers/screen_frames.py',
            {
                'MAX_CANDIDATE_DECODED_BYTES',
                'CAPTURE_WINDOW_SLACK_SECONDS',
                'IDEMPOTENCY_TTL_SECONDS',
                '_ensure_aware',
                '_validate_capture_window',
                '_request_fingerprint',
            },
        )
    )
    # Read/validate every input before publishing any source. The stage owner is
    # temporary; it must never replace an existing domain module or follow a link.
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('screenshot projection collides with a staged source owner')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
