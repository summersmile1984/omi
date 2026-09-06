#!/usr/bin/env python3
"""Stage the upstream frame wire types and pure admission/lifecycle policy."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from screen_frame_sources import selected_nodes, source


def generate(output: Path) -> None:
    policy = source('backend/utils/retrieval/frame_request_policy.py')
    if policy.count('from models.frame_request import ') != 1:
        raise ValueError('frame request model import changed')
    outputs = {
        'frame_request_contract.py': source('backend/models/frame_request.py'),
        'frame_request_policy.py': policy.replace(
            'from models.frame_request import ', 'from frame_request_contract import '
        ),
        'jit_policy.py': (
            'from __future__ import annotations\nfrom dataclasses import dataclass\nfrom enum import Enum\n\n'
            + selected_nodes(
                'backend/utils/jit_rollout.py',
                {
                    'JIT_ADMISSION_ALLOWLIST',
                    'TriState',
                    'JITDecisionReason',
                    'JITErrorClass',
                    'JITFlagEvaluation',
                    'JITRolloutDecision',
                    'is_jit_admission_allowlisted',
                    '_effective_decision',
                },
            )
        ),
    }
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('frame request projection collides with a staged source owner')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
