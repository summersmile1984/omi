"""The same reviewed case inventory consumed by the Python and Worker runners."""

import hashlib
import json
from pathlib import Path

_BYTES = Path(__file__).with_name('product-cases.json').read_bytes()
CONTRACT_SHA256 = hashlib.sha256(_BYTES).hexdigest()
_CONTRACT = json.loads(_BYTES)


def complete_cases(cases, suite, remote):
    expected = _CONTRACT['suites'][suite] + (_CONTRACT['remote_cleanup'].get(suite, []) if remote else [])
    ids = [row['id'] for row in cases]
    return len(set(ids)) == len(ids) and sorted(ids) == sorted(expected) and all(row['result'] == 'pass' for row in cases)
