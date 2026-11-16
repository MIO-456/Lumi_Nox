#!/usr/bin/env python3
"""Documentation checks for the agent-facing doc system.

Two hard checks (any failure exits non-zero):

1. Internal links resolve. Relative markdown links in root and docs/ pages must
   point at a file that exists; anchors are resolved by the reader, not here.
2. Every top-level topic doc is registered in ``docs/README.md``, so a new page
   cannot be added without appearing in the map.

Advisory checks (warn, never fail): entry/topic length and the opening
``何时读 / 何时不用读 / 复核`` metadata block. Keep useful documents together;
exceeding a recommended length does not require splitting them.

Run from the repository root:

    python scripts/check_docs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

ENTRY_FILE = ROOT / "AGENTS.md"
ENTRY_MAX_LINES = 220
DOC_MAX_LINES = 180
EXEMPT_DIRS = ("games", "releases", "issue-drafts")

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)


def markdown_files() -> list[Path]:
    files = [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "README.en.md", ROOT / "CONTRIBUTING.md"]
    files += sorted(ROOT.glob("docs/**/*.md"))
    files += sorted(ROOT.glob("games/**/*.md"))
    return [f for f in files if f.is_file()]


def prose_only(text: str) -> str:
    """Exclude fenced blocks and matching backtick code spans from link checks."""
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        match = FENCE_RE.match(line.rstrip("\r\n"))
        if fence is not None:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
            lines.append("\n")
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            fence = match[1]
            lines.append("\n")
        else:
            lines.append(line)
    return INLINE_CODE_RE.sub("", "".join(lines))


def internal_targets(text: str) -> list[str]:
    targets = []
    for raw in LINK_RE.findall(prose_only(text)):
        target = raw.strip()
        if target.startswith("<") and ">" in target:
            target = target[1:target.index(">")]
        else:
            target = target.split(maxsplit=1)[0] if target else ""
        if (not target or target.startswith(("#", "//"))
                or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target)):
            continue
        # Split URL suffixes before decoding: %23 may be part of a file name.
        targets.append(unquote(target.split("#", 1)[0].split("?", 1)[0]))
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
            warnings.append(
                f"AGENTS.md: {count} lines, recommended {ENTRY_MAX_LINES} — review whether detail belongs in docs/"
            )
    for md in sorted((ROOT / "docs").rglob("*.md")):
        rel = md.relative_to(ROOT / "docs")
        if rel.parts and rel.parts[0] in EXEMPT_DIRS:
            continue
        if md.name == "README.md":
            continue
        count = len(md.read_text(encoding="utf-8").splitlines())
        if count > DOC_MAX_LINES:
            warnings.append(
                f"docs/{rel.as_posix()}: {count} lines, recommended {DOC_MAX_LINES} — review readability"
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
    registered = {(index.parent / target).resolve() for target in internal_targets(text)}
    problems = []
    for md in sorted((ROOT / "docs").glob("*.md")):
        if md.name == "README.md":
            continue
        if md.resolve() not in registered:
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
    print(f"docs ok: {len(files)} files, links resolve, topics are indexed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
