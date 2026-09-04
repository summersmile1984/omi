#!/usr/bin/env python3
"""Build both local identity targets using installed frozen dependencies."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
from prepare import ROOT, COMPONENT, load_module, prepare


def synthetic_manifest():
    render = load_module('electron_ci_profiles', ROOT / 'scripts/profiles/render.py')
    value = copy.deepcopy(render.load_manifest('omi-upstream', ROOT))
    value['brand'].update(id='synthetic-electron-ci', display_name='Synthetic Desktop CI')
    value['identifiers']['windows_app_id'] = 'test.synthetic.desktop'
    value['deployments'] = {
        target: {
            'local': {
                key: f'http://127.0.0.1:{port}'
                for key in ('api_base', 'auth_base', 'web_app', 'mcp_base', 'share_base', 'objects_base')
            }
        }
        for target, port in [('self_hosted', 39911), ('cloudflare', 39912)]
    }
    return value


def check_matrix():
    dependencies = COMPONENT / 'node_modules'
    if not (dependencies / '.bin/electron-vite').exists():
        raise ValueError('Run pnpm install --frozen-lockfile in desktop/windows first')
    with tempfile.TemporaryDirectory(prefix='fork-electron-ci-') as temp:
        root = Path(temp)
        manifest = root / 'brand.json'
        manifest.write_text(json.dumps(synthetic_manifest()))
        for target in ('self_hosted', 'cloudflare'):
            stage = prepare(manifest, target, root / target)
            (stage / 'node_modules').symlink_to(dependencies.resolve(), target_is_directory=True)
            for command in [
                ['pnpm', 'exec', 'vitest', 'run', '--config', 'fork/vitest-stage.config.ts'],
                ['pnpm', 'build'],
            ]:
                subprocess.run(command, cwd=stage, check=True)


if __name__ == '__main__':
    check_matrix()
