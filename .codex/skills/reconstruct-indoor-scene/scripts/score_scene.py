#!/usr/bin/env python3
"""Compatibility entrypoint for the archived Semantic Scene V1 scorer.

Semantic Scene V2 must use ``scene-core/quality_report_v2.py``. This wrapper
keeps old example verification commands discoverable without allowing a V2
scene to be silently evaluated against legacy fields.
"""

from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys


def _scene_argument(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--scene")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1])


def main() -> int:
    scene_path = _scene_argument(sys.argv[1:])
    if scene_path and scene_path.is_file():
        try:
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            scene = None
        if isinstance(scene, dict) and scene.get("schemaVersion") == "2.0":
            print(
                "V2_SCENE_USE_QUALITY_REPORT:run scene-core/quality_report_v2.py",
                file=sys.stderr,
            )
            return 2
    legacy = Path(__file__).with_name("legacy") / "score_scene_v1.py"
    if not legacy.is_file():
        print("LEGACY_SCORER_MISSING", file=sys.stderr)
        return 2
    runpy.run_path(str(legacy), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
