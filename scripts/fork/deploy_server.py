#!/usr/bin/env python3
"""Deploy accepted Docker image IDs into a persistent, separately owned host root."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import socket
import sys
import time
import uuid
from urllib.error import URLError
from urllib.request import urlopen

from release_ci import sha256_file

ROOT = Path(__file__).resolve().parents[2]
READINESS = json.loads((ROOT / 'contracts/deployment/readiness.json').read_text())['self_hosted']
sys.path.insert(0, str(ROOT / 'deploy/self-host'))
from model_services import profile_for, image_keys


def accepted_image_keys(receipt):
    selected = image_keys(
        profile_for(
            {'SELF_HOST_BRAND_MANIFEST': f'brand/{receipt["brand"]}/manifest.yaml', 'SELF_HOST_STAGE': receipt['stage']}
        )
    )
    if set(receipt['images']) != set(selected):
        raise ValueError('delivery image roles differ from the frozen deployment profile')
    return selected


def run(command, *, env=None, capture=False, cwd=ROOT):
    result = subprocess.run(
        command, cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout or ''


def read_environment(path):
    from dotenv import dotenv_values

    values = dotenv_values(path, interpolate=False)
    if any(value is None for value in values.values()):
        raise ValueError('runtime environment contains a key without a value')
    return values


def verify_images(receipt, environment, execute=run):
    for role, key in accepted_image_keys(receipt).items():
        accepted = receipt['images'][role]['image_id']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', accepted) or environment.get(key) != accepted:
            raise ValueError(f'{role} deployment must use its exact accepted image ID')
        image = json.loads(execute(['docker', 'image', 'inspect', accepted], capture=True))[0]
        labels = image.get('Config', {}).get('Labels') or {}
        if (
            image['Id'] != accepted
            or image['Os'] != 'linux'
            or image['Architecture'] != receipt['platform'].split('/')[1]
            or labels.get('com.omi.source.git-commit') != receipt['commit']
            or labels.get('com.omi.source.git-tree') != receipt['tree']
        ):
            raise ValueError(f'{role} installed image source or architecture differs from the delivery')


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(data, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def redact_reason(reason, values):
    """Strip credential values before a failure reason is retained or printed.

    The journal is durable and is copied into the run's failure evidence, and the
    reason reaches the job log; either would otherwise be a place a runtime
    secret could surface. Values shorter than 8 characters are left alone: they
    are not credentials and replacing them would mangle ordinary identifiers.
    """
    text = reason if isinstance(reason, str) else repr(reason)
    secrets = sorted(
        (value for value in values.values() if isinstance(value, str) and len(value) >= 8),
        key=len,
        reverse=True,
    )
    for value in secrets:
        text = text.replace(value, '***')
    return text if len(text) <= 2000 else text[:2000] + '…'


def failure_record(stage, error, values):
    return {
        'target': 'self_hosted',
        'stage': stage,
        'at': int(time.time()),
        'error_name': type(error).__name__,
        'reason': redact_reason(str(error), values),
    }


def write_environment(path, values):
    with open(path, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        # Literal dotenv strings; neither a shell nor interpolation evaluates these.
        output.write(''.join(f"{key}='{value.replace(chr(39), chr(92) + chr(39))}'\n" for key, value in values.items()))


def wait_ready(metadata, *, open_url=urlopen, pause=time.sleep, now=time.monotonic, timeout=120):
    deadline = now() + timeout
    pending = {
        metadata[check['origin'] + '_origin'].rstrip('/') + check['path']: check['ready_json']
        for check in READINESS.values()
        if check['origin'] + '_origin' in metadata
    }
    while pending and now() < deadline:
        for url in list(pending):
            if now() >= deadline:
                break
            try:
                with open_url(url, timeout=min(5, max(0.1, deadline - now()))) as response:
                    if response.getcode() == 200 and response.geturl() == url:
                        if not pending[url] or json.load(response).get('status') == 'ready':
                            del pending[url]
            except (URLError, TimeoutError, OSError, ValueError, AttributeError):
                pass
        if pending:
            pause(min(2, max(0, deadline - now())))
    if pending:
        raise RuntimeError('deployment origins did not become ready before the acceptance deadline')


def boot_test(release, values, env):
    """Exercise the accepted images with disposable volumes before live mutation."""
    directory = release / 'boot-test'
    directory.mkdir(mode=0o700)
    ports = {}
    reservations = []
    try:
        for key in ('BACKEND_PORT', 'AUTH_SERVER_PORT', 'MINIO_API_PORT', 'MINIO_CONSOLE_PORT'):
            connection = socket.socket()
            connection.bind(('127.0.0.1', 0))
            reservations.append(connection)
            ports[key] = str(connection.getsockname()[1])
    finally:
        for connection in reservations:
            connection.close()
    auth_origin = 'http://127.0.0.1:' + ports['AUTH_SERVER_PORT']
    testing = {**values, **ports, 'SELF_HOST_BIND_ADDRESS': '127.0.0.1'}
    runtime = directory / 'runtime.env'
    write_environment(runtime, testing)
    project = 'eddy-boot-' + uuid.uuid4().hex[:12]
    source = release / 'source'
    test_env = {**env, 'SELF_HOST_ENV': str(runtime), 'SELF_HOST_PROJECT': project}
    metadata = {
        'target': 'self_hosted',
        'brand_id': 'eddy',
        'trace_dir': str(directory),
        'api_origin': 'http://127.0.0.1:' + ports['BACKEND_PORT'],
        'api_public_origin': values['PUBLIC_BACKEND_URL'],
        'auth_origin': auth_origin,
        'auth_public_origin': values['PUBLIC_AUTH_URL'],
    }
    save(directory / 'metadata.json', metadata)
    try:
        run(['bash', str(source / 'deploy/self-host/operations.sh'), 'deploy-images'], env=test_env)
        wait_ready(metadata)
        run(
            [
                sys.executable,
                str(source / 'contracts/deployment/core.py'),
                '--metadata',
                str(directory / 'metadata.json'),
            ],
            env=test_env,
        )
    finally:
        # This invocation creates the unique project and exclusively owns its
        # volumes. Persistent eddy-server-* projects are never selected here.
        run(
            [
                'bash',
                str(source / 'deploy/self-host/compose-clean-env.sh'),
                str(runtime),
                str(source / 'deploy/self-host/compose.production.yml'),
                '--project-name',
                project,
                'down',
                '--volumes',
                '--remove-orphans',
            ],
            env=env,
        )


def deploy(delivery, destination, docker_context, *, qualification=False):
    if not destination.is_absolute() or '/_work/' in str(destination) or destination == Path('/'):
        raise ValueError('Server requires a persistent absolute directory outside Actions _work')
    if not docker_context or docker_context in {'default', 'desktop-linux'}:
        raise ValueError('select the dedicated Server Docker context, isolated from the CI engine')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = json.loads((delivery / 'delivery.json').read_text())
        runtime = destination / 'runtime.env'
        values = read_environment(runtime)
        if values.get('SELF_HOST_STAGE') != receipt['stage'] or receipt['brand'] != 'eddy':
            raise ValueError('persistent host stage differs from the delivery')
        archive = delivery / 'server-images.tar'
        if sha256_file(archive) != receipt['files']['server-images.tar']:
            raise ValueError('image archive changed after admission')
        env = {**os.environ, 'DOCKER_CONTEXT': docker_context}
        # Import only after checking the accepted archive. Every service then uses
        # the content-addressed image ID, never its mutable tag.
        run(['docker', 'image', 'load', '--input', str(archive)], env=env, capture=True)
        values.update({key: receipt['images'][role]['image_id'] for role, key in accepted_image_keys(receipt).items()})
        verify_images(receipt, values, lambda args, **kwargs: run(args, env=env, **kwargs))
        # Keep bind-mounted source on the same host mount as the deployment.
        # A macOS /var/folders scratch path is not shared into the Server VM;
        # Docker otherwise creates an empty directory for searxng-settings.yml.
        release = (
            destination / 'qualifications' / f'{receipt["commit"]}-{uuid.uuid4().hex}'
            if qualification
            else destination / 'releases' / receipt['commit']
        )
        if release.exists():
            raise ValueError('release already has an execution directory; inspect its journal before retrying')
        source = release / 'source'
        source.mkdir(parents=True, mode=0o700)
        run(['git', 'init', '-q', str(source)])
        run(['git', 'fetch', '--depth=1', str(ROOT), receipt['commit']], cwd=source, capture=True)
        run(['git', 'checkout', '--detach', 'FETCH_HEAD'], cwd=source, capture=True)
        release_env = release / 'runtime.env'
        write_environment(release_env, values)
        save(release / 'delivery.json', receipt)
        env.update(
            SELF_HOST_ENV=str(release_env),
            SELF_HOST_PROJECT=f'eddy-server-{receipt["stage"]}',
            SELF_HOST_DELIVERY_RECEIPT=str(release / 'delivery.json'),
            PYTHON=sys.executable,
            SELF_HOST_BACKUP_KEY_FILE=str(destination / 'backup.key'),
        )
        journal = {
            'commit': receipt['commit'],
            'stage': receipt['stage'],
            'state': 'admitted',
            'previous': (
                json.loads((destination / 'current.json').read_text())
                if (destination / 'current.json').exists()
                else None
            ),
            'started_at': int(time.time()),
            'release_ready': False,
        }
        path = release / 'journal.json'
        save(path, journal)
        try:
            journal['state'] = 'boot-testing'
            save(path, journal)
            boot_test(release, values, env)
            if qualification:
                journal.update(
                    state='image-qualified',
                    artifact_qualified=True,
                    image_archive_sha256=receipt['files']['server-images.tar'],
                    images=receipt['images'],
                    execution_directory=str(release),
                    completed_at=int(time.time()),
                )
                save(path, journal)
                return journal
            if journal['previous']:
                previous = Path(journal['previous']['release'])
                prior_env = {
                    **env,
                    'SELF_HOST_ENV': str(previous / 'runtime.env'),
                    'SELF_HOST_START_MODE': 'immutable',
                    'SELF_HOST_DELIVERY_RECEIPT': str(previous / 'delivery.json'),
                }
                run(
                    [
                        'bash',
                        str(previous / 'source/deploy/self-host/operations.sh'),
                        'backup',
                        str(release / 'backup'),
                    ],
                    env=prior_env,
                )
            journal['state'] = 'deploying'
            save(path, journal)
            run(['bash', str(source / 'deploy/self-host/operations.sh'), 'deploy-images'], env=env)
            run([sys.executable, str(ROOT / 'scripts/fork/server_gateway.py'), '--release', str(release)], env=env)
            wait_ready(json.loads((release / 'metadata.json').read_text()))
            run(
                [
                    sys.executable,
                    str(ROOT / 'contracts/deployment/core.py'),
                    '--metadata',
                    str(release / 'metadata.json'),
                    '--remote',
                ],
                env=env,
            )
            run(
                [
                    sys.executable,
                    str(ROOT / 'scripts/fork/server_ai_acceptance.py'),
                    '--metadata',
                    str(release / 'metadata.json'),
                ],
                env=env,
            )
            run(
                [
                    'bash',
                    str(source / 'deploy/self-host/compose-clean-env.sh'),
                    str(release_env),
                    str(source / 'deploy/self-host/compose.production.yml'),
                    '--project-name',
                    env['SELF_HOST_PROJECT'],
                    'exec',
                    '-T',
                    'backend',
                    'python',
                    '-c',
                    'from fork.embedding import build; vector=build().embed_query("Synthetic release embedding"); assert len(vector)==1024; print("Embedding inference passed: 1024 dimensions")',
                ],
                env=env,
            )
            journal.update(state='deployed', release_ready=True, completed_at=int(time.time()))
            save(path, journal)
            save(destination / 'current.json', {'release': str(release), 'commit': receipt['commit']})
        except Exception as error:
            # Record why before re-raising: the workflow's failure step reads this
            # from the journal, and the previous behaviour left only the state.
            journal['state'] = 'failed-reconciliation-required'
            journal['failure'] = failure_record(receipt['stage'], error, values)
            save(path, journal)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', default='deploy', choices=['deploy', 'verify-images', 'qualify'])
    parser.add_argument('--delivery', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--docker-context')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    if args.operation == 'verify-images':
        verify_images(json.loads(args.receipt.read_text()), read_environment(args.env_file))
    else:
        result = deploy(
            args.delivery.resolve(),
            args.root,
            args.docker_context,
            qualification=args.operation == 'qualify',
        )
        if args.operation == 'qualify':
            print(
                json.dumps(
                    {key: result[key] for key in ('state', 'commit', 'artifact_qualified', 'execution_directory')}
                )
            )


if __name__ == '__main__':
    main()
