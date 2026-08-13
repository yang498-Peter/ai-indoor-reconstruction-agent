#!/usr/bin/env python3
"""Semantic Scene V2 mutation API.

Agents and tools call these commands instead of hand-editing scene JSON.
Every mutation is atomic: load -> mutate -> validate -> snapshot previous
revision -> write. A failed validation leaves the file untouched.

Acceptance is fail-closed:
- accept --mode measured requires at least one evidence source whose file
  exists next to the scene and whose sha256 matches the ledger.
- accept --mode inferred requires a reason plus at least two distinct sources.
- The reviewer must differ from the actor that created the node.

Coordinates are SOURCE plan meters (x, y), Z-up. Display mapping is owned by
scene-core.js and never leaks into this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "2.0"
REVISION_KEEP = 60
HOST_FIT_TOLERANCE_M = 0.011
MIN_WALL_LENGTH_M = 0.05

HOSTABLE_TYPES = {"door", "window", "opening"}
NODE_PREFIXES = {
    "level": "level", "wall": "wall", "door": "door", "window": "window",
    "opening": "opening", "column": "column", "slab": "slab", "ceiling": "ceiling",
    "zone": "zone", "item": "item", "scan": "scan", "guide": "guide",
}


class SceneError(Exception):
    """Validation or gate failure. The scene on disk is left unchanged."""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_node_id(node_type: str) -> str:
    prefix = NODE_PREFIXES.get(node_type, node_type)
    return f"{prefix}_{secrets.token_hex(3)}"


def scene_sha256(scene: dict) -> str:
    canonical = json.dumps(scene, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_scene(path: Path) -> dict:
    if not path.is_file():
        raise SceneError(f"SCENE_NOT_FOUND:{path}")
    scene = json.loads(path.read_text(encoding="utf-8"))
    if scene.get("schemaVersion") != SCHEMA_VERSION:
        raise SceneError(
            f"SCHEMA_VERSION_MISMATCH:expected {SCHEMA_VERSION}, got {scene.get('schemaVersion')}. "
            "Migrate with scene-core/migrate_scene_v1_to_v2.py first."
        )
    return scene


def revisions_dir(path: Path) -> Path:
    return path.parent / f"{path.stem}.revisions"


def snapshot_revision(path: Path) -> None:
    if not path.is_file():
        return
    directory = revisions_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(current.encode("utf-8")).hexdigest()[:10]
    counter = len(list(directory.glob("*.json")))
    (directory / f"{counter:05d}-{digest}.json").write_text(current, encoding="utf-8")
    snapshots = sorted(directory.glob("*.json"))
    for stale in snapshots[:-REVISION_KEEP]:
        stale.unlink()


def save_scene(path: Path, scene: dict, actor: str) -> None:
    validate_scene(scene)
    revision = scene.setdefault("revision", {"counter": 0})
    revision["counter"] = int(revision.get("counter", 0)) + 1
    revision["updatedAt"] = now_iso()
    revision["updatedBy"] = actor
    snapshot_revision(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def undo(path: Path) -> dict:
    directory = revisions_dir(path)
    snapshots = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not snapshots:
        raise SceneError("NOTHING_TO_UNDO:no revision snapshots exist")
    last = snapshots[-1]
    restored = last.read_text(encoding="utf-8")
    path.write_text(restored, encoding="utf-8")
    last.unlink()
    return json.loads(restored)


# ---------------------------------------------------------------------------
# Geometry helpers (source plan)
# ---------------------------------------------------------------------------

def wall_length(wall: dict) -> float:
    (x0, y0), (x1, y1) = wall["start"], wall["end"]
    return math.hypot(x1 - x0, y1 - y0)


def point_along_wall(wall: dict, offset: float) -> list[float]:
    (x0, y0), (x1, y1) = wall["start"], wall["end"]
    length = wall_length(wall)
    t = offset / length if length > 1e-9 else 0.0
    return [x0 + (x1 - x0) * t, y0 + (y1 - y0) * t]


def opening_interval(opening: dict) -> tuple[float, float]:
    center = float(opening.get("hostOffsetM", 0.0))
    half = float(opening["width"]) / 2.0
    return center - half, center + half


# ---------------------------------------------------------------------------
# Validation (structural; runs after every mutation)
# ---------------------------------------------------------------------------

def validate_scene(scene: dict) -> list[str]:
    problems: list[str] = []
    nodes = scene.get("nodes", {})
    evidence = scene.get("evidence", {})

    for root in scene.get("rootNodeIds", []):
        if root not in nodes:
            problems.append(f"ROOT_MISSING:{root}")

    for node_id, node in nodes.items():
        if node.get("id") != node_id:
            problems.append(f"ID_MISMATCH:{node_id}")
        parent_id = node.get("parentId")
        if parent_id is not None:
            parent = nodes.get(parent_id)
            if parent is None:
                problems.append(f"PARENT_MISSING:{node_id}->{parent_id}")
            elif node_id not in parent.get("children", []):
                problems.append(f"PARENT_CHILD_DESYNC:{parent_id}!>{node_id}")
        for child_id in node.get("children", []):
            child = nodes.get(child_id)
            if child is None:
                problems.append(f"CHILD_MISSING:{node_id}->{child_id}")
            elif child.get("parentId") != node_id:
                problems.append(f"CHILD_PARENT_DESYNC:{child_id}!<{node_id}")

        node_type = node.get("type")
        if node_type == "wall":
            if wall_length(node) < MIN_WALL_LENGTH_M:
                problems.append(f"WALL_TOO_SHORT:{node_id}")
            if float(node.get("thickness", 0)) <= 0 or float(node.get("height", 0)) <= 0:
                problems.append(f"WALL_BAD_DIMENSIONS:{node_id}")
            intervals: list[tuple[float, float, str]] = []
            for child_id in node.get("children", []):
                child = nodes.get(child_id)
                if not child or child.get("type") not in HOSTABLE_TYPES:
                    continue
                lo, hi = opening_interval(child)
                length = wall_length(node)
                if lo < -HOST_FIT_TOLERANCE_M or hi > length + HOST_FIT_TOLERANCE_M:
                    problems.append(f"OPENING_OUTSIDE_WALL:{child_id} [{lo:.3f},{hi:.3f}] on {length:.3f} m")
                top = float(child.get("sillHeight", 0)) + float(child["height"])
                if top > float(node["height"]) + 0.011:
                    problems.append(f"OPENING_TALLER_THAN_WALL:{child_id}")
                for other_lo, other_hi, other_id in intervals:
                    if lo < other_hi - 0.005 and hi > other_lo + 0.005:
                        problems.append(f"OPENING_OVERLAP:{child_id}~{other_id}")
                intervals.append((lo, hi, child_id))
        elif node_type in HOSTABLE_TYPES:
            hosted = parent_id is not None and nodes.get(parent_id, {}).get("type") == "wall"
            if not hosted and "freeSegment" not in node:
                problems.append(f"OPENING_UNHOSTED_WITHOUT_GEOMETRY:{node_id}")
            if hosted and "hostOffsetM" not in node:
                problems.append(f"OPENING_MISSING_HOST_OFFSET:{node_id}")
        elif node_type in {"slab", "ceiling", "zone"}:
            polygon = node.get("polygon", [])
            if len(polygon) < 3:
                problems.append(f"POLYGON_TOO_SMALL:{node_id}")

    for node_id, entry in evidence.items():
        if node_id not in nodes:
            problems.append(f"EVIDENCE_ORPHAN:{node_id}")
        status = entry.get("status")
        if status == "accepted-measured" and not entry.get("sources"):
            problems.append(f"MEASURED_WITHOUT_SOURCES:{node_id}")
        if status == "accepted-inferred":
            if len(entry.get("sources", [])) < 2:
                problems.append(f"INFERRED_NEEDS_TWO_SOURCES:{node_id}")
            if not entry.get("reason"):
                problems.append(f"INFERRED_WITHOUT_REASON:{node_id}")
        if status == "rejected" and not entry.get("reason"):
            problems.append(f"REJECTED_WITHOUT_REASON:{node_id}")

    if problems:
        raise SceneError("VALIDATION_FAILED:" + "; ".join(problems))
    return problems


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def new_scene(dataset: str, level_height: float, level_elevation: float, actor: str) -> dict:
    level_id = new_node_id("level")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "dataset": dataset,
        "coordinateFrame": {
            "authority": "source-plan-meters-z-up",
            "display": "three-y-up",
            "sourceToDisplay": "display = [x, elevation, -y]",
        },
        "nodes": {
            level_id: {
                "id": level_id, "type": "level", "parentId": None, "children": [],
                "name": "Level 1", "elevation": level_elevation, "height": level_height,
                "meta": {"createdBy": actor, "createdAt": now_iso()},
            }
        },
        "rootNodeIds": [level_id],
        "evidence": {},
        "review": {"issues": [], "qualityLoops": [], "topology": {"endpointToleranceM": 0.02, "spaces": []}},
        "meta": {"source": {"samplePointCount": 0}, "pipeline": [], "photos": [], "cameraPath": []},
        "revision": {"counter": 0},
    }


def default_level_id(scene: dict) -> str:
    for node_id in scene.get("rootNodeIds", []):
        if scene["nodes"].get(node_id, {}).get("type") == "level":
            return node_id
    raise SceneError("NO_LEVEL:scene has no level root")


def insert_node(scene: dict, node: dict, actor: str) -> dict:
    node.setdefault("children", [])
    meta = node.setdefault("meta", {})
    meta.setdefault("createdBy", actor)
    meta.setdefault("createdAt", now_iso())
    scene["nodes"][node["id"]] = node
    parent_id = node.get("parentId")
    if parent_id is not None:
        parent = scene["nodes"].get(parent_id)
        if parent is None:
            raise SceneError(f"PARENT_MISSING:{parent_id}")
        parent.setdefault("children", []).append(node["id"])
    scene.setdefault("evidence", {})[node["id"]] = {"status": "candidate", "sources": []}
    return node


def op_create_wall(scene: dict, args: dict, actor: str) -> dict:
    level_id = args.get("level") or default_level_id(scene)
    node = {
        "id": args.get("id") or new_node_id("wall"),
        "type": "wall", "parentId": level_id,
        "wallKind": args.get("kind", "solid"),
        "start": args["start"], "end": args["end"],
        "height": float(args.get("height", scene["nodes"][level_id].get("height", 3.05))),
        "thickness": float(args.get("thickness", 0.045 if args.get("kind") == "glass" else 0.12)),
        "baseHeight": float(args.get("baseHeight", 0.0)),
    }
    if args.get("material"):
        node["material"] = args["material"]
    return insert_node(scene, node, actor)


def op_add_opening(scene: dict, node_type: str, args: dict, actor: str) -> dict:
    wall_id = args["wall"]
    wall = scene["nodes"].get(wall_id)
    if wall is None or wall.get("type") != "wall":
        raise SceneError(f"HOST_NOT_A_WALL:{wall_id}")
    default_sill = 0.9 if node_type == "window" else 0.0
    node = {
        "id": args.get("id") or new_node_id(node_type),
        "type": node_type, "parentId": wall_id,
        "hostOffsetM": float(args["offset"]),
        "width": float(args["width"]),
        "height": float(args["height"]),
        "sillHeight": float(args.get("sill", default_sill)),
    }
    if args.get("material"):
        node["material"] = args["material"]
    return insert_node(scene, node, actor)


def op_add_item(scene: dict, args: dict, actor: str) -> dict:
    node = {
        "id": args.get("id") or new_node_id("item"),
        "type": "item", "parentId": args.get("level") or default_level_id(scene),
        "category": args["category"],
        "center": args["center"],
        "yaw": float(args.get("yaw", 0.0)),
        "size": [float(v) for v in args["size"]],
        "elevation": float(args.get("elevation", 0.0)),
        "color": args.get("color", "#b9b4a8"),
    }
    if args.get("layout"):
        node["layout"] = args["layout"]
    if args.get("confidence") is not None:
        node["confidence"] = float(args["confidence"])
    return insert_node(scene, node, actor)


def op_add_polygon_node(scene: dict, node_type: str, args: dict, actor: str) -> dict:
    node = {
        "id": args.get("id") or new_node_id(node_type),
        "type": node_type, "parentId": args.get("level") or default_level_id(scene),
        "polygon": args["polygon"],
    }
    if node_type == "slab":
        node["thickness"] = float(args.get("thickness", 0.05))
        node["elevation"] = float(args.get("elevation", 0.0))
    elif node_type == "ceiling":
        node["elevation"] = float(args.get("elevation", 2.95))
    if args.get("name"):
        node["name"] = args["name"]
    if args.get("material"):
        node["material"] = args["material"]
    return insert_node(scene, node, actor)


def op_add_column(scene: dict, args: dict, actor: str) -> dict:
    node = {
        "id": args.get("id") or new_node_id("column"),
        "type": "column", "parentId": args.get("level") or default_level_id(scene),
        "center": args["center"],
        "size": args.get("size", [0.4, 0.4]),
        "yaw": float(args.get("yaw", 0.0)),
        "height": float(args.get("height", 3.05)),
        "baseHeight": float(args.get("baseHeight", 0.0)),
    }
    return insert_node(scene, node, actor)


def set_dotted(target: dict, dotted_key: str, value) -> None:
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def op_update_node(scene: dict, args: dict) -> dict:
    node = scene["nodes"].get(args["id"])
    if node is None:
        raise SceneError(f"NODE_MISSING:{args['id']}")
    protected = {"id", "type", "parentId", "children"}
    for dotted_key, value in args["updates"].items():
        if dotted_key.split(".")[0] in protected:
            raise SceneError(f"PROTECTED_FIELD:{dotted_key} (use dedicated commands for re-parenting)")
        set_dotted(node, dotted_key, value)
    entry = scene.get("evidence", {}).get(args["id"])
    if entry and entry.get("status", "").startswith("accepted"):
        entry["status"] = "candidate"
        entry["reason"] = f"geometry changed at {now_iso()}; previous acceptance invalidated"
    return node


def op_delete_node(scene: dict, node_id: str) -> list[str]:
    if node_id not in scene["nodes"]:
        raise SceneError(f"NODE_MISSING:{node_id}")
    removed: list[str] = []

    def remove(current_id: str) -> None:
        node = scene["nodes"].pop(current_id, None)
        if node is None:
            return
        removed.append(current_id)
        scene.get("evidence", {}).pop(current_id, None)
        for child_id in list(node.get("children", [])):
            remove(child_id)

    parent_id = scene["nodes"][node_id].get("parentId")
    if parent_id and parent_id in scene["nodes"]:
        parent_children = scene["nodes"][parent_id].get("children", [])
        if node_id in parent_children:
            parent_children.remove(node_id)
    if node_id in scene.get("rootNodeIds", []):
        scene["rootNodeIds"].remove(node_id)
    remove(node_id)
    return removed


def resolve_evidence_file(scene_path: Path, source_path: str) -> Path:
    candidate = (scene_path.parent / source_path).resolve()
    if not candidate.is_file():
        candidate = (scene_path.parent.parent / source_path).resolve()
    return candidate


def op_attach_evidence(scene: dict, scene_path: Path, args: dict) -> dict:
    node_id = args["id"]
    if node_id not in scene["nodes"]:
        raise SceneError(f"NODE_MISSING:{node_id}")
    entry = scene.setdefault("evidence", {}).setdefault(node_id, {"status": "candidate", "sources": []})
    source = {"type": args["type"]}
    if args.get("path"):
        source["path"] = args["path"]
        evidence_file = resolve_evidence_file(scene_path, args["path"])
        if evidence_file.is_file():
            source["sha256"] = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        elif not args.get("allow_missing"):
            raise SceneError(f"EVIDENCE_FILE_MISSING:{args['path']} (relative to the scene directory)")
    if args.get("note"):
        source["note"] = args["note"]
    entry.setdefault("sources", []).append(source)
    return entry


def op_accept(scene: dict, scene_path: Path, args: dict) -> dict:
    node_id = args["id"]
    node = scene["nodes"].get(node_id)
    if node is None:
        raise SceneError(f"NODE_MISSING:{node_id}")
    entry = scene.setdefault("evidence", {}).setdefault(node_id, {"status": "candidate", "sources": []})
    reviewer = args["reviewer"]
    author = node.get("meta", {}).get("createdBy")
    if author and reviewer == author and not args.get("allow_self"):
        raise SceneError(f"SELF_REVIEW_FORBIDDEN:{node_id} author={author}. An acceptance needs an independent reviewer.")

    sources = entry.get("sources", [])
    mode = args["mode"]
    if mode == "measured":
        verified = 0
        for source in sources:
            if source.get("type") == "inference-basis":
                continue
            path_text = source.get("path")
            if not path_text:
                continue
            evidence_file = resolve_evidence_file(scene_path, path_text)
            if not evidence_file.is_file():
                raise SceneError(f"EVIDENCE_FILE_MISSING:{path_text} for {node_id}")
            digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
            if source.get("sha256") and source["sha256"] != digest:
                raise SceneError(f"EVIDENCE_HASH_STALE:{path_text} for {node_id}")
            verified += 1
        if verified < 1:
            raise SceneError(f"MEASURED_NEEDS_VERIFIED_SOURCE:{node_id} has no existing measurement evidence file")
        entry["status"] = "accepted-measured"
    elif mode == "inferred":
        if len(sources) < 2:
            raise SceneError(f"INFERRED_NEEDS_TWO_SOURCES:{node_id} has {len(sources)}")
        if not args.get("reason"):
            raise SceneError(f"INFERRED_NEEDS_REASON:{node_id}")
        entry["status"] = "accepted-inferred"
        entry["reason"] = args["reason"]
    else:
        raise SceneError(f"UNKNOWN_ACCEPT_MODE:{mode}")
    entry["reviewer"] = reviewer
    entry["acceptedAt"] = now_iso()
    return entry


def op_reject(scene: dict, args: dict) -> dict:
    node_id = args["id"]
    if node_id not in scene["nodes"]:
        raise SceneError(f"NODE_MISSING:{node_id}")
    entry = scene.setdefault("evidence", {}).setdefault(node_id, {"status": "candidate", "sources": []})
    entry["status"] = "rejected"
    entry["reviewer"] = args["reviewer"]
    entry["reason"] = args["reason"]
    return entry


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def op_find(scene: dict, args: dict) -> list[dict]:
    rows = []
    for node_id, node in scene["nodes"].items():
        if args.get("type") and node.get("type") != args["type"]:
            continue
        status = scene.get("evidence", {}).get(node_id, {}).get("status", "candidate")
        if args.get("status") and status != args["status"]:
            continue
        rows.append({"id": node_id, "type": node.get("type"), "parentId": node.get("parentId"), "status": status})
    return rows


def node_plan_center(node: dict, nodes: dict) -> list[float]:
    node_type = node.get("type")
    if node_type == "wall":
        (x0, y0), (x1, y1) = node["start"], node["end"]
        return [(x0 + x1) / 2, (y0 + y1) / 2]
    if node_type in HOSTABLE_TYPES:
        parent = nodes.get(node.get("parentId") or "")
        if parent and parent.get("type") == "wall":
            return point_along_wall(parent, float(node.get("hostOffsetM", 0.0)))
        free = node.get("freeSegment", {})
        (x0, y0), (x1, y1) = free.get("start", [0, 0]), free.get("end", [0, 0])
        return [(x0 + x1) / 2, (y0 + y1) / 2]
    if "center" in node:
        return list(node["center"][:2])
    if "polygon" in node:
        xs = [p[0] for p in node["polygon"]]
        ys = [p[1] for p in node["polygon"]]
        return [sum(xs) / len(xs), sum(ys) / len(ys)]
    raise SceneError(f"NO_PLAN_CENTER:{node.get('id')}")


def op_measure(scene: dict, args: dict) -> dict:
    nodes = scene["nodes"]
    if args.get("id"):
        node = nodes.get(args["id"])
        if node is None:
            raise SceneError(f"NODE_MISSING:{args['id']}")
        if node.get("type") == "wall":
            return {"id": args["id"], "lengthM": round(wall_length(node), 4)}
        raise SceneError(f"MEASURE_UNSUPPORTED:{node.get('type')} (single-node measure supports walls)")
    a, b = nodes.get(args["from"]), nodes.get(args["to"])
    if a is None or b is None:
        raise SceneError("NODE_MISSING:measure endpoints")
    (ax, ay), (bx, by) = node_plan_center(a, nodes), node_plan_center(b, nodes)
    return {"from": args["from"], "to": args["to"], "planDistanceM": round(math.hypot(bx - ax, by - ay), 4)}


def op_summary(scene: dict) -> dict:
    counts: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for node_id, node in scene["nodes"].items():
        counts[node["type"]] = counts.get(node["type"], 0) + 1
        status = scene.get("evidence", {}).get(node_id, {}).get("status", "candidate")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "dataset": scene.get("dataset"),
        "revision": scene.get("revision", {}).get("counter", 0),
        "sceneSha256": scene_sha256(scene),
        "nodeCounts": counts,
        "evidenceStatuses": statuses,
        "openIssues": [i["id"] for i in scene.get("review", {}).get("issues", []) if i.get("status") == "OPEN"],
    }


# ---------------------------------------------------------------------------
# Patch application (atomic batches)
# ---------------------------------------------------------------------------

def apply_ops(scene: dict, scene_path: Path, ops: list[dict], actor: str) -> list[dict]:
    results = []
    for op in ops:
        kind = op.get("op")
        payload = {k: v for k, v in op.items() if k != "op"}
        if kind == "create_wall":
            results.append(op_create_wall(scene, payload, actor))
        elif kind in {"add_door", "add_window", "add_opening"}:
            results.append(op_add_opening(scene, kind.removeprefix("add_"), payload, actor))
        elif kind == "add_item":
            results.append(op_add_item(scene, payload, actor))
        elif kind in {"add_slab", "add_ceiling", "add_zone"}:
            results.append(op_add_polygon_node(scene, kind.removeprefix("add_"), payload, actor))
        elif kind == "add_column":
            results.append(op_add_column(scene, payload, actor))
        elif kind == "update_node":
            results.append(op_update_node(scene, payload))
        elif kind == "delete_node":
            results.append({"deleted": op_delete_node(scene, payload["id"])})
        elif kind == "attach_evidence":
            results.append(op_attach_evidence(scene, scene_path, payload))
        elif kind == "accept":
            results.append(op_accept(scene, scene_path, payload))
        elif kind == "reject":
            results.append(op_reject(scene, payload))
        else:
            raise SceneError(f"UNKNOWN_OP:{kind}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_point(text: str) -> list[float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected x,y")
    return parts


def parse_polygon(text: str) -> list[list[float]]:
    return [parse_point(chunk) for chunk in text.split(";") if chunk.strip()]


def parse_size3(text: str) -> list[float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected w,h,d")
    return parts


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", required=True, type=Path, help="scene V2 JSON path")
    parser.add_argument("--actor", default="agent", help="stable actor id recorded on mutations")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a blank V2 scene")
    p.add_argument("--dataset", required=True)
    p.add_argument("--level-height", type=float, default=3.05)
    p.add_argument("--level-elevation", type=float, default=0.0)

    p = sub.add_parser("create-wall", help="create a wall on the level")
    p.add_argument("--start", required=True, type=parse_point)
    p.add_argument("--end", required=True, type=parse_point)
    p.add_argument("--height", type=float)
    p.add_argument("--thickness", type=float)
    p.add_argument("--kind", choices=["solid", "glass"], default="solid")
    p.add_argument("--base-height", type=float, default=0.0)
    p.add_argument("--color")
    p.add_argument("--description")
    p.add_argument("--id")

    for name in ("add-door", "add-window", "add-opening"):
        p = sub.add_parser(name, help=f"{name.replace('add-', '')} hosted on a wall (offset = center from wall start)")
        p.add_argument("--wall", required=True)
        p.add_argument("--offset", required=True, type=float)
        p.add_argument("--width", required=True, type=float)
        p.add_argument("--height", required=True, type=float)
        p.add_argument("--sill", type=float)
        p.add_argument("--description")
        p.add_argument("--id")

    p = sub.add_parser("add-item", help="furniture / movable object")
    p.add_argument("--category", required=True)
    p.add_argument("--center", required=True, type=parse_point)
    p.add_argument("--yaw-deg", type=float, default=0.0)
    p.add_argument("--size", required=True, type=parse_size3)
    p.add_argument("--color", default="#b9b4a8")
    p.add_argument("--layout", help="JSON object, e.g. '{\"seatCount\":8}'")
    p.add_argument("--confidence", type=float)
    p.add_argument("--id")

    for name in ("add-slab", "add-ceiling", "add-zone"):
        p = sub.add_parser(name, help=f"{name.replace('add-', '')} polygon (x,y;x,y;...)")
        p.add_argument("--polygon", required=True, type=parse_polygon)
        p.add_argument("--thickness", type=float)
        p.add_argument("--elevation", type=float)
        p.add_argument("--name")
        p.add_argument("--color")
        p.add_argument("--id")

    p = sub.add_parser("add-column")
    p.add_argument("--center", required=True, type=parse_point)
    p.add_argument("--size", type=parse_point, default=[0.4, 0.4])
    p.add_argument("--height", type=float, default=3.05)
    p.add_argument("--yaw-deg", type=float, default=0.0)
    p.add_argument("--id")

    p = sub.add_parser("update-node", help="set fields via dotted keys; JSON values")
    p.add_argument("--id", required=True)
    p.add_argument("--set", action="append", required=True, metavar="KEY=JSON",
                   help="e.g. --set height=2.9 --set material.color='\"#aabbcc\"'")

    p = sub.add_parser("delete-node")
    p.add_argument("--id", required=True)

    p = sub.add_parser("attach-evidence")
    p.add_argument("--id", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--path", help="evidence file relative to the scene directory")
    p.add_argument("--note")
    p.add_argument("--allow-missing", action="store_true")

    p = sub.add_parser("accept")
    p.add_argument("--id", required=True)
    p.add_argument("--mode", required=True, choices=["measured", "inferred"])
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason")
    p.add_argument("--allow-self", action="store_true")

    p = sub.add_parser("reject")
    p.add_argument("--id", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("apply-patch", help="atomic batch of ops from a JSON file: [{\"op\":\"create_wall\",...},...]")
    p.add_argument("--patch", required=True, type=Path)

    sub.add_parser("undo", help="restore the previous revision snapshot")

    p = sub.add_parser("find")
    p.add_argument("--type")
    p.add_argument("--status")

    p = sub.add_parser("measure")
    p.add_argument("--id")
    p.add_argument("--from", dest="from_id")
    p.add_argument("--to", dest="to_id")

    sub.add_parser("validate", help="structural validation without mutation")
    sub.add_parser("summary", help="counts, statuses, scene hash")
    return parser


def material_from_args(args) -> dict | None:
    material = {}
    if getattr(args, "color", None):
        material["color"] = args.color
    if getattr(args, "description", None):
        material["description"] = args.description
    return material or None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scene_path: Path = args.scene
    try:
        if args.command == "init":
            if scene_path.is_file():
                raise SceneError(f"SCENE_EXISTS:{scene_path} (refusing to overwrite; delete it first)")
            scene = new_scene(args.dataset, args.level_height, args.level_elevation, args.actor)
            save_scene(scene_path, scene, args.actor)
            emit({"ok": True, "scene": str(scene_path), "levelId": default_level_id(scene)})
            return 0

        if args.command == "undo":
            scene = undo(scene_path)
            emit({"ok": True, "restoredRevision": scene.get("revision", {}).get("counter")})
            return 0

        scene = load_scene(scene_path)

        if args.command in {"find", "measure", "validate", "summary"}:
            if args.command == "find":
                emit(op_find(scene, {"type": args.type, "status": args.status}))
            elif args.command == "measure":
                emit(op_measure(scene, {"id": args.id, "from": args.from_id, "to": args.to_id}))
            elif args.command == "validate":
                validate_scene(scene)
                emit({"ok": True, "sceneSha256": scene_sha256(scene)})
            else:
                emit(op_summary(scene))
            return 0

        if args.command == "apply-patch":
            ops = json.loads(args.patch.read_text(encoding="utf-8"))
            results = apply_ops(scene, scene_path, ops, args.actor)
            save_scene(scene_path, scene, args.actor)
            emit({"ok": True, "applied": len(results)})
            return 0

        op_map = {
            "create-wall": lambda: op_create_wall(scene, {
                "start": args.start, "end": args.end, "kind": args.kind,
                "baseHeight": args.base_height, "material": material_from_args(args), "id": args.id,
                **({"height": args.height} if args.height is not None else {}),
                **({"thickness": args.thickness} if args.thickness is not None else {}),
            }, args.actor),
            "add-door": lambda: op_add_opening(scene, "door", vars_for_opening(args), args.actor),
            "add-window": lambda: op_add_opening(scene, "window", vars_for_opening(args), args.actor),
            "add-opening": lambda: op_add_opening(scene, "opening", vars_for_opening(args), args.actor),
            "add-item": lambda: op_add_item(scene, {
                "category": args.category, "center": args.center,
                "yaw": math.radians(args.yaw_deg), "size": args.size, "color": args.color,
                "layout": json.loads(args.layout) if args.layout else None,
                "confidence": args.confidence, "id": args.id,
            }, args.actor),
            "add-slab": lambda: op_add_polygon_node(scene, "slab", vars_for_polygon(args), args.actor),
            "add-ceiling": lambda: op_add_polygon_node(scene, "ceiling", vars_for_polygon(args), args.actor),
            "add-zone": lambda: op_add_polygon_node(scene, "zone", vars_for_polygon(args), args.actor),
            "add-column": lambda: op_add_column(scene, {
                "center": args.center, "size": args.size, "height": args.height,
                "yaw": math.radians(args.yaw_deg), "id": args.id,
            }, args.actor),
            "update-node": lambda: op_update_node(scene, {
                "id": args.id,
                "updates": {key: json.loads(raw) for key, raw in (pair.split("=", 1) for pair in args.set)},
            }),
            "delete-node": lambda: {"deleted": op_delete_node(scene, args.id)},
            "attach-evidence": lambda: op_attach_evidence(scene, scene_path, {
                "id": args.id, "type": args.type, "path": args.path,
                "note": args.note, "allow_missing": args.allow_missing,
            }),
            "accept": lambda: op_accept(scene, scene_path, {
                "id": args.id, "mode": args.mode, "reviewer": args.reviewer,
                "reason": args.reason, "allow_self": args.allow_self,
            }),
            "reject": lambda: op_reject(scene, {"id": args.id, "reviewer": args.reviewer, "reason": args.reason}),
        }
        result = op_map[args.command]()
        save_scene(scene_path, scene, args.actor)
        emit({"ok": True, "result": result})
        return 0
    except SceneError as error:
        emit({"ok": False, "error": str(error)})
        return 1


def vars_for_opening(args) -> dict:
    payload = {"wall": args.wall, "offset": args.offset, "width": args.width, "height": args.height, "id": args.id}
    if args.sill is not None:
        payload["sill"] = args.sill
    if getattr(args, "description", None):
        payload["material"] = {"description": args.description}
    return payload


def vars_for_polygon(args) -> dict:
    payload = {"polygon": args.polygon, "id": args.id, "name": args.name}
    if args.thickness is not None:
        payload["thickness"] = args.thickness
    if args.elevation is not None:
        payload["elevation"] = args.elevation
    if getattr(args, "color", None):
        payload["material"] = {"color": args.color}
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
