#!/usr/bin/env python3
"""Project the existing brand/profile authority into public Web build inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/profiles'))
from render import ProfileError, load_manifest, resolve  # noqa: E402

sys.path.insert(0, str(ROOT / 'scripts/brand'))
from generators.firmware import public_policy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=['self_hosted', 'cloudflare'])
    parser.add_argument('--stage', required=True, choices=['local', 'beta', 'production'])
    parser.add_argument('--brand')
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        resolved = resolve(args.target, args.brand, args.manifest, args.stage)
        manifest = load_manifest(args.brand, ROOT, args.manifest)
    except ProfileError as error:
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                'brand_id': manifest['brand']['id'],
                'product_name': manifest['brand']['display_name'],
                'tagline': manifest['brand'].get('tagline', ''),
                'support_email': manifest['brand']['support_email'],
                'links': {
                    **{
                        key: manifest['domains'][key]
                        for key in ('docs', 'help', 'feedback', 'community', 'privacy', 'terms')
                    },
                    'website': resolved['profiles'][f'{args.target}.{args.stage}']['web_base_url'],
                    'download': resolved['profiles'][f'{args.target}.{args.stage}']['api_base_url'].rstrip('/')
                    + '/v2/desktop/download/latest',
                },
                'asset_input': {
                    'root': str(
                        (args.manifest.parent if args.manifest else ROOT / 'brand' / manifest['brand']['id']).resolve()
                    ),
                    'refs': manifest['assets'],
                },
                'brand_runtime': {
                    'brand_id': manifest['brand']['id'],
                    'display_name': manifest['brand']['display_name'],
                    'ai_persona_name': manifest['brand']['ai_persona_name'],
                },
                'firmware_policy': public_policy(manifest),
                'profile': resolved['profiles'][f'{args.target}.{args.stage}'],
            }
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
