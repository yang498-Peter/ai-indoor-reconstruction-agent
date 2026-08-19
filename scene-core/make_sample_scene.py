#!/usr/bin/env python3
"""Generate a fully synthetic Scene V2 sample through the mutation API.

The sample exists so a fresh clone can see the whole chain working - node
graph, hosted openings, miter/T joinery, evidence gates, candidate rendering -
without any customer capture. Every accepted element cites synthetic-sample
evidence receipts; nothing pretends to be a real measurement.

Usage:
  python scene-core/make_sample_scene.py --output prototypes/litereality-three-redraw-20260812/generated
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scene_api  # noqa: E402

AUTHOR = "sample-author"
REVIEWER = "sample-reviewer"


def execution(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "sample-generator", "model": "deterministic-local", "policyId": policy,
        "toolPolicyHash": scene_api.execution_identity_api.policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "sample-generator", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR_EXECUTION = execution(
    AUTHOR, "11111111-1111-4111-8111-111111111111", "author", "author-v1",
)
REVIEW_EXECUTION = execution(
    REVIEWER, "22222222-2222-4222-8222-222222222222",
    "reviewer", "reviewer-readonly-v1", "regional",
)


def write_receipts(output: Path) -> list[str]:
    evidence_dir = output / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    receipts = {
        "sample-plan-receipt.json": {
            "kind": "synthetic-sample",
            "note": "Deterministic sample plan; NOT a measurement of any real capture.",
            "planExtentM": [13.0, 8.0],
        },
        "sample-elevation-receipt.json": {
            "kind": "synthetic-sample",
            "note": "Deterministic sample elevations; NOT a measurement of any real capture.",
            "wallHeightM": 2.95,
        },
    }
    paths = []
    for name, payload in receipts.items():
        (evidence_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(f"evidence/{name}")
    return paths


def accept_measured(scene: dict, scene_path: Path, node_id: str, receipt_paths: list[str]) -> None:
    for index, receipt in enumerate(receipt_paths):
        scene_api.op_attach_evidence(scene, scene_path, {
            "id": node_id,
            "type": "synthetic-sample",
            "path": receipt,
            "producer": "sample-generator",
            "note": "sample receipt" if index == 0 else "sample cross-check",
        })
    scene_api.op_accept(scene, scene_path, {
        "id": node_id, "mode": "measured", "reviewerIdentity": REVIEW_EXECUTION,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="directory that receives scene.json and evidence/")
    args = parser.parse_args()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    scene_path = output / "scene.json"
    receipts = write_receipts(output)

    scene = scene_api.new_scene("synthetic-sample-office", 2.95, 0.0, AUTHOR, AUTHOR_EXECUTION)
    level = scene_api.default_level_id(scene)

    def wall(wall_id, start, end, thickness=0.12, kind="solid", color=None, description=None):
        material = {}
        if color:
            material["color"] = color
        if description:
            material["description"] = description
        return scene_api.op_create_wall(scene, {
            "id": wall_id, "start": start, "end": end, "height": 2.95,
            "thickness": thickness, "kind": kind, "level": level,
            "material": material or None,
            "execution": AUTHOR_EXECUTION,
        }, AUTHOR)["id"]

    # Exterior shell: shared corners -> mitered joints.
    wall("wall_south", [0, 0], [13, 0], 0.2, color="#d8d6ce", description="painted exterior wall")
    wall("wall_east", [13, 0], [13, 8], 0.2, color="#d8d6ce", description="painted exterior wall")
    wall("wall_north", [13, 8], [0, 8], 0.2, color="#d8d6ce", description="painted exterior wall")
    wall("wall_west", [0, 8], [0, 0], 0.2, color="#d8d6ce", description="painted exterior wall")
    # Interior partition: both ends T-embed into the shell.
    wall("wall_office", [8.5, 0], [8.5, 8], 0.12, color="#c7c0b6", description="warm-gray partition")
    # Glass meeting-room front between the partition and the east shell.
    wall("wall_meeting_glass", [8.5, 4.6], [13, 4.6], 0.045, kind="glass", description="glass partition with roller blind")

    def opening(kind, node_id, host, offset, width, height, sill=None, description=None):
        payload = {
            "id": node_id, "wall": host, "offset": offset, "width": width,
            "height": height, "execution": AUTHOR_EXECUTION,
        }
        if sill is not None:
            payload["sill"] = sill
        if description:
            payload["material"] = {"description": description}
        return scene_api.op_add_opening(scene, kind, payload, AUTHOR)["id"]

    opening("window", "window_south_a", "wall_south", 3.0, 1.8, 1.5, sill=0.9)
    opening("window", "window_south_b", "wall_south", 10.5, 1.8, 1.5, sill=0.9)
    opening("window", "window_north", "wall_north", 6.5, 2.4, 1.6, sill=0.85)
    opening("door", "door_office", "wall_office", 2.0, 0.95, 2.05, sill=0.0, description="oak office door")
    opening("door", "door_meeting", "wall_meeting_glass", 1.2, 0.95, 2.05, sill=0.0, description="tempered glass door")

    footprint = [[0, 0], [13, 0], [13, 8], [0, 8]]
    scene_api.op_add_polygon_node(scene, "slab", {
        "id": "slab_floor", "polygon": footprint, "thickness": 0.06, "elevation": 0.0,
        "level": level, "material": {"color": "#5b6260"},
        "execution": AUTHOR_EXECUTION,
    }, AUTHOR)
    scene_api.op_add_polygon_node(scene, "ceiling", {
        "id": "ceiling_main", "polygon": footprint, "elevation": 2.88,
        "level": level, "material": {"color": "#e9e6dc", "opacity": 0.9},
        "execution": AUTHOR_EXECUTION,
    }, AUTHOR)
    scene_api.op_add_polygon_node(scene, "zone", {
        "id": "zone_meeting", "polygon": [[8.56, 4.66], [12.9, 4.66], [12.9, 7.9], [8.56, 7.9]],
        "name": "Meeting room", "level": level, "execution": AUTHOR_EXECUTION,
    }, AUTHOR)
    scene_api.op_add_polygon_node(scene, "zone", {
        "id": "zone_open_office", "polygon": [[0.1, 0.1], [8.44, 0.1], [8.44, 7.9], [0.1, 7.9]],
        "name": "Open office", "level": level, "execution": AUTHOR_EXECUTION,
    }, AUTHOR)

    def item(item_id, category, center, size, yaw_deg=0.0, color="#b9b4a8", layout=None, confidence=0.9):
        return scene_api.op_add_item(scene, {
            "id": item_id, "category": category, "center": center, "size": size,
            "yaw": math.radians(yaw_deg), "color": color, "layout": layout,
            "confidence": confidence, "level": level,
            "execution": AUTHOR_EXECUTION,
        }, AUTHOR)["id"]

    item("item_meeting_table", "meeting-table", [10.75, 6.3], [2.6, 0.75, 1.2], 0, "#8a6f4d", {"seatCount": 8})
    item("item_meeting_cabinet", "cabinet", [12.62, 6.2], [1.6, 1.9, 0.45], 90, "#7f8a84")
    item("item_ws_row_a", "workstation", [3.2, 5.2], [4.2, 0.74, 1.4], 0, "#c9b18c",
         {"seatsPerSide": 3, "monitorSlots": ["0:-1", "1:1", "2:-1"]})
    item("item_ws_row_b", "workstation", [3.2, 2.4], [4.2, 0.74, 1.4], 0, "#c9b18c",
         {"seatsPerSide": 3, "monitorSlots": ["0:1", "2:1"]})
    item("item_sofa_west", "sofa", [0.95, 6.6], [2.0, 0.8, 0.9], 90, "#6b7a83")
    item("item_round_table", "round-table", [6.5, 1.5], [0.9, 0.74, 0.9], 0, "#a08c6a", {"seatCount": 4})
    item("item_office_desk", "table", [10.6, 2.3], [1.8, 0.75, 0.9], 0, "#9c7f5c")
    item("item_office_cabinet", "cabinet", [12.62, 1.1], [1.4, 1.9, 0.45], 90, "#7f8a84")

    scene["review"]["topology"] = {
        "endpointToleranceM": 0.02,
        "spaces": [
            {"id": "zone_meeting", "boundaryNodeIds": ["wall_office", "wall_meeting_glass", "wall_east", "wall_north"]},
            {"id": "zone_open_office", "boundaryNodeIds": ["wall_south", "wall_office", "wall_north", "wall_west"]},
        ],
    }

    # Accept everything above with synthetic receipts.
    for node_id in list(scene["nodes"]):
        if scene["nodes"][node_id]["type"] == "level":
            continue
        accept_measured(scene, scene_path, node_id, receipts)

    # Deliberate leftovers: one candidate wall and one candidate chair show the
    # unresolved-candidate rendering and keep the seed stage honest.
    scene_api.op_create_wall(scene, {
        "id": "wall_candidate_west", "start": [0, 4.2], "end": [2.2, 4.2],
        "height": 2.95, "thickness": 0.12, "level": level,
        "material": {"description": "low return detected in slice; awaiting review"},
        "execution": AUTHOR_EXECUTION,
    }, AUTHOR)
    scene_api.op_add_item(scene, {
        "id": "item_candidate_chair", "category": "chair", "center": [6.0, 6.5],
        "size": [0.58, 1.02, 0.58], "yaw": 0.0, "color": "#aab0b0", "level": level,
        "execution": AUTHOR_EXECUTION,
    }, AUTHOR)

    scene["review"]["qualityLoops"] = [
        {"iteration": 1, "name": "合成样例几何自检", "status": "PASS", "remainingCount": 2},
    ]
    scene["meta"]["source"] = {"samplePointCount": 0, "kind": "synthetic-sample"}

    scene_api.save_scene(scene_path, scene, AUTHOR)
    summary = scene_api.op_summary(scene)
    print(json.dumps({"ok": True, "scene": str(scene_path), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
