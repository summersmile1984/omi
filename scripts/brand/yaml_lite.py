"""A deliberately small YAML reader, shared by apply.py and check.py.

Every fork script in this repo reads its own YAML with a hand-rolled subset
rather than depending on PyYAML -- scripts/fork/check-upstream-touch.py's
allowlist parser and scripts/profiles/render.py's profile-table parser are
the two existing precedents, both for the same reason: these files must be
readable by a bare Python with no install step, on any machine, before any
environment (backend/.venv or otherwise) is provisioned. This is closely
adapted from scripts/profiles/render.py's own reader, which already handles
the structure brand manifests need: nested mappings by indentation, inline
`[a, b]` lists, block `- ` lists, quoted strings, and int/bool/null scalars.
"""

from __future__ import annotations

from pathlib import Path


class YamlError(RuntimeError):
    pass


def _strip_inline_comment(text: str) -> str:
    """Drop a trailing ` # comment`, leaving quoted values intact."""
    text = text.strip()
    if text[:1] in ('"', "'"):
        closing = text.find(text[0], 1)
        if closing != -1:
            return text[: closing + 1]
        return text
    cut = text.find(" #")
    return text[:cut].rstrip() if cut != -1 else text


def _unquote_key(text: str) -> str:
    """Strip surrounding quotes from a mapping key, nothing else.

    Unlike `_scalar`, a key is never int/bool/null-coerced -- "true" or "123"
    used as a key must stay the string it looks like.
    """
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _scalar(text: str):
    text = _strip_inline_comment(text)
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        return text


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise YamlError(f"missing file: {path}")

    # Pre-filter to (lineno, indent, content) triples, comments and blank
    # lines dropped, so a bare "key:" can look ahead to see whether the next
    # real line is a "- " item (this key holds a block list) or something
    # else (this key holds a nested mapping) -- YAML's own grammar requires
    # that lookahead; a single forward pass over raw lines cannot resolve it.
    entries: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        entries.append((lineno, len(raw) - len(raw.lstrip()), raw.strip()))

    root: dict = {}
    # stack of (indent, container) so nesting follows indentation
    stack: list[tuple[int, object]] = [(-1, root)]
    pending_list: list | None = None
    pending_indent = -1

    for i, (lineno, indent, line) in enumerate(entries):
        if line.startswith("- "):
            if pending_list is None or indent <= pending_indent:
                raise YamlError(f"{path}:{lineno}: list item outside a list key")
            pending_list.append(_scalar(line[2:]))
            continue

        pending_list = None
        if ":" not in line:
            raise YamlError(f"{path}:{lineno}: expected 'key: value'")
        key, _, value = line.partition(":")
        key, value = _unquote_key(key.strip()), value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise YamlError(f"{path}:{lineno}: broken indentation")
        parent = stack[-1][1]

        if value == "":
            next_entry = entries[i + 1] if i + 1 < len(entries) else None
            next_is_list_item = next_entry is not None and next_entry[1] > indent and next_entry[2].startswith("- ")
            if next_is_list_item:
                parent[key] = []
                pending_list = parent[key]
                pending_indent = indent
            else:
                child: dict = {}
                parent[key] = child
                stack.append((indent, child))
        elif value == "[]":
            parent[key] = []
        else:
            parent[key] = _scalar(value)
    return root
