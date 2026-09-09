#!/usr/bin/env python3
"""Run actionlint with the fork label catalog after resolving constant selectors."""

import json
from pathlib import Path
import re
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ['fork-checks.yml', 'fork-release-prepare.yml', 'fork-cd-cloudflare.yml', 'fork-cd-server.yml']
CONSTANT_RUNNER = re.compile(r"\$\{\{ *fromJSON\('([^'\n]*)'\) *\}\}")


def resolve_constant_runners(source):
    def entries(node):
        return dict((key.value, value) for key, value in node.value) if isinstance(node, yaml.MappingNode) else {}

    jobs = entries(entries(yaml.compose(source)).get('jobs'))
    replacements = []
    for job in jobs.values():
        runner = entries(job).get('runs-on')
        if not isinstance(runner, yaml.ScalarNode):
            continue
        match = CONSTANT_RUNNER.fullmatch(runner.value)
        if not match:
            continue
        labels = json.loads(match[1])
        if not isinstance(labels, list) or not labels or any(not isinstance(label, str) for label in labels):
            raise ValueError('constant runner selector must contain a nonempty list of labels')
        replacements.append((runner.start_mark.index, runner.end_mark.index, json.dumps(labels)))

    for start, end, labels in reversed(replacements):
        source = source[:start] + labels + source[end:]
    return source


def check(path):
    # The upstream catalog cannot contain fork-only runner labels. Constant
    # fromJSON selectors are valid GitHub syntax; resolve them for the fork
    # linter so misspelled labels still fail the actual actionlint rule.
    result = subprocess.run(
        ['actionlint', '-config-file', str(ROOT / '.github/actionlint.fork.yaml'), '-stdin-filename', str(path), '-'],
        input=resolve_constant_runners(path.read_text()),
        text=True,
        cwd=ROOT,
    )
    return result.returncode


if __name__ == '__main__':
    paths = [Path(path) for path in sys.argv[1:]] or [ROOT / '.github/workflows' / name for name in WORKFLOWS]
    sys.exit(max(check(path) for path in paths))
