#!/usr/bin/env python3
"""Freeze both deployment deliverables without contacting a deployment target."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/self-host'))
from model_services import profile_for, image_keys

PYTHON_BASE = 'python:3.11.10-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e'


def execute(arguments: list[str], *, root: Path, capture: bool = False) -> str:
    result = subprocess.run(arguments, cwd=root, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout or ''


def build_plan(root: Path, output: Path, inventory: Path, brand: str, stage: str, commit: str, tree: str) -> dict:
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', brand) or stage not in ('beta', 'production'):
        raise ValueError('select a repository brand and beta or production stage')
    if not (root / 'brand' / brand / 'manifest.yaml').is_file():
        raise ValueError('brand manifest is missing')
    if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (commit, tree)):
        raise ValueError('release requires exact Git commit and tree identities')
    if output == root or root in output.parents:
        raise ValueError('release output must be outside the source checkout')
    candidate = output / 'cloudflare'
    profile = profile_for(
        {'SELF_HOST_BRAND_MANIFEST': f'brand/{brand}/manifest.yaml', 'SELF_HOST_STAGE': stage}, root=root
    )
    images = {role: f'memweft-release/{brand}-{role}:{commit}' for role in ('runtime', *image_keys(profile))}
    common = ['docker', 'build', '--platform', 'linux/amd64']
    labels = ['--build-arg', f'OMI_SOURCE_GIT_COMMIT={commit}', '--build-arg', f'OMI_SOURCE_GIT_TREE={tree}']
    commands = [
        [
            'node',
            'deploy/cloudflare/scripts/release.mjs',
            'prepare',
            '--stage',
            stage,
            '--brand',
            brand,
            '--inventory',
            str(inventory),
            '--output',
            str(candidate),
        ],
        ['node', 'deploy/cloudflare/scripts/release.mjs', 'check', '--candidate', str(candidate)],
        [
            *common,
            '-f',
            'backend/Dockerfile',
            '--build-arg',
            f'PYTHON_BASE_IMAGE={PYTHON_BASE}',
            '-t',
            images['runtime'],
            '.',
        ],
        [
            *common,
            '-f',
            'deploy/self-host/Dockerfile',
            *labels,
            '--build-arg',
            f'UPSTREAM_BACKEND_IMAGE={images["runtime"]}',
            '--build-arg',
            f'DEPLOYMENT_STAGE={stage}',
            '--build-arg',
            f'BRAND_MANIFEST=brand/{brand}/manifest.yaml',
            '-t',
            images['backend'],
            '.',
        ],
        [*common, '-f', 'auth-server/Dockerfile', *labels, '-t', images['auth'], '.'],
        *(
            [
                [
                    *common,
                    '-f',
                    'deploy/self-host/Dockerfile.llm',
                    *labels,
                    '--build-arg',
                    f'BACKEND_IMAGE={images["backend"]}',
                    '-t',
                    images['llm'],
                    '.',
                ]
            ]
            if 'llm' in images
            else []
        ),
        [
            *common,
            '-f',
            str(root / 'deploy/web/Dockerfile'),
            '--label',
            f'com.omi.source.git-commit={commit}',
            '--label',
            f'com.omi.source.git-tree={tree}',
            '-t',
            images['web'],
            str(candidate / 'web/self_hosted/artifact'),
        ],
        [
            'docker',
            'image',
            'save',
            '--output',
            str(output / 'server-images.tar'),
            *[image for role, image in images.items() if role != 'runtime'],
        ],
        ['git', 'archive', '--format=tar.gz', f'--output={output / "source.tar.gz"}', commit],
        ['tar', '-czf', str(output / 'cloudflare.tar.gz'), '-C', str(output), 'cloudflare'],
    ]
    return {
        'schema_version': 1,
        'commit': commit,
        'tree': tree,
        'brand': brand,
        'stage': stage,
        'platform': 'linux/amd64',
        'images': images,
        'commands': commands,
        'release_ready': False,
    }


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def prepare(plan: dict, root: Path, output: Path, run=execute) -> dict:
    def unchanged() -> None:
        if run(['git', 'rev-parse', 'HEAD'], root=root, capture=True).strip() != plan['commit']:
            raise ValueError('source commit changed during preparation')
        if run(['git', 'status', '--porcelain'], root=root, capture=True).strip():
            raise ValueError('commit source changes before preparing delivery artifacts')

    unchanged()
    output.mkdir(parents=True, exist_ok=False)
    for command in plan['commands']:
        run(command, root=root)
    identities = {}
    for role, tag in plan['images'].items():
        if role == 'runtime':
            continue
        image = json.loads(run(['docker', 'image', 'inspect', tag], root=root, capture=True))[0]
        if (image['Os'], image['Architecture']) != ('linux', 'amd64'):
            raise ValueError(f'{role} image has the wrong runtime platform')
        labels = image.get('Config', {}).get('Labels') or {}
        if (
            labels.get('com.omi.source.git-commit') != plan['commit']
            or labels.get('com.omi.source.git-tree') != plan['tree']
        ):
            raise ValueError(f'{role} image source identity differs from the candidate')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image['Id']):
            raise ValueError(f'{role} image has no immutable image identity')
        identities[role] = {'tag': tag, 'image_id': image['Id']}
    unchanged()
    # The normal candidate verifier still owns byte/source validation.
    run(
        ['node', 'deploy/cloudflare/scripts/release.mjs', 'check', '--candidate', str(output / 'cloudflare')], root=root
    )
    candidate = json.loads((output / 'cloudflare/candidate.json').read_text())
    if candidate['source']['commit'] != plan['commit'] or candidate['source']['tree'] != plan['tree']:
        raise ValueError('Cloudflare candidate source differs from the Server images')
    receipt = {key: value for key, value in plan.items() if key not in ('commands', 'images')}
    receipt.update(
        {
            'candidate_digest': candidate['candidate_digest'],
            'images': identities,
            'files': {
                name: sha256(output / name) for name in ('cloudflare.tar.gz', 'server-images.tar', 'source.tar.gz')
            },
            'scope': 'frozen-workers-and-linux-images; deployment-and-image-boot-qualification-pending',
            'pending': [*candidate['pending'], 'Server accepted-image boot and public deployment acceptance'],
        }
    )
    (output / 'delivery.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brand', required=True)
    parser.add_argument('--stage', required=True, choices=('beta', 'production'))
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--plan', action='store_true', help='print commands without building or deploying')
    parser.add_argument(
        '--ci-run-id', type=int, help='reuse a successful complete Fork Checks run at this exact commit'
    )
    args = parser.parse_args()
    output = args.output.resolve()
    plan = build_plan(
        ROOT,
        output,
        args.inventory.resolve(),
        args.brand,
        args.stage,
        execute(['git', 'rev-parse', 'HEAD'], root=ROOT, capture=True).strip(),
        execute(['git', 'rev-parse', 'HEAD^{tree}'], root=ROOT, capture=True).strip(),
    )
    ci = None
    if args.ci_run_id:
        from release_ci import verify_ci

        ci = verify_ci(args.ci_run_id, plan['commit'])
    result = plan if args.plan else prepare(plan, ROOT, output)
    if ci and not args.plan:
        result.update(ci)
        (output / 'delivery.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
