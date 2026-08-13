#!/usr/bin/env python3
"""Audit accepted solid-wall endpoint joints before runtime visual sign-off."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def accepted_wall(structure: dict[str, Any]) -> bool:
    return (
        structure.get("geometryType") == "segment"
        and structure.get("category") == "wall"
        and str(structure.get("decision", {}).get("status", "")).startswith("accepted")
        and len(structure.get("sourceStart", [])) >= 2
        and len(structure.get("sourceEnd", [])) >= 2
    )


def material_key(structure: dict[str, Any]) -> tuple[str, str]:
    material = structure.get("material", {})
    return (
        str(material.get("color", "")).strip().casefold(),
        str(material.get("description", "")).strip().casefold(),
    )


def distance(left: list[float], right: list[float]) -> float:
    return math.hypot(float(left[0]) - float(right[0]), float(left[1]) - float(right[1]))


def point_segment_distance(point: list[float], start: list[float], end: list[float]) -> float:
    segment_x = float(end[0]) - float(start[0])
    segment_y = float(end[1]) - float(start[1])
    denominator = segment_x * segment_x + segment_y * segment_y
    if denominator < 1e-12:
        return distance(point, start)
    parameter = max(
        0.0,
        min(
            1.0,
            ((float(point[0]) - float(start[0])) * segment_x + (float(point[1]) - float(start[1])) * segment_y) / denominator,
        ),
    )
    projection = [float(start[0]) + parameter * segment_x, float(start[1]) + parameter * segment_y]
    return distance(point, projection)


def vertical_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_base = float(left.get("baseHeight", 0.0))
    right_base = float(right.get("baseHeight", 0.0))
    return min(left_base + float(left.get("height", 0.0)), right_base + float(right.get("height", 0.0))) - max(left_base, right_base)


def audit(scene: dict[str, Any], scene_sha256: str, endpoint_tolerance: float, micro_gap_limit: float, visual_overlap: float) -> dict[str, Any]:
    walls = [item for item in scene.get("structures", []) if accepted_wall(item)]
    joints: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for left_index, left in enumerate(walls):
        for right in walls[left_index + 1 :]:
            if material_key(left) != material_key(right) or vertical_overlap(left, right) < 0.05:
                continue
            pair_has_joint = False
            pair_closest = math.inf
            for source, target in ((left, right), (right, left)):
                for source_end, source_point in enumerate((source["sourceStart"], source["sourceEnd"])):
                    separation = point_segment_distance(source_point, target["sourceStart"], target["sourceEnd"])
                    pair_closest = min(pair_closest, separation)
                    if separation > endpoint_tolerance:
                        continue
                    pair_has_joint = True
                    joints.append(
                        {
                            "sourceId": source["id"],
                            "targetId": target["id"],
                            "sourceEndpoint": source_end,
                            "centerlineSeparationM": round(separation, 6),
                            "requiredRenderOverlapM": round(visual_overlap, 4),
                            "requiredCloseup": True,
                        }
                    )
            if not pair_has_joint and pair_closest <= micro_gap_limit:
                issues.append(
                    {
                        "code": "WALL_JOINT_MICRO_GAP",
                        "leftId": left["id"],
                        "rightId": right["id"],
                        "centerlineSeparationM": round(pair_closest, 6),
                        "message": "same-material accepted wall endpoint is close to another wall segment but not construction-closed",
                    }
                )

    return {
        "schemaVersion": 1,
        "status": "PASS" if not issues else "FAIL",
        "sceneSha256": scene_sha256,
        "rule": "measurement centerlines stay unchanged; same-material accepted solids use a render-only end overlap and require a fresh oblique close-up",
        "endpointToleranceM": endpoint_tolerance,
        "microGapLimitM": micro_gap_limit,
        "visualOverlapM": visual_overlap,
        "acceptedSolidWallCount": len(walls),
        "jointCount": len(joints),
        "joints": joints,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint-tolerance", type=float, default=0.012)
    parser.add_argument("--micro-gap-limit", type=float, default=0.06)
    parser.add_argument("--visual-overlap", type=float, default=0.024)
    args = parser.parse_args()

    scene_bytes = args.scene.read_bytes()
    result = audit(
        json.loads(scene_bytes.decode("utf-8")),
        hashlib.sha256(scene_bytes).hexdigest(),
        args.endpoint_tolerance,
        args.micro_gap_limit,
        args.visual_overlap,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "jointCount": result["jointCount"], "issueCount": len(result["issues"])}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
