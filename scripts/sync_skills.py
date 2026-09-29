#!/usr/bin/env python3
"""Validate the canonical repo skill and the legacy compatibility skill."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".agents" / "skills" / "reconstruct-indoor-scene" / "SKILL.md"
LEGACY = ROOT / ".codex" / "skills" / "reconstruct-indoor-scene" / "SKILL.md"
REQUIRED_CANONICAL_TOKENS = (
    "indoor-recon status", "indoor-recon next", "indoor-recon init",
    "indoor-recon mcp", "indoor-recon evaluate", "indoor-recon publish",
)


def _name(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^name:\s*([^\s]+)\s*$", text)
    return match.group(1) if match else None


def check() -> list[str]:
    issues: list[str] = []
    for path in (CANONICAL, LEGACY):
        if not path.is_file():
            issues.append(f"SKILL_MISSING:{path.relative_to(ROOT).as_posix()}")
    if issues:
        return issues
    if _name(CANONICAL) != "reconstruct-indoor-scene":
        issues.append("CANONICAL_SKILL_NAME_DRIFT")
    if _name(LEGACY) != _name(CANONICAL):
        issues.append("LEGACY_SKILL_IDENTITY_DRIFT")
    canonical_text = CANONICAL.read_text(encoding="utf-8")
    for token in REQUIRED_CANONICAL_TOKENS:
        if token not in canonical_text:
            issues.append(f"CANONICAL_SKILL_COMMAND_MISSING:{token}")
    if len(canonical_text.encode("utf-8")) >= 8 * 1024:
        issues.append("CANONICAL_SKILL_NOT_THIN")
    references = sorted((CANONICAL.parent / 'references').glob('*.md'))
    for document in [CANONICAL, *references, ROOT / 'docs/PORTABILITY.zh-CN.md',
                     ROOT / 'docs/室内建模准确性与快速交付SOP.zh-CN.md']:
        for target in re.findall(r'\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
            if '://' in target or target.startswith('#'):
                continue
            local = target.split('#', 1)[0]
            if not (document.parent / local).is_file():
                issues.append(f"SKILL_LINK_MISSING:{document.relative_to(ROOT).as_posix()}:{target}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    issues = check()
    if issues:
        print("Skill compatibility validation FAILED")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Skill compatibility validation PASS: .agents canonical, .codex legacy identity aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
