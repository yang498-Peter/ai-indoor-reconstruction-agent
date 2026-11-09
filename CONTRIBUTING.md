# Contributing

1. Create a feature branch: `codex/<short-description>`.
2. Keep source captures read-only and outside Git.
3. Never promote an algorithm suggestion directly to accepted authority geometry.
4. Add a focused red test before fixing a quality-gate regression.
5. Update the relevant Chinese change record with symptom, files, change, verification and current status.
6. Run `uv sync --locked --all-groups`, `python scripts/validate_portable_repo.py`,
   `python scripts/sync_skills.py --check`, the full pytest/Node suites, Ruff, Mypy,
   the quick benchmark and `uv build` before opening a PR.
7. Attach fresh evidence manifests when visual geometry changes; an old screenshot is not valid evidence for a new scene SHA.

All text files must remain UTF-8. Avoid broad formatting and unrelated rewrites.
The canonical agent skill lives under `.agents/skills/`; `.codex/skills/` remains a
migration compatibility layer. MCP transport and protocol behavior must use the pinned
official `mcp` SDK rather than repository-owned JSON-RPC framing.
