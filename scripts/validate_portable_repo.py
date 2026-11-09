#!/usr/bin/env python3
"""Fail closed when a portable reconstruction checkout contains raw data or machine paths."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
BANNED_SUFFIXES = {".las", ".laz", ".e57", ".ply", ".pcd", ".mcap", ".bag", ".bin"}
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".mjs", ".ps1", ".py", ".txt", ".yaml", ".yml"}
ABSOLUTE_PATH = re.compile(
    r"(?i)(?:[A-Z]:[\\/](?:Users|cloudstudio|2026-|S1|MVP)|file:///(?:home|Users)/|(?<![A-Za-z0-9_./-])/(?:home|Users)/)"
)
SECRET_TOKEN = re.compile(
    r"(?i)(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?<![A-Za-z])sk-[A-Za-z0-9_-]{16,})"
)
SECRET_ASSIGNMENT = re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][^'\"]+")


def repository_files() -> list[Path]:
    """Return files that Git would publish, excluding local ignored artifacts."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"GIT_LIST_FAILED:{message}")
    relative_paths = result.stdout.decode("utf-8").split("\0")
    return [ROOT / relative for relative in relative_paths if relative and (ROOT / relative).is_file()]


def main() -> int:
    issues: list[str] = []
    try:
        files = repository_files()
    except RuntimeError as error:
        print(f"Portable repository validation FAILED\n- {error}")
        return 1
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in BANNED_SUFFIXES:
            issues.append(f"RAW_DATA_TRACKED:{relative}")
        if path.stat().st_size > MAX_FILE_BYTES:
            issues.append(f"FILE_TOO_LARGE:{relative}:{path.stat().st_size}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(f"NOT_UTF8:{relative}")
            continue
        if path.name != Path(__file__).name and ("\ufffd" in text or re.search(r"ä¸|Â|Ã", text)):
            issues.append(f"MOJIBAKE:{relative}")
        if path.name != Path(__file__).name and ABSOLUTE_PATH.search(text):
            issues.append(f"ABSOLUTE_PATH:{relative}")
        if path.name != Path(__file__).name and (
            SECRET_TOKEN.search(text) or SECRET_ASSIGNMENT.search(text)
        ):
            issues.append(f"POSSIBLE_SECRET:{relative}")

    # Required files must all be COMMITTED artifacts: a fresh clone has no
    # generated/ output, so listing generated files here breaks first-run
    # validation on every new machine.
    required = [
        ROOT / ".agents/skills/reconstruct-indoor-scene/SKILL.md",
        ROOT / "AGENTS.md",
        ROOT / "pyproject.toml",
        ROOT / "templates/minimal-viewer/index.html",
        ROOT / "prototypes/litereality-three-redraw-20260812/viewer.html",
        ROOT / "scene-core/semantic-scene-v2.schema.json",
        ROOT / "scene-core/scene_api.py",
        ROOT / "scene-core/scene-core.js",
        ROOT / "scene-core/make_sample_scene.py",
        ROOT / "tests/scene-v2/test_scene_api.py",
        ROOT / "tests/scene-v2/scene-core.test.mjs",
    ]
    for path in required:
        if not path.is_file():
            issues.append(f"REQUIRED_FILE_MISSING:{path.relative_to(ROOT).as_posix()}")

    if issues:
        print("Portable repository validation FAILED")
        for issue in issues:
            print(f"- {issue}")
        return 1
    total_bytes = sum(path.stat().st_size for path in files)
    print(f"Portable repository validation PASS: {len(files)} files, {total_bytes / 1024 / 1024:.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
