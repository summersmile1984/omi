#!/usr/bin/env python3
"""Select Compose model services from the same public profile frozen in images."""

import argparse
from copy import deepcopy
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = Path(__file__).with_name('compose.production.yml')


def profile_for(values, *, root=ROOT):
    sys.path.insert(0, str(ROOT / 'scripts/profiles'))
    from render import resolve

    root = root.resolve()
    manifest = (root / values['SELF_HOST_BRAND_MANIFEST']).resolve()
    if not manifest.is_relative_to(root):
        raise ValueError('deployment manifest must remain inside the source checkout')
    stage = values.get('SELF_HOST_STAGE', 'production')
    return resolve('self_hosted', None, manifest, stage)['profiles']['self_hosted.' + stage]


def image_keys(profile):
    """The frozen application images required by the selected Compose graph."""
    from fork.operator_ai import select

    keys = {'backend': 'BACKEND_IMAGE', 'auth': 'AUTH_SERVER_IMAGE', 'web': 'WEB_IMAGE'}
    if not select(profile):
        keys['llm'] = 'LLM_IMAGE'
    return keys


def specialize(config, profile):
    config = deepcopy(config)
    if 'operator_ai' not in profile:
        return config
    sys.path.insert(0, str(ROOT / 'backend'))
    from fork.operator_ai import CLOUDFLARE_GATEWAY, CLOUDFLARE_TOKEN_ENV, CREDENTIAL_ENV, select

    selected = select(profile)
    hosted = selected.provider != 'mimo'
    services = config['services']
    removed = ['llm', 'llm-artifact-check']
    if hosted:
        # A hosted operator AI owns embeddings too, so the server runs no AI compute.
        removed += ['embedding', 'embedding-artifact-check']
    for name in removed:
        del services[name]
    credential_envs = [CREDENTIAL_ENV[selected.provider]]
    if selected.provider == CLOUDFLARE_GATEWAY:
        credential_envs.append(CLOUDFLARE_TOKEN_ENV)
    removed_env_prefixes = ('SPEECH_MODEL_STORE=', 'LLM_ENDPOINT=')
    if hosted:
        removed_env_prefixes += ('EMBEDDING_ENDPOINT=',)
    for service in services.values():
        depends = service.get('depends_on', {})
        for name in removed:
            depends.pop(name, None)
        environment = service.get('environment') or []
        service['environment'] = [value for value in environment if not value.startswith(removed_env_prefixes)]
    credential_lines = [f'{name}=${{{name}:?{name} is required}}' for name in credential_envs]
    for name in ('backend', 'memory-maintenance-worker'):
        service = services.get(name)
        if service is not None:
            service['environment'] = list(service.get('environment') or []) + credential_lines
    backend = services['backend']
    backend['volumes'] = [value for value in backend['volumes'] if ':/models/speech:' not in value]
    return config


def selected_config(values):
    return specialize(yaml.safe_load(COMPOSE.read_text()), profile_for(values))


def main():
    from dotenv import dotenv_values

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--input', type=Path, default=COMPOSE)
    parser.add_argument('--providers', action='store_true')
    args = parser.parse_args()
    if args.input.resolve() != COMPOSE.resolve():
        if args.providers:
            raise ValueError('provider selection requires the canonical Compose source')
        print(args.input.read_text(), end='')
        return
    config = selected_config(dotenv_values(args.env_file, interpolate=False))
    if args.providers:
        print(' '.join(name for name in ('embedding', 'llm') if name in config['services']))
    else:
        print(yaml.safe_dump(config, sort_keys=False), end='')


if __name__ == '__main__':
    main()
