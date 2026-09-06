#!/usr/bin/env python3
"""Stage the unchanged, persistence-free canonical memory apply rules for Core."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULES = {
    'models.memory_apply': 'memory_kernel_apply',
    'models.memory_admission': 'memory_kernel_admission',
    'models.memory_contracts': 'memory_kernel_contracts',
    'models.memory_promotion': 'memory_kernel_promotion',
    'models.memory_domain': 'memory_kernel_domain',
    'models.memory_operations': 'memory_kernel_operations',
    'models.product_memory': 'memory_kernel_item',
    'models.memory_evidence': 'memory_kernel_evidence',
    'utils.memory.short_term_lifecycle': 'memory_kernel_short_term_lifecycle',
}


def project(module: str) -> str:
    path = ROOT / 'backend' / (module.replace('.', '/') + '.py')
    if not path.is_file() or path.is_symlink():
        raise ValueError('memory kernel source must be an ordinary repository file')
    content = path.read_text()
    lines = content.splitlines(keepends=True)
    replacements = []
    for node in ast.walk(ast.parse(content)):
        if isinstance(node, ast.ImportFrom) and node.module:
            dependency = node.module
            if dependency.startswith(('models.', 'utils.')):
                if node.level or dependency not in MODULES:
                    raise ValueError('memory kernel dependency changed; review the source boundary')
                replacements.append((node.lineno - 1, node.col_offset, dependency))
        elif isinstance(node, ast.Import):
            if any(alias.name.startswith(('models.', 'utils.')) for alias in node.names):
                raise ValueError('memory kernel package import requires an explicit projection')
    for line, column, dependency in sorted(replacements, reverse=True):
        before, after = 'from ' + dependency + ' import ', 'from ' + MODULES[dependency] + ' import '
        if not lines[line][column:].startswith(before):
            raise ValueError('memory kernel import syntax changed')
        lines[line] = lines[line][:column] + after + lines[line][column + len(before) :]
    return ''.join(lines)


def generate(output: Path) -> None:
    outputs = {target + '.py': project(module) for module, target in MODULES.items()}
    for name, content in outputs.items():
        compile(content, name, 'exec')
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError('memory kernel projection collides with another source owner')
    for name, content in outputs.items():
        (output / name).write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    generate(parser.parse_args().output)
