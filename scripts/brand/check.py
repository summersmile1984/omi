#!/usr/bin/env python3
"""Guard: no other brand's identity leaks into this brand's surfaces.

Three faces (dev/unified-main/04-brand-layer.md §6):
  1. lexicon leak scan -- upstream's brand words/domains/identifiers must not
     appear in a non-upstream brand's user-visible output.
  2. apply.py --check-clean -- generated files match the manifest exactly
     (delegated to apply.py itself; this script just calls it).
  3. credential scan -- upstream's own service identifiers (hosting
     hostnames, project domains) must not appear in a non-upstream brand's
     build. Real secret VALUES are never checked into a scanner's word list;
     see lexicon.yaml's own note on this.

`--brand omi-upstream` is the self-check case: upstream saying "Omi" is not a
leak, so face 1 there reports a match COUNT (the scanner proving it can see
what upstream's own tree actually contains) rather than a pass/fail verdict.
Every other brand's matches are real leaks and must be zero.

Coverage today (B0): ARB translation values, Swift/Dart string-literal call
sites (Text/Button/Label/.alert/.help -- a heuristic regex, not a full
Swift/Dart parser), Info.plist string values, prompt/notification template
files by path, templates/*.html, docs/docs.json, firmware brand-identity
source (omi.conf, nfc.c), config/public-build-values.json, Dockerfile URL
literals. Not yet covered: OpenAPI output (needs a running backend to
generate -- see backend/scripts/export_openapi.py -- not just a static file,
deferred until B4 gives it a brand.py to import), store metadata (no
generator or fixed location exists until B1-B8 land content there).

Usage:
    scripts/brand/check.py --brand <id> [--base upstream/main] [--baseline PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_PATH = Path(__file__).resolve().parent / "lexicon.yaml"
ALLOW_PATH = REPO_ROOT / "brand/_allow.yaml"
UPSTREAM_BRAND_ID = "omi-upstream"

# Path globs for each named surface in §6. Deliberately explicit rather than
# "scan everything" -- deploy/, backend/fork/, scripts/, dev/ are fork
# infrastructure and design docs, not brand-facing product surfaces, and
# scanning them would bury real leaks in noise from files that are ABOUT the
# brand system rather than PART OF the branded product.
SURFACE_GLOBS: dict[str, tuple[str, ...]] = {
    "mobile_l10n": ("app/lib/l10n/*.arb",),
    "mobile_source": ("app/lib/**/*.dart",),
    "macos_source": ("desktop/macos/Desktop/**/*.swift",),
    "macos_plist": ("desktop/macos/Desktop/**/Info.plist",),
    "windows_source": ("desktop/windows/src/**/*.ts", "desktop/windows/src/**/*.tsx"),
    "backend_prompts": (
        "backend/utils/llm/**/*.py",
        "backend/llm_gateway/**/*.py",
    ),
    "backend_templates": ("backend/templates/*.html",),
    "docs_config": ("docs/docs.json",),
    "firmware_identity": (
        "omi/firmware/omi/omi.conf",
        "omi/firmware/omi/src/lib/core/nfc.c",
    ),
    "build_config": ("config/public-build-values.json",),
    "dockerfiles": ("**/Dockerfile*",),
}

# service_identifiers, scanned everywhere the code surfaces above are
# scanned -- never against real secret VALUES, only public-safe hostnames
# and project identifiers (lexicon.yaml's own note explains why).
SERVICE_IDENTIFIER_KEY = "service_identifiers"


@dataclass
class Match:
    surface: str
    path: str
    line: int
    word: str
    context: str


@dataclass
class Exemption:
    glob: str
    words: frozenset[str] | None
    reason: str


def load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_exemptions() -> list[Exemption]:
    if not ALLOW_PATH.exists():
        return []
    data = load_yaml(ALLOW_PATH)
    out = []
    for entry in data.get("exemptions", []):
        words = entry.get("words")
        out.append(Exemption(glob=entry["glob"], words=frozenset(words) if words else None, reason=entry["reason"]))
    return out


def is_exempt(path: str, word: str, exemptions: list[Exemption]) -> bool:
    for ex in exemptions:
        if fnmatch.fnmatch(path, ex.glob) or path == ex.glob:
            if ex.words is None or word in ex.words:
                return True
    return False


def iter_surface_files(repo_root: Path = REPO_ROOT) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for surface, globs in SURFACE_GLOBS.items():
        found: list[Path] = []
        for glob in globs:
            found.extend(repo_root.glob(glob))
        out[surface] = sorted(set(found))
    return out


_SWIFT_LITERAL_RE = re.compile(r"""(?:Text|Button|Label|\.alert|\.help)\([^)]*?['"]([^'"]*)['"]""", re.DOTALL)
_ARB_VALUE_RE = re.compile(r'"[^"]+"\s*:\s*"([^"]*)"')
_PLIST_STRING_RE = re.compile(r"<string>([^<]*)</string>")


def _lines_matching(text: str, patterns: list[re.Pattern]) -> list[tuple[int, str]]:
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in patterns:
            for m in pattern.finditer(line):
                out.append((lineno, m.group(1) if m.groups() else line))
    return out


def extract_candidate_strings(surface: str, path: Path) -> list[tuple[int, str]]:
    """Return (line_number, extracted_string) pairs worth lexicon-checking.

    For surfaces without a narrower extractor (templates, JSON config,
    firmware source, Dockerfiles), the whole line is the candidate -- these
    are already small, brand-relevant files where a substring hit is
    meaningful without extracting a specific literal first.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if surface == "mobile_l10n":
        return _lines_matching(text, [_ARB_VALUE_RE])
    if surface in ("macos_source", "windows_source"):
        return _lines_matching(text, [_SWIFT_LITERAL_RE])
    if surface == "macos_plist":
        return _lines_matching(text, [_PLIST_STRING_RE])
    if surface == "mobile_source":
        # Dart's own user-facing-string shape (Text(...)) mirrors Swift's
        # closely enough to reuse the same heuristic literal extractor.
        return _lines_matching(text, [_SWIFT_LITERAL_RE, _ARB_VALUE_RE])
    return [(i, line) for i, line in enumerate(text.splitlines(), start=1)]


def scan(brand_id: str, exemptions: list[Exemption], repo_root: Path = REPO_ROOT) -> list[Match]:
    lexicon = load_yaml(LEXICON_PATH)
    words = list(lexicon.get("words", []))
    substrings = list(lexicon.get("domains", [])) + list(lexicon.get("identifiers", []))
    word_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE) if words else None

    matches: list[Match] = []
    for surface, paths in iter_surface_files(repo_root).items():
        for path in paths:
            rel = str(path.relative_to(repo_root))
            for lineno, candidate in extract_candidate_strings(surface, path):
                if word_re:
                    for m in word_re.finditer(candidate):
                        hit = m.group(1)
                        if is_exempt(rel, hit, exemptions):
                            continue
                        matches.append(Match(surface, rel, lineno, hit, candidate.strip()[:120]))
                for sub in substrings:
                    if sub.lower() in candidate.lower():
                        if is_exempt(rel, sub, exemptions):
                            continue
                        matches.append(Match(surface, rel, lineno, sub, candidate.strip()[:120]))
    return matches


def scan_service_identifiers(repo_root: Path = REPO_ROOT) -> list[Match]:
    lexicon = load_yaml(LEXICON_PATH)
    identifiers = list(lexicon.get(SERVICE_IDENTIFIER_KEY, []))
    if not identifiers:
        return []
    exemptions = load_exemptions()
    matches: list[Match] = []
    for surface, paths in iter_surface_files(repo_root).items():
        for path in paths:
            rel = str(path.relative_to(repo_root))
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for identifier in identifiers:
                    if identifier.lower() in line.lower() and not is_exempt(rel, identifier, exemptions):
                        matches.append(Match(f"{surface}:service-id", rel, lineno, identifier, line.strip()[:120]))
    return matches


def run_apply_check_clean(brand_id: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "apply.py"), "--brand", brand_id, "--check-clean"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand", required=True)
    parser.add_argument(
        "--base", default="upstream/main", help="unused today; reserved for a future upstream-diff-scoped scan"
    )
    parser.add_argument(
        "--baseline", type=Path, help="path to a baseline count file; ratchets (never allows an increase)"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    exemptions = load_exemptions()
    lexicon_matches = scan(args.brand, exemptions)
    service_matches = scan_service_identifiers() if args.brand != UPSTREAM_BRAND_ID else []
    clean_ok, clean_detail = run_apply_check_clean(args.brand)

    is_self_check = args.brand == UPSTREAM_BRAND_ID
    total = len(lexicon_matches) + len(service_matches)

    if args.json:
        print(
            json.dumps(
                {
                    "brand": args.brand,
                    "self_check": is_self_check,
                    "lexicon_matches": total if is_self_check else len(lexicon_matches),
                    "service_identifier_matches": len(service_matches),
                    "apply_check_clean": clean_ok,
                }
            )
        )
    else:
        label = "match count (self-check, not leaks)" if is_self_check else "leak(s) found"
        print(f"face 1 (lexicon): {len(lexicon_matches)} {label}")
        if not is_self_check:
            print(f"face 3 (service identifiers): {len(service_matches)} leak(s) found")
        print(f"face 2 (apply.py --check-clean): {'OK' if clean_ok else 'FAIL: ' + clean_detail}")
        if not is_self_check:
            for m in lexicon_matches[:50]:
                print(f"  LEAK  {m.path}:{m.line}  {m.word!r}  in: {m.context}")
            for m in service_matches[:50]:
                print(f"  LEAK  {m.path}:{m.line}  {m.word!r}  in: {m.context}")
            if len(lexicon_matches) + len(service_matches) > 50:
                print(f"  ... and {len(lexicon_matches) + len(service_matches) - 50} more")

    if args.baseline:
        if args.baseline.exists():
            previous = int(args.baseline.read_text(encoding="utf-8").strip())
            if total > previous:
                print(
                    f"FAIL: leak count grew from {previous} to {total} -- ratchet only allows a decrease",
                    file=sys.stderr,
                )
                return 1
            if not args.baseline.parent.exists():
                args.baseline.parent.mkdir(parents=True)
            if total < previous:
                args.baseline.write_text(f"{total}\n", encoding="utf-8")
                print(f"baseline lowered: {previous} -> {total}")
        else:
            args.baseline.parent.mkdir(parents=True, exist_ok=True)
            args.baseline.write_text(f"{total}\n", encoding="utf-8")
            print(f"baseline recorded: {total}")
        return 0

    if is_self_check:
        return 0 if clean_ok else 1
    return 0 if (total == 0 and clean_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
