#!/usr/bin/env python3
"""Transport only the candidate owner's frozen files, never build scratch links."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def candidate_files(raw, expected_digest=None):
    candidate = json.loads(raw)
    body = {key: value for key, value in candidate.items() if key != 'candidate_digest'}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    ).hexdigest()
    if candidate.get('candidate_digest') != digest or (expected_digest is not None and digest != expected_digest):
        raise ValueError('candidate digest differs from the accepted delivery')
    files = {}
    for root, entries in candidate['artifact_files'].items():
        for name, expected in entries.items():
            for value in [root, name]:
                path = PurePosixPath(value)
                if not path.parts or path.is_absolute() or '..' in path.parts or str(path) != value or '\\' in value:
                    raise ValueError('frozen manifest paths must be canonical relative paths')
            name = f'{root}/{name}'
            if name in files or name == 'candidate.json' or not re.fullmatch('[0-9a-f]{64}', expected):
                raise ValueError('frozen manifest contains a duplicate path or invalid hash')
            files[name] = expected
    if not files:
        raise ValueError('candidate declares no frozen files')
    return files


def pack_candidate(directory, output):
    directory = Path(directory).resolve()
    raw = (directory / 'candidate.json').read_bytes()
    files = candidate_files(raw)
    with tarfile.open(output, 'x:gz') as archive:
        for name in ['candidate.json', *sorted(files)]:
            path = directory / name
            if path.resolve() != path or not path.is_file():
                raise ValueError('frozen payload must contain ordinary files without links')
            if name in files and hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
                raise ValueError(f'frozen file hash changed: {name}')
            archive.add(path, arcname=f'cloudflare/{name}', recursive=False)


def unpack_candidate(path, destination, expected_digest):
    destination = Path(destination)
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        candidates = [member for member in members if member.name == 'cloudflare/candidate.json']
        if len(candidates) != 1 or not candidates[0].isfile() or candidates[0].size > 16 * 1024 * 1024:
            raise ValueError('archive requires one bounded ordinary candidate manifest')
        raw = archive.extractfile(candidates[0]).read()
        files = candidate_files(raw, expected_digest)
        wanted = {f'cloudflare/{name}': expected for name, expected in files.items()}
        wanted['cloudflare/candidate.json'] = hashlib.sha256(raw).hexdigest()
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        seen = set()
        for member in members:
            # The preparation workspace includes resources/, proof/ and staged
            # Web source. None are release inputs; never materialize
            # their contents, especially links back into the build machine.
            if member.name not in wanted:
                continue
            if not member.isfile() or member.name in seen:
                raise ValueError('declared frozen file is a link, special file or duplicate')
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.extractfile(member) as source, target.open('xb') as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != wanted[member.name]:
                raise ValueError(f'frozen file hash changed: {member.name}')
            target.chmod(member.mode & 0o755)
            seen.add(member.name)
        if seen != set(wanted):
            raise ValueError('archive is missing declared frozen files')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['pack'])
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    pack_candidate(args.candidate, args.output)
