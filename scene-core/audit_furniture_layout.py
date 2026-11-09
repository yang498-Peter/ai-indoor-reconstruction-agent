#!/usr/bin/env python3
"""Report furniture and procedural-chair footprint collisions for Scene V2."""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
from pathlib import Path

from shapely.geometry import Polygon


def load_plan_renderer(repo_root: Path):
    renderer_path = (
        repo_root / ".codex" / "skills" / "reconstruct-indoor-scene"
        / "scripts" / "render_scene_plan.py"
    )
    spec = importlib.util.spec_from_file_location("indoor_scene_plan_renderer", renderer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load plan renderer: {renderer_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--minimum-area", type=float, default=1e-5)
    args = parser.parse_args()

    scene = json.loads(args.scene.read_text(encoding="utf-8"))
    # Compile candidates in memory for authoring QA only. This never mutates or
    # upgrades the evidence ledger.
    review_scene = json.loads(json.dumps(scene))
    for evidence in review_scene.get("evidence", {}).values():
        evidence["status"] = "accepted-inferred"
    renderer = load_plan_renderer(args.repo_root.resolve())
    compiled = renderer.compile_scene_v2_for_review(review_scene)

    footprints: list[tuple[str, Polygon]] = []
    for item in compiled.get("objects", []):
        center = [float(item["center"][0]), float(item["center"][2])]
        body = Polygon(renderer.box(center, float(item["size"][0]), float(item["size"][2]), float(item["yaw"])))
        footprints.append((item["id"], body))
        for index, chair in enumerate(renderer.object_chairs(item), 1):
            footprints.append((f"{item['id']}_chair{index}", Polygon(chair)))

    collisions: list[dict[str, object]] = []
    for (left_id, left), (right_id, right) in itertools.combinations(footprints, 2):
        if left_id.split("_chair", 1)[0] == right_id.split("_chair", 1)[0]:
            continue
        area = float(left.intersection(right).area)
        if area > args.minimum_area:
            collisions.append({"left": left_id, "right": right_id, "areaM2": round(area, 6)})
    print(json.dumps({
        "scene": str(args.scene.resolve()),
        "objectCount": len(compiled.get("objects", [])),
        "footprintCount": len(footprints),
        "collisionCount": len(collisions),
        "collisions": collisions,
    }, ensure_ascii=False, indent=2))
    return 1 if collisions else 0


if __name__ == "__main__":
    raise SystemExit(main())
