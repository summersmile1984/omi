"""One module per apply.py category (dev/unified-main/04-brand-layer.md §4).

Each module exposes `render(manifest: dict) -> dict[str, str]` (relative path to UTF-8 content).
Generators are pure: apply.py owns writing and read-only byte comparison.
apply.py's own GENERATORS dict imports and registers them explicitly --
nothing here is auto-discovered, so a new category is always a one-line
addition to apply.py, not an implicit side effect of adding a file.
"""

from __future__ import annotations
