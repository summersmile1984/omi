"""One module per apply.py category (dev/unified-main/04-brand-layer.md §4).

Each module exposes `render(manifest: dict, repo_root: Path) -> list[Path]`.
apply.py's own GENERATORS dict imports and registers them explicitly --
nothing here is auto-discovered, so a new category is always a one-line
addition to apply.py, not an implicit side effect of adding a file.
"""

from __future__ import annotations
