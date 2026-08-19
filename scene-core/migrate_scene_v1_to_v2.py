#!/usr/bin/env python3
"""Migrate a V1 viewer scene.json into Semantic Scene V2.

V1 stored display-frame 3-vectors ([x, elevation, z], Y-up) in a flat mix of
walls/structures/structureCandidates/objects. V2 stores source plan meters
(x, y), Z-up, in a node graph with hosted openings and a separate evidence
ledger. Display inverse mapping: source = [x, -z].

Doors and windows are re-hosted onto the nearest parallel wall; anything that
cannot be hosted keeps its geometry as an explicit freeSegment node so nothing
is silently dropped. Legacy acceptance states migrate into the ledger with
    type 'inference-basis' sources and are demoted to candidates. Legacy actor
    strings, missing content hashes and missing run identities cannot mint a V2
    acceptance; every migrated claim must be re-evaluated explicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scene_api  # noqa: E402

HOST_PARALLEL_TOLERANCE_RAD = math.radians(10)


def display_to_source(point3: list[float]) -> list[float]:
    return [float(point3[0]), -float(point3[2])]


def slugify(raw_id: str, prefix: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "" for ch in raw_id.lower())
    return f"{prefix}_{cleaned or 'x'}"


def legacy_status(structure: dict) -> str:
    decision = structure.get("decision", {}).get("status", "")
    state = structure.get("evidence", {}).get("state", "")
    if decision == "rejected":
        return "rejected"
    if decision == "accepted" or state in {"accepted", "measured", "photo-confirmed", "rendered"}:
        return "accepted-inferred" if state == "inferred-completed" else "accepted-measured"
    if state == "inferred-completed":
        return "accepted-inferred"
    return "candidate"


def legacy_sources(structure: dict) -> list[dict]:
    evidence = structure.get("evidence", {})
    sources = [{
        "type": evidence.get("sourceLayer", "inference-basis"),
        "note": evidence.get("note", "migrated from V1 without file hashes"),
    }]
    reason = structure.get("decision", {}).get("reason")
    if reason:
        sources.append({"type": "inference-basis", "note": f"V1 decision reason: {reason}"})
    return sources


def ledger_entry(structure: dict) -> dict:
    status = legacy_status(structure)
    entry: dict = {
        "status": "candidate" if status.startswith("accepted") else status,
        "sources": legacy_sources(structure),
        "legacyDisposition": status,
    }
    if status.startswith("accepted"):
        entry["reason"] = "legacy acceptance requires PR-C identity, provenance and claim re-evaluation"
    if status == "rejected":
        entry["reason"] = structure.get("decision", {}).get("reason", "migrated V1 rejection")
    return entry


def try_host_opening(opening: dict, wall_nodes: list[dict]) -> tuple[dict, float] | None:
    start = display_to_source(opening["start"])
    end = display_to_source(opening["end"])
    center = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2]
    opening_dir = [end[0] - start[0], end[1] - start[1]]
    opening_len = math.hypot(*opening_dir)
    if opening_len < 1e-6:
        return None
    opening_angle = math.atan2(opening_dir[1], opening_dir[0])
    best = None
    for wall in wall_nodes:
        wx, wy = wall["end"][0] - wall["start"][0], wall["end"][1] - wall["start"][1]
        wall_len = math.hypot(wx, wy)
        if wall_len < 1e-6:
            continue
        wall_angle = math.atan2(wy, wx)
        delta = abs((opening_angle - wall_angle + math.pi / 2) % math.pi - math.pi / 2)
        if delta > HOST_PARALLEL_TOLERANCE_RAD:
            continue
        ux, uy = wx / wall_len, wy / wall_len
        relative = [center[0] - wall["start"][0], center[1] - wall["start"][1]]
        along = relative[0] * ux + relative[1] * uy
        lateral = abs(-relative[0] * uy + relative[1] * ux)
        if along < -0.05 or along > wall_len + 0.05:
            continue
        limit = wall.get("thickness", 0.12) / 2 + 0.15
        if lateral > limit:
            continue
        if best is None or lateral < best[1]:
            best = (wall, lateral, along)
    if best is None:
        return None
    return best[0], best[2]


def migrate(v1: dict, actor: str) -> dict:
    scene = scene_api.new_scene(v1.get("dataset", "migrated-v1-scene"), 3.05, 0.0, actor)
    level = scene_api.default_level_id(scene)
    if v1.get("levels"):
        first = v1["levels"][0]
        scene["nodes"][level]["height"] = float(first.get("height", 3.05))
        scene["nodes"][level]["name"] = first.get("name", "Level 1")
    report = {"walls": 0, "hostedOpenings": 0, "freeOpenings": 0, "columns": 0,
              "surfaces": 0, "items": 0, "candidates": 0, "skipped": []}

    everything = [(s, False) for s in v1.get("structures", [])] + [(s, True) for s in v1.get("structureCandidates", [])]
    wall_nodes: list[dict] = []
    openings: list[tuple[dict, bool]] = []

    for structure, is_candidate in everything:
        category = structure.get("category")
        geometry = structure.get("geometryType")
        if category in {"wall", "glass"} and geometry == "segment":
            node = {
                "id": slugify(structure["id"], "wall"), "type": "wall", "parentId": level,
                "wallKind": "glass" if category == "glass" else "solid",
                "start": display_to_source(structure["start"]),
                "end": display_to_source(structure["end"]),
                "height": float(structure.get("height", 3.05)),
                "thickness": float(structure.get("thickness", 0.045 if category == "glass" else 0.12)),
                "baseHeight": float(structure.get("baseHeight", 0.0)),
            }
            if structure.get("material"):
                node["material"] = structure["material"]
            scene_api.insert_node(scene, node, actor)
            scene["evidence"][node["id"]] = {"status": "candidate"} if is_candidate else ledger_entry(structure)
            wall_nodes.append(node)
            report["walls"] += 1
        elif category in {"door", "window"} and geometry == "segment":
            openings.append((structure, is_candidate))
        elif geometry == "rectangle" and category in {"column", "partition-unknown"}:
            node = {
                "id": slugify(structure["id"], "column"), "type": "column", "parentId": level,
                "center": display_to_source(structure["center"]),
                "size": list(structure.get("size", [0.4, 0.4])),
                "yaw": float(structure.get("yaw", 0.0)),
                "height": float(structure.get("height", 3.05)),
                "baseHeight": float(structure.get("baseHeight", 0.0)),
            }
            scene_api.insert_node(scene, node, actor)
            scene["evidence"][node["id"]] = {"status": "candidate"} if is_candidate else ledger_entry(structure)
            report["columns"] += 1
        elif geometry == "polygon" and category in {"floor-zone", "ceiling-zone"}:
            node_type = "slab" if category == "floor-zone" else "ceiling"
            node = {
                "id": slugify(structure["id"], node_type), "type": node_type, "parentId": level,
                "polygon": [display_to_source(p) for p in structure["points"]],
            }
            if node_type == "slab":
                node["thickness"] = float(structure.get("height", 0.05))
                node["elevation"] = float(structure.get("baseHeight", 0.0))
            else:
                node["elevation"] = float(structure.get("baseHeight") or structure.get("height", 2.95))
            if structure.get("material"):
                node["material"] = structure["material"]
            scene_api.insert_node(scene, node, actor)
            scene["evidence"][node["id"]] = {"status": "candidate"} if is_candidate else ledger_entry(structure)
            report["surfaces"] += 1
        else:
            report["skipped"].append(structure.get("id", "?"))

    for structure, is_candidate in openings:
        hosted = try_host_opening(structure, wall_nodes)
        start = display_to_source(structure["start"])
        end = display_to_source(structure["end"])
        width = math.hypot(end[0] - start[0], end[1] - start[1])
        node = {
            "id": slugify(structure["id"], structure["category"]),
            "type": structure["category"],
            "width": round(width, 4),
            "height": float(structure.get("height", 2.05 if structure["category"] == "door" else 1.5)),
            "sillHeight": float(structure.get("baseHeight", 0.0)),
        }
        if structure.get("material"):
            node["material"] = structure["material"]
        if hosted:
            wall, along = hosted
            node["parentId"] = wall["id"]
            node["hostOffsetM"] = round(along, 4)
            report["hostedOpenings"] += 1
        else:
            node["parentId"] = level
            node["freeSegment"] = {"start": start, "end": end, "thickness": float(structure.get("thickness", 0.06))}
            report["freeOpenings"] += 1
        scene_api.insert_node(scene, node, actor)
        scene["evidence"][node["id"]] = {"status": "candidate"} if is_candidate else ledger_entry(structure)

    for obj in v1.get("objects", []):
        node = {
            "id": slugify(obj["id"], "item"), "type": "item", "parentId": level,
            "category": obj.get("category", "generic"),
            "center": display_to_source(obj["center"]),
            "yaw": float(obj.get("yaw", 0.0)),
            "size": [float(v) for v in obj.get("size", [0.5, 0.5, 0.5])],
            "elevation": float(obj.get("center", [0, 0, 0])[1]),
            "color": obj.get("color", "#b9b4a8"),
        }
        if obj.get("layout"):
            node["layout"] = obj["layout"]
        if obj.get("confidence") is not None:
            node["confidence"] = float(obj["confidence"])
        scene_api.insert_node(scene, node, actor)
        passed = obj.get("deliveryValidation", {}).get("status") == "PASS"
        inferred = obj.get("furnitureValidation", {}).get("evidenceClass") == "accepted-inferred"
        if passed:
            scene["evidence"][node["id"]] = {
                "status": "candidate",
                "sources": [{"type": "inference-basis", "note": "migrated from V1 deliveryValidation"}],
                "legacyDisposition": "accepted-inferred" if inferred else "accepted-measured",
                "reason": "legacy furniture acceptance requires PR-C re-evaluation",
            }
        report["items"] += 1

    report["candidates"] = sum(1 for entry in scene["evidence"].values() if entry.get("status") == "candidate")

    for key in ("source", "photos", "cameraPath", "artifacts", "focusEnvelope", "derivedGeometry"):
        if key in v1:
            scene["meta"][key] = v1[key]
    if v1.get("pipeline"):
        scene["meta"]["pipeline"] = v1["pipeline"]
    if v1.get("qualityLoops"):
        scene["review"]["qualityLoops"] = v1["qualityLoops"]
    if v1.get("topologyReview"):
        topology = v1["topologyReview"]
        scene["review"]["topology"] = {
            "endpointToleranceM": topology.get("endpointToleranceM", 0.02),
            "spaces": [
                {"id": space["id"], "boundaryNodeIds": [slugify(e, "wall") for e in space.get("boundaryElementIds", [])]}
                for space in topology.get("spaces", [])
            ],
        }
    return scene, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="V1 scene.json")
    parser.add_argument("--output", required=True, type=Path, help="V2 scene.json destination")
    parser.add_argument("--actor", default="v1-migration")
    args = parser.parse_args()

    v1 = json.loads(args.input.read_text(encoding="utf-8"))
    if v1.get("schemaVersion") == "2.0":
        print(json.dumps({"ok": False, "error": "ALREADY_V2"}, ensure_ascii=False))
        return 1
    scene, report = migrate(v1, args.actor)
    scene_api.save_scene(args.output, scene, args.actor)
    print(json.dumps({"ok": True, "output": str(args.output), "report": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
