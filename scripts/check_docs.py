#!/usr/bin/env python3
"""Documentation checks for the agent-facing doc system.

Three hard checks (any failure exits non-zero):

1. Internal links resolve. Relative markdown links in root and docs/ pages must
   point at a file that exists; anchors are resolved by the reader, not here.
2. Line budgets hold. ``AGENTS.md`` stays a router (<= 220 lines) and each
   topic doc in ``docs/`` stays within 180 lines. Long-form reference notes
   (``docs/games/``, ``docs/releases/``, ``docs/issue-drafts/``) are exempt.
3. Every top-level topic doc is registered in ``docs/README.md``, so a new page
   cannot be added without appearing in the map.

One advisory check (warns, never fails): each topic doc opens with the
``何时读 / 何时不用读 / 复核`` metadata block.

Run from the repository root:

    python scripts/check_docs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENTRY_FILE = ROOT / "AGENTS.md"
ENTRY_MAX_LINES = 220
DOC_MAX_LINES = 180
EXEMPT_DIRS = ("games", "releases", "issue-drafts")

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    files = [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "README.en.md", ROOT / "CONTRIBUTING.md"]
    files += sorted(ROOT.glob("docs/**/*.md"))
    files += sorted(ROOT.glob("games/**/*.md"))
    return [f for f in files if f.is_file()]


def internal_targets(text: str) -> list[str]:
    targets = []
    for raw in LINK_RE.findall(text):
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        elif " " in target:
            target = target.split(" ", 1)[0]
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        targets.append(target.split("#", 1)[0])
    return [t for t in targets if t]


def check_links(files: list[Path]) -> list[str]:
    problems = []
    for md in files:
        text = md.read_text(encoding="utf-8")
        for target in internal_targets(text):
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                where = md.relative_to(ROOT).as_posix()
                problems.append(f"{where}: broken link -> {target}")
    return problems


def check_budgets() -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    if ENTRY_FILE.is_file():
        count = len(ENTRY_FILE.read_text(encoding="utf-8").splitlines())
        if count > ENTRY_MAX_LINES:
            problems.append(
                f"AGENTS.md: {count} lines, budget {ENTRY_MAX_LINES} — move detail into docs/"
            )
    for md in sorted((ROOT / "docs").rglob("*.md")):
        rel = md.relative_to(ROOT / "docs")
        if rel.parts and rel.parts[0] in EXEMPT_DIRS:
            continue
        if md.name == "README.md":
            continue
        count = len(md.read_text(encoding="utf-8").splitlines())
        if count > DOC_MAX_LINES:
            problems.append(
                f"docs/{rel.as_posix()}: {count} lines, budget {DOC_MAX_LINES} — split by topic"
            )
        head = "\n".join(md.read_text(encoding="utf-8").splitlines()[:20])
        if len(re.findall(r"^> \*\*.+\*\*", head, re.MULTILINE)) < 2:
            warnings.append(f"docs/{rel.as_posix()}: missing 何时读/复核 metadata block")
    return problems, warnings


def check_registry() -> list[str]:
    index = ROOT / "docs" / "README.md"
    if not index.is_file():
        return ["docs/README.md is missing — the doc map is the entry point for docs/"]
    text = index.read_text(encoding="utf-8")
    problems = []
    for md in sorted((ROOT / "docs").glob("*.md")):
        if md.name == "README.md":
            continue
        if md.name not in text:
            problems.append(f"docs/{md.name}: not listed in docs/README.md")
    return problems


def main() -> int:
    files = markdown_files()
    failures = check_links(files)
    budget_problems, warnings = check_budgets()
    failures += budget_problems
    failures += check_registry()

    for warning in warnings:
        print(f"warn: {warning}")
    for failure in failures:
        print(f"fail: {failure}")

    if failures:
        print(f"\n{len(failures)} problem(s) across {len(files)} markdown files.")
        return 1
    print(f"docs ok: {len(files)} files, links resolve, budgets hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
