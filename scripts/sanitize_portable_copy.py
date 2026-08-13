#!/usr/bin/env python3
"""Replace machine-specific provenance paths with portable placeholders."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".mjs", ".ps1", ".py", ".txt", ".yaml", ".yml"}
PATTERNS = [
    (re.compile(r"E:[\\/]2026-04-21_12-36-43\(桌面初始化\)[\\/]process[\\/]2026-04-21_12-36-43", re.I), "${DATASET_ROOT}"),
    (re.compile(r"E:[\\/]S1[\\/]S1-DEMO", re.I), "${DATASET_ROOT}"),
    (re.compile(r"D:[\\/]cloudstudio-windows-XL-0424[\\/]cloudstudio-windows", re.I), "${REPO_ROOT}"),
    (re.compile(r"C:[\\/]Users[\\/]69027", re.I), "${USER_HOME}"),
]


def main() -> None:
    changed = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        updated = text
        for pattern, replacement in PATTERNS:
            updated = pattern.sub(lambda _match, value=replacement: value, updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="")
            changed += 1
    print(f"Sanitized {changed} text files")


if __name__ == "__main__":
    main()
