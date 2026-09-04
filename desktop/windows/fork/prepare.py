#!/usr/bin/env python3
"""Create an isolated, local Electron identity candidate from current upstream.

The reviewed TypeScript owner map is applied only to this fresh copy. No source,
lockfile, generated brand profile or upstream test changes enter the repository.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
COMPONENT = ROOT / 'desktop/windows'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_source_owners(component, owners):
    for path, expected in owners.items():
        if hashlib.sha256((component / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Review upstream identity owner drift: {path}')


def prepare(manifest, target, output):
    if output.is_symlink():
        raise ValueError('Output must not be a symlink')
    output = output.resolve()
    if output.exists() or output.is_relative_to(ROOT):
        raise ValueError('Output must be a new directory outside the repository')
    render = load_module('electron_profile_render', ROOT / 'scripts/profiles/render.py')
    brand = render.load_manifest(None, ROOT, manifest)
    if brand['brand']['id'] == 'omi-upstream':
        raise ValueError('Use an isolated synthetic/private brand, not the upstream identity')
    resolved = render.resolve(target, manifest_path=manifest, stage='local')
    row = resolved['profiles'][f'{target}.local']
    identity = f"{brand['identifiers']['windows_app_id']}.forktest.{target.replace('_', '')}"
    profile = {
        'applicationId': identity,
        'displayName': brand['brand']['display_name'],
        'personaName': brand['brand']['ai_persona_name'],
        'supportEmail': brand['brand']['support_email'],
        'legalEntity': brand['brand']['legal_entity'],
        'links': {
            **{
                key: brand['domains'][key]
                for key in ('docs', 'help', 'feedback', 'privacy', 'terms', 'status', 'community')
            },
            'webApp': row['web_base_url'],
        },
        'target': target,
        'stage': 'local',
        'authBase': row['auth_base_url'],
        'apiBase': row['api_base_url'],
        'shareBase': row['share_base_url'],
        'capabilities': row['capabilities'],
        'updates': 'disabled',
    }
    owners = json.loads((COMPONENT / 'fork/source-owners.json').read_text())
    verify_source_owners(COMPONENT, owners)
    tracked = subprocess.check_output(['git', 'ls-files', '-z', 'desktop/windows'], cwd=ROOT).decode().split('\0')
    output.mkdir(parents=True)
    stage = output / 'desktop'
    for path in tracked:
        if not path:
            continue
        rel = Path(path).relative_to('desktop/windows')
        if rel.parts[0] == 'fork' or rel.name.startswith('.env') or rel.name in ('dev-app-update.yml',):
            continue
        source = ROOT / path
        if source.is_symlink():
            raise ValueError(f'Unexpected source symlink: {path}')
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    shutil.copytree(COMPONENT / 'fork', stage / 'fork', ignore=shutil.ignore_patterns('__pycache__'))
    (stage / 'fork/native/profile.generated.ts').write_text(
        'export const profile = ' + json.dumps(profile, indent=2) + ' as const\n'
    )
    (stage / 'fork/profile.json').write_text(json.dumps(profile, indent=2) + '\n')
    subprocess.run(['node', str(COMPONENT / 'fork/source-stage.mjs'), str(stage)], check=True, cwd=COMPONENT)
    record = {
        'source': str(ROOT),
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        'target': target,
        'stage': 'local',
        'identity': identity,
        'profile': profile,
        'reviewed_source_owners': owners,
        'fork_source_hashes': {
            str(p.relative_to(COMPONENT / 'fork')): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((COMPONENT / 'fork').rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts
        },
        'release_qualified': False,
    }
    (output / 'build-manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(stage)
    return stage


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--target', choices=['self_hosted', 'cloudflare'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.manifest.resolve(), args.target, args.output)
