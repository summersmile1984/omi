"""Rewrite compiler-located Swift declarations in an isolated source stage.

The Swift parser owns declaration boundaries. Exact owner digests require an
explicit review when upstream changes the replaced behavior; no regex guesses
where a nested Swift function or string literal ends.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path


class OverlayError(RuntimeError):
    pass


def declarations(path: Path) -> dict[str, list[tuple[int, int]]]:
    result = subprocess.run(
        ["xcrun", "swiftc", "-frontend", "-dump-parse", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    source = path.read_bytes()
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    found: dict[str, list[tuple[int, int]]] = {}
    pattern = re.compile(r'\((?:func|constructor)_decl .*?range=\[.*?:(\d+):(\d+) - line:(\d+):(\d+)\] "([^"]+)"')
    for match in pattern.finditer(result.stdout + result.stderr):
        first_line, first_col, last_line, last_col = map(int, match.group(1, 2, 3, 4))
        start = offsets[first_line - 1] + first_col - 1
        end = offsets[last_line - 1] + last_col
        found.setdefault(match[5], []).append((start, end))
    return found


def verify_owner(key: str, value: bytes, owners: dict[str, str]) -> None:
    if owners.get(key) != hashlib.sha256(value).hexdigest():
        raise OverlayError(f"Swift owner changed and requires review: {key}")


def rewrite_functions(path: Path, replacements: dict[str, str], owners: dict[str, str]) -> None:
    ranges = declarations(path)
    source = path.read_bytes()
    edits = []
    for name, replacement in replacements.items():
        matches = ranges.get(name, [])
        if len(matches) != 1:
            raise OverlayError(f"Expected one Swift declaration {path.name}:{name}, got {len(matches)}")
        start, end = matches[0]
        verify_owner(f"{path.name}:{name}", source[start:end], owners)
        edits.append((start, end, replacement.encode()))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    path.write_bytes(source)


def rewrite_region(path: Path, start: str, end: str, replacement: str, key: str, owners: dict[str, str]) -> None:
    source = path.read_text()
    if source.count(start) != 1 or source.count(end) != 1:
        raise OverlayError(f"Expected one Swift region {key}")
    left = source.index(start)
    right = source.index(end, left)
    verify_owner(key, source[left:right].encode(), owners)
    path.write_text(source[:left] + replacement + source[right:])


def replace_literal(path: Path, old: str, new: str, expected: int = 1) -> None:
    source = path.read_text()
    if source.count(old) != expected:
        raise OverlayError(f"Swift literal owner changed in {path.name}")
    path.write_text(source.replace(old, new))


def replace_brand_literals(path: Path, entries: list[tuple[str, int]], display: str) -> None:
    """Substitute `display` for "Omi" inside each reviewed literal, one pass.

    All raw occurrence counts are validated against the reviewed pins first;
    the single regex pass (longest alternative first) means overlapping
    literals such as "Close Omi Chat" inside "Close Omi Chat (Esc)" are each
    rewritten exactly once, at the longest match. Byte-identical no-op for
    the omi-upstream brand.
    """
    with open(path, encoding="utf-8", newline="") as handle:
        source = handle.read()
    for literal, expected in entries:
        if source.count(literal) != expected:
            raise OverlayError(
                f"Brand copy owner changed in {path.name}: {literal!r} (expected {expected})"
            )
    if display == "Omi":
        return
    pattern = re.compile(
        "|".join(sorted((re.escape(literal) for literal, _ in entries), key=len, reverse=True))
    )
    rewritten = pattern.sub(lambda match: match.group(0).replace("Omi", display), source)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(rewritten)


def load_owners() -> dict[str, str]:
    return json.loads((Path(__file__).parent / "source-owners.json").read_text())
