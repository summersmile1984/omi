#!/usr/bin/env python3
"""Run the upstream name-only setting classifier with the fork's additive policy."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def combine(upstream, fork):
    if set(fork) != {'kinds'} or set(fork['kinds']) != {'secret', 'config', 'public_build'}:
        raise ValueError('fork classifications may add names only; exceptions remain upstream-owned')
    existing = {name for names in upstream['kinds'].values() for name in names}
    additions = [name for names in fork['kinds'].values() for name in names]
    if existing.intersection(additions) or len(set(additions)) != len(additions):
        raise ValueError('fork classifications must not replace or duplicate an existing classification')
    return {
        **upstream,
        'kinds': {kind: names + fork['kinds'][kind] for kind, names in upstream['kinds'].items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    policy = combine(
        json.loads((ROOT / 'config/deployment-setting-classification.json').read_text()),
        json.loads((ROOT / 'config/deployment-setting-classification.fork.json').read_text()),
    )
    with tempfile.TemporaryDirectory(prefix='fork-setting-policy-') as directory:
        path = Path(directory) / 'policy.json'
        path.write_text(json.dumps(policy))
        return subprocess.call(
            [
                sys.executable,
                str(ROOT / '.github/scripts/check_deployment_secret_boundary.py'),
                '--base',
                args.base,
                '--policy',
                str(path),
            ],
            cwd=ROOT,
        )


if __name__ == '__main__':
    raise SystemExit(main())
