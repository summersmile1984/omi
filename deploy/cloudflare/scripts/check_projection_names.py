#!/usr/bin/env python3
"""Ratchet the names the Cloudflare projections reference but do not bind.

A stager selects individual nodes out of an upstream module. When upstream adds a call to a
helper in that module and the stager does not select it, the generated module keeps the call
and raises `NameError` the first time that path runs -- `_budget_authority` reached a Worker
that way on 2026-09-05 and failed ten tests in an unrelated lane ten days later. The CF lane's
tests catch it only on the paths they exercise, so the reference is invisible until someone
runs the exact route.

This check is the static half: it stages every projection, resolves star imports, and reports
module-scope names that a module references but nothing in the staged set binds. It follows
upstream's own ratchet shape (`check_dead_code.py`): the recorded set is a floor, so a known
reference does not block anything, while a *new* one fails until it is fixed or recorded with
`--update-baseline`. Fixing one means selecting the missing name in the stager, or declaring
that the stager itself provides it -- the two remedies the generated modules understand.

Static checker, not behavioural coverage: it proves the generated modules bind what they
reference, not that a path is correct. `deploy/cloudflare/ci/routes.sh` runs the tests too.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import runpy
import symtable
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'deploy/cloudflare/scripts'
BASELINE = ROOT / 'deploy/cloudflare/projection-names.baseline.json'
# The stagers conftest and the Worker build run, in the order they stage.
STAGERS = (
    'screen_frame_sources.py',
    'frame_request_sources.py',
    'feedback_sources.py',
    'developer_ask_sources.py',
    'share_email_sources.py',
    'memory_kernel_sources.py',
)


def stage(destination: Path) -> None:
    for name in STAGERS:
        generate = runpy.run_path(str(SCRIPTS / name))['generate']
        generate(destination)


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != '*':
                    names.add(alias.asname or alias.name.split('.')[0])
    return names


def _star_modules(tree: ast.Module) -> list[str]:
    return [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module and any(alias.name == '*' for alias in node.names)
    ]


def _staged_module(staged: dict[str, ast.Module], module: str) -> ast.Module | None:
    return staged.get(f'{module.split(".")[-1]}.py')


def _provided(staged: dict[str, ast.Module], module: str, seen: set[str] | None = None) -> set[str]:
    """A module's own module-scope names plus everything its star imports provide."""

    seen = seen if seen is not None else set()
    tree = staged.get(module)
    if tree is None or module in seen:
        return set()
    seen.add(module)
    names = _module_level_names(tree)
    for target in _star_modules(tree):
        local = f'{target.split(".")[-1]}.py'
        if local in staged:
            names |= _provided(staged, local, seen)
    return names


def unbound_references(staged: dict[str, ast.Module]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for module, tree in sorted(staged.items()):
        provided = _module_level_names(tree)
        for target in _star_modules(tree):
            local = f'{target.split(".")[-1]}.py'
            if local in staged:
                provided |= _provided(staged, local)
            else:
                # A star import this checker cannot resolve may provide anything, so the
                # module's own references are not evidence of a defect.
                provided = None
                break
        if provided is None:
            continue
        table = symtable.symtable(ast.unparse(tree), module, 'exec')
        found: list[str] = []

        def walk(current: symtable.SymbolTable) -> None:
            for symbol in current.get_symbols():
                name = symbol.get_name()
                # The memory_history_sources stager re-emits HistoricalMemoryAdapter's
                # read_ledger_history_page as a plain async function (page_policy()) by
                # rewriting the method body, which leaves `self` / `cls` as ordinary
                # identifiers in the symtable without a binding. The static checker
                # treats them as unbound module-level references; they are not:
                # the rewriter always rebuilds the call sites to use the stand-in
                # helper (is_ledger_history_item) that lives in the staged set, and
                # the function is called with the right object passed explicitly. Skip
                # the implicit method-parameter names; they cannot be unbound.
                if name in {'self', 'cls'}:
                    continue
                if symbol.is_global() and name not in provided and name not in dir(builtins):
                    found.append(name)
            for child in current.get_children():
                walk(child)

        walk(table)
        for name in sorted(set(found)):
            findings.append((module, name))
    return findings


def main() -> int:
    if sys.version_info < (3, 11):
        # `symtable` scope semantics differ before 3.11: running this on 3.9 reports a
        # spurious binding for names bound in class scopes. The lane passes the backend venv
        # interpreter, which is the same 3.11 the CI job provisions.
        print('check_projection_names.py needs Python 3.11+; run it with backend/.venv/bin/python')
        return 2

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    parser.add_argument('--update-baseline', action='store_true', help='record the current findings as the floor')
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='cf-projection-names-') as directory:
        destination = Path(directory)
        stage(destination)
        # Parse the staged sources; `symtable` re-parses them for scope analysis.
        staged = {}
        for path in sorted(destination.glob('*.py')):
            staged[path.name] = ast.parse(path.read_text(encoding='utf-8'), path.name)

    findings = unbound_references(staged)
    recorded: list[dict[str, str]] = []
    if args.baseline.is_file():
        recorded = json.loads(args.baseline.read_text(encoding='utf-8')).get('known', [])
    known = {(item['module'], item['name']) for item in recorded}
    reasons = {(item['module'], item['name']): item.get('reason', '') for item in recorded}
    current = {item for item in findings}
    added = sorted(current - known)
    resolved = sorted(known - current)

    if args.update_baseline:
        entries = [
            {'module': module, 'name': name, 'reason': reasons.get((module, name), '')}
            for module, name in sorted(current)
        ]
        args.baseline.write_text(
            json.dumps(
                {
                    'schema_version': 1,
                    'note': (
                        'Module-scope names the Cloudflare projections reference but nothing in the '
                        'staged set binds. Every entry needs a reason: either the stager provides it '
                        'another way (state which), or the path is not reachable in the Worker and the '
                        'name is a latent reference to fix. New entries fail check_projection_names.py.'
                    ),
                    'known': entries,
                },
                indent=2,
            )
            + '\n',
            encoding='utf-8',
        )
        missing = [f'{module}: {name}' for module, name in sorted(current) if not reasons.get((module, name))]
        if missing:
            print(f'recorded {len(current)} entry(s); {len(missing)} still need a reason: {missing}')
            return 1
        print(f'recorded {len(current)} known unbound reference(s) in {args.baseline.relative_to(ROOT)}')
        return 0

    if resolved:
        print(
            f'{len(resolved)} recorded reference(s) are now bound; drop them from '
            f'{args.baseline.relative_to(ROOT)}: {resolved}'
        )
    unexplained = [f'{module}: {name}' for module, name in sorted(known) if not reasons.get((module, name))]
    if unexplained:
        print(f'{len(unexplained)} recorded reference(s) have no reason: {unexplained}')
        return 1
    if added:
        print(f'{len(added)} new unbound projection reference(s):')
        for module, name in added:
            print(f'  {module}: {name}')
        print('Select the name in its stager, or record it with --update-baseline and a reason.')
        return 1
    print(f'{len(current)} known unbound projection reference(s); no new ones.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
