#!/usr/bin/env python3
"""Resume GitHub's immutable artifact ZIP and verify it before extraction."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from release_ci import REPOSITORY, github, sha256_file

FILES = {'delivery.json', 'cloudflare.tar.gz', 'server-images.tar', 'source.tar.gz'}
ATTEMPTS = 5


def signed_url(artifact_id):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, url):
            return None

    token = os.environ.get('GH_TOKEN') or subprocess.check_output(['gh', 'auth', 'token'], text=True).strip()
    request = Request(
        f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'},
    )
    try:
        response = build_opener(NoRedirect).open(request, timeout=30)
    except HTTPError as error:
        if error.code != 302:
            raise RuntimeError(f'GitHub artifact redirect failed: HTTP {error.code}') from None
        location = error.headers.get('Location', '')
        error.close()
    else:
        response.close()
        raise RuntimeError('GitHub artifact endpoint did not return a redirect')
    if urlsplit(location).scheme != 'https' or any(char in location for char in '\r\n'):
        raise ValueError('GitHub artifact redirect must use HTTPS')
    return location


def transfer(url, destination):
    # Keep the signed URL out of process arguments/logs. A fresh curl process
    # computes its resume offset from the retained file on every attempt.
    result = subprocess.run(
        [
            'curl',
            '-q',
            '--config',
            '-',
            '--fail',
            '--silent',
            '--show-error',
            '--location',
            '--proto',
            '=https',
            '--proto-redir',
            '=https',
            '--connect-timeout',
            '20',
            '--max-time',
            '300',
            '--speed-limit',
            '1024',
            '--speed-time',
            '30',
            '--continue-at',
            '-',
            '--output',
            str(destination),
        ],
        input='url = ' + json.dumps(url) + '\n',
        text=True,
        capture_output=True,
        timeout=320,
    )
    return result.returncode == 0


def unpack_delivery(archive, destination):
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise ValueError('download destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle, tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        members = [member for member in bundle.infolist() if member.filename in FILES]
        if len(members) != len(FILES) or {member.filename for member in members} != FILES:
            raise ValueError('artifact ZIP must contain each delivery file exactly once')
        for member in members:
            kind = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir() or kind not in (0, stat.S_IFREG) or not member.file_size:
                raise ValueError('delivery ZIP members must be nonempty ordinary files')
            target = Path(temporary) / member.filename
            with bundle.open(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
        os.rename(temporary, destination)


def download(run_id, name, destination, cache_root=None, api=github, locate=signed_url, fetch=transfer):
    artifacts = api(f'actions/runs/{int(run_id)}/artifacts?per_page=100')['artifacts']
    matches = [row for row in artifacts if row['name'] == name and not row['expired']]
    if len(matches) != 1:
        raise ValueError('run must have one unexpired artifact with the selected name')
    artifact = matches[0]
    digest = artifact.get('digest', '')
    if (
        not re.fullmatch('sha256:[0-9a-f]{64}', digest)
        or not isinstance(artifact['size_in_bytes'], int)
        or artifact['size_in_bytes'] <= 0
    ):
        raise ValueError('GitHub must supply the complete artifact size and SHA-256')
    expected = digest.split(':')[1]
    base = Path(cache_root) if cache_root else Path.home() / '.cache/eddy-delivery/summersmile1984-omi'
    directory = base / str(int(artifact['id'])) / expected
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / 'artifact.zip'
    partial = directory / 'artifact.zip.partial'
    with (directory / 'download.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if archive.is_symlink() or partial.is_symlink():
            raise ValueError('artifact cache files must not be symbolic links')
        if not archive.exists():
            for attempt in range(ATTEMPTS):
                offset = partial.stat().st_size if partial.exists() else 0
                if offset > artifact['size_in_bytes']:
                    raise ValueError('partial artifact exceeds the GitHub size')
                if offset == artifact['size_in_bytes']:
                    break
                print(
                    f'Artifact {artifact["id"]}: attempt {attempt + 1}, resuming at {offset}/{artifact["size_in_bytes"]} bytes',
                    file=sys.stderr,
                )
                complete = fetch(locate(artifact['id']), partial)
                if partial.exists():
                    partial.chmod(0o600)
                if complete and partial.exists() and partial.stat().st_size == artifact['size_in_bytes']:
                    break
            if not partial.is_file() or partial.stat().st_size != artifact['size_in_bytes']:
                raise RuntimeError('artifact transfer incomplete; partial ZIP retained for resume')
            if sha256_file(partial) != expected:
                raise ValueError('artifact ZIP hash differs from GitHub; cache not admitted')
            partial.rename(archive)
        if archive.stat().st_size != artifact['size_in_bytes'] or sha256_file(archive) != expected:
            raise ValueError('cached artifact ZIP differs from the current GitHub metadata')
        unpack_delivery(archive, destination)
    print(json.dumps({'artifact_id': artifact['id'], 'zip_sha256': expected, 'download_verified': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    download(args.run_id, args.artifact, args.directory)
