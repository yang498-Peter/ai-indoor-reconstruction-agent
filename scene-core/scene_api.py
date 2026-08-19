#!/usr/bin/env python3
"""Semantic Scene V2 mutation API.

Agents and tools call these commands instead of hand-editing scene JSON.
Every mutation is atomic: load -> mutate -> validate -> snapshot previous
revision -> write. A failed validation leaves the file untouched.

Acceptance is fail-closed:
- accept --mode measured requires at least one evidence source whose file
  exists next to the scene and whose sha256 matches the ledger.
- accept --mode inferred requires a reason plus at least two verified files
  with distinct content hashes and root lineages.
- Acceptance requires an independent read-only reviewer execution and stores a
  claimHash that becomes stale when geometry, topology or a host changes.

Coordinates are SOURCE plan meters (x, y), Z-up. Display mapping is owned by
scene-core.js and never leaks into this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import secrets
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import execution_identity as execution_identity_api  # noqa: E402

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


def _canonical_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def geometry_digest(scene: dict) -> str:
    """Hash stable authority geometry without review, presentation, or revisions."""
    return _canonical_digest(
        {
            "schemaVersion": scene.get("schemaVersion"),
            "dataset": scene.get("dataset"),
            "coordinateFrame": scene.get("coordinateFrame"),
            "nodes": {
                node_id: _claim_node_payload(node)
                for node_id, node in scene.get("nodes", {}).items()
            },
            "rootNodeIds": scene.get("rootNodeIds", []),
            "topology": scene.get("review", {}).get("topology", {}),
        }
    )


def evidence_set_digest(scene: dict) -> str:
    """Hash the authority evidence ledger independently from geometry bytes."""
    return _canonical_digest(scene.get("evidence", {}))


def _identity(value: object) -> dict:
    try:
        return execution_identity_api.normalize_identity(value)
    except execution_identity_api.IdentityError as error:
        raise SceneError(str(error)) from error


def _require_operation(value: object, operation: str, roles: set[str] | None = None) -> dict:
    try:
        return execution_identity_api.require_operation(value, operation, roles)
    except execution_identity_api.IdentityError as error:
        raise SceneError(str(error)) from error


def _register_execution(scene: dict, identity: object) -> dict:
    normalized = _identity(identity)
    executions = scene.setdefault("meta", {}).setdefault("executions", {})
    existing = executions.get(normalized["runId"])
    if existing is not None and execution_identity_api.identity_digest(existing) != execution_identity_api.identity_digest(normalized):
        raise SceneError(f"EXECUTION_RUN_ROLE_CONFLICT:{normalized['runId']}")
    executions[normalized["runId"]] = normalized
    return normalized


def _author_identity(scene: dict, node: dict) -> dict:
    run_id = node.get("meta", {}).get("createdRunId")
    identity = scene.get("meta", {}).get("executions", {}).get(run_id)
    if not run_id or not isinstance(identity, dict):
        raise SceneError(f"AUTHOR_EXECUTION_IDENTITY_REQUIRED:{node.get('id')}")
    return _identity(identity)


def _claim_node_payload(node: dict) -> dict:
    return {key: value for key, value in node.items() if key != "meta"}


def claim_payload(scene: dict, node_id: str) -> dict:
    nodes = scene.get("nodes", {})
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise SceneError(f"NODE_MISSING:{node_id}")
    dependencies: list[dict] = []
    parent_id = node.get("parentId")
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = nodes.get(parent_id)
        if not isinstance(parent, dict):
            break
        parent_payload = _claim_node_payload(parent)
        parent_payload = {key: value for key, value in parent_payload.items() if key != "children"}
        dependencies.append({"nodeId": parent_id, "claim": parent_payload})
        parent_id = parent.get("parentId")
    hosted_children = []
    if node.get("type") == "wall":
        for child_id in sorted(node.get("children", [])):
            child = nodes.get(child_id)
            if isinstance(child, dict) and child.get("type") in HOSTABLE_TYPES:
                hosted_children.append({"nodeId": child_id, "claim": _claim_node_payload(child)})
    spaces = [
        space
        for space in scene.get("review", {}).get("topology", {}).get("spaces", [])
        if node_id in space.get("boundaryNodeIds", [])
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "coordinateFrame": scene.get("coordinateFrame"),
        "nodeId": node_id,
        "nodeClaim": _claim_node_payload(node),
        "dependencyClaims": dependencies,
        "hostedChildrenClaims": hosted_children,
        "topologyClaims": spaces,
    }


def claim_hash(scene: dict, node_id: str) -> str:
    return _canonical_digest(claim_payload(scene, node_id))


def _accepted_source_digest(sources: list[dict]) -> str:
    normalized = [
        {
            "sourceRole": source.get("sourceRole") or source.get("type"),
            "contentSha256": source.get("contentSha256"),
            "lineageId": source.get("lineageId"),
            "rootContentSha256s": sorted(source.get("rootContentSha256s", [])),
            "producer": source.get("producer"),
        }
        for source in sources
    ]
    return _canonical_digest(normalized)


def has_two_independent_sources(sources: list[dict]) -> bool:
    verified = [
        source
        for source in sources
        if source.get("contentSha256")
        and source.get("lineageId")
        and source.get("rootContentSha256s")
    ]
    for index, first in enumerate(verified):
        first_roots = set(first["rootContentSha256s"])
        for second in verified[index + 1:]:
            if (
                first.get("contentSha256") != second.get("contentSha256")
                and first.get("lineageId") != second.get("lineageId")
                and first_roots.isdisjoint(set(second["rootContentSha256s"]))
            ):
                return True
    return False


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
            intervals: list[tuple[float, float, float, float, str]] = []
            for child_id in node.get("children", []):
                child = nodes.get(child_id)
                if not child or child.get("type") not in HOSTABLE_TYPES:
                    continue
                lo, hi = opening_interval(child)
                length = wall_length(node)
                if lo < -HOST_FIT_TOLERANCE_M or hi > length + HOST_FIT_TOLERANCE_M:
                    problems.append(f"OPENING_OUTSIDE_WALL:{child_id} [{lo:.3f},{hi:.3f}] on {length:.3f} m")
                sill = float(child.get("sillHeight", 0))
                top = sill + float(child["height"])
                if top > float(node["height"]) + 0.011:
                    problems.append(f"OPENING_TALLER_THAN_WALL:{child_id}")
                # A clash needs BOTH plan and vertical overlap: a transom over
                # a door or a hatch under a window is legitimate construction.
                for other_lo, other_hi, other_sill, other_top, other_id in intervals:
                    plan_overlap = lo < other_hi - 0.005 and hi > other_lo + 0.005
                    vertical_overlap = sill < other_top - 0.005 and top > other_sill + 0.005
                    if plan_overlap and vertical_overlap:
                        problems.append(f"OPENING_OVERLAP:{child_id}~{other_id}")
                intervals.append((lo, hi, sill, top, child_id))
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
        if status in {"accepted-measured", "accepted-inferred"}:
            sources = entry.get("sources", [])
            current_claim = claim_payload(scene, node_id)
            if entry.get("claimSnapshot") != current_claim:
                problems.append(f"EVIDENCE_CLAIM_SNAPSHOT_STALE:{node_id}")
            if entry.get("claimHash") != _canonical_digest(current_claim):
                problems.append(f"EVIDENCE_CLAIM_STALE:{node_id}")
            if entry.get("acceptedSourceDigest") != _accepted_source_digest(sources):
                problems.append(f"ACCEPTED_SOURCE_DIGEST_STALE:{node_id}")
            try:
                _identity(entry.get("reviewer"))
            except SceneError:
                problems.append(f"REVIEW_IDENTITY_INVALID:{node_id}")
        if status == "accepted-measured" and not entry.get("sources"):
            problems.append(f"MEASURED_WITHOUT_SOURCES:{node_id}")
        if status == "accepted-inferred":
            lineages = {
                source.get("lineageId")
                for source in entry.get("sources", [])
                if source.get("contentSha256")
            }
            contents = {
                source.get("contentSha256")
                for source in entry.get("sources", [])
                if source.get("contentSha256")
            }
            if len(lineages) < 2 or len(contents) < 2 or not has_two_independent_sources(entry.get("sources", [])):
                problems.append(f"INFERRED_NEEDS_TWO_DISTINCT_SOURCES:{node_id}")
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

def new_scene(
    dataset: str,
    level_height: float,
    level_elevation: float,
    actor: str,
    execution: dict | None = None,
) -> dict:
    level_id = new_node_id("level")
    scene = {
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
        "meta": {
            "source": {"samplePointCount": 0}, "pipeline": [], "photos": [],
            "cameraPath": [], "executions": {},
        },
        "revision": {"counter": 0},
    }
    if execution is not None:
        normalized = _register_execution(scene, execution)
        if normalized["actorId"] != strict_actor_key(actor):
            raise SceneError("EXECUTION_ACTOR_MISMATCH:init")
        scene["nodes"][level_id]["meta"].update(
            {
                "createdRunId": normalized["runId"],
                "createdIdentityDigest": execution_identity_api.identity_digest(normalized),
            }
        )
    return scene


def default_level_id(scene: dict) -> str:
    for node_id in scene.get("rootNodeIds", []):
        if scene["nodes"].get(node_id, {}).get("type") == "level":
            return node_id
    raise SceneError("NO_LEVEL:scene has no level root")


def _dependent_node_ids(scene: dict, changed_ids: set[str]) -> set[str]:
    nodes = scene.get("nodes", {})
    affected = set(changed_ids)
    queue = list(changed_ids)
    while queue:
        current_id = queue.pop()
        node = nodes.get(current_id, {})
        for child_id in node.get("children", []):
            if child_id not in affected:
                affected.add(child_id)
                queue.append(child_id)
        if node.get("type") in HOSTABLE_TYPES:
            parent_id = node.get("parentId")
            if parent_id and parent_id not in affected:
                affected.add(parent_id)
    return affected


def _invalidate_nodes(
    scene: dict,
    changed_ids: set[str],
    reason: str,
    *,
    cascade: bool = True,
) -> list[str]:
    invalidated = []
    affected = _dependent_node_ids(scene, changed_ids) if cascade else changed_ids
    for node_id in sorted(affected):
        entry = scene.get("evidence", {}).get(node_id)
        if not isinstance(entry, dict) or entry.get("status") == "candidate":
            continue
        entry["status"] = "candidate"
        entry["reason"] = f"{reason} at {now_iso()}; previous disposition invalidated"
        entry.pop("claimHash", None)
        entry.pop("claimSnapshot", None)
        entry.pop("acceptedSourceDigest", None)
        entry.pop("reviewer", None)
        entry.pop("acceptedAt", None)
        invalidated.append(node_id)
    return invalidated


def insert_node(scene: dict, node: dict, actor: str, execution: dict | None = None) -> dict:
    node.setdefault("children", [])
    meta = node.setdefault("meta", {})
    meta.setdefault("createdBy", actor)
    meta.setdefault("createdAt", now_iso())
    if execution is not None:
        normalized = _require_operation(execution, "scene:mutate", {"author"})
        if normalized["actorId"] != strict_actor_key(actor):
            raise SceneError("EXECUTION_ACTOR_MISMATCH:insert-node")
        _register_execution(scene, normalized)
        meta["createdRunId"] = normalized["runId"]
        meta["createdIdentityDigest"] = execution_identity_api.identity_digest(normalized)
    scene["nodes"][node["id"]] = node
    parent_id = node.get("parentId")
    if parent_id is not None:
        parent = scene["nodes"].get(parent_id)
        if parent is None:
            raise SceneError(f"PARENT_MISSING:{parent_id}")
        parent.setdefault("children", []).append(node["id"])
        _invalidate_nodes(
            scene,
            {parent_id},
            "child geometry changed",
            cascade=parent.get("type") == "wall",
        )
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
    return insert_node(scene, node, actor, args.get("execution"))


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
    return insert_node(scene, node, actor, args.get("execution"))


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
    return insert_node(scene, node, actor, args.get("execution"))


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
    return insert_node(scene, node, actor, args.get("execution"))


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
    return insert_node(scene, node, actor, args.get("execution"))


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
    protected_meta = {"meta.createdBy", "meta.createdAt", "meta.createdRunId", "meta.createdIdentityDigest"}
    for dotted_key, value in args["updates"].items():
        if dotted_key.split(".")[0] in protected or dotted_key in protected_meta:
            raise SceneError(f"PROTECTED_FIELD:{dotted_key} (use dedicated commands for re-parenting)")
        set_dotted(node, dotted_key, value)
    if any(not dotted_key.startswith("meta.") for dotted_key in args["updates"]):
        _invalidate_nodes(scene, {args["id"]}, "authority claim changed")
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
        _invalidate_nodes(
            scene,
            {parent_id},
            "child geometry deleted",
            cascade=scene["nodes"][parent_id].get("type") == "wall",
        )
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
    source_role = str(args.get("sourceRole") or args["type"])
    source = {"type": args["type"], "sourceRole": source_role}
    if args.get("path"):
        source["path"] = args["path"]
        evidence_file = resolve_evidence_file(scene_path, args["path"])
        if evidence_file.is_file():
            content_digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
            source["sha256"] = content_digest
            source["contentSha256"] = content_digest
            receipt_path = args.get("provenanceReceipt")
            if receipt_path:
                receipt_file = resolve_evidence_file(scene_path, receipt_path)
                if not receipt_file.is_file():
                    raise SceneError(f"PROVENANCE_RECEIPT_MISSING:{receipt_path}")
                try:
                    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise SceneError(f"PROVENANCE_RECEIPT_INVALID:{receipt_path}") from error
                roots = receipt.get("rootContentSha256s")
                if (
                    receipt.get("outputContentSha256") != content_digest
                    or not isinstance(roots, list)
                    or not roots
                    or any(re.fullmatch(r"[0-9a-f]{64}", str(value)) is None for value in roots)
                ):
                    raise SceneError(f"PROVENANCE_RECEIPT_INVALID:{receipt_path}")
                source["rootContentSha256s"] = sorted(set(roots))
                source["provenanceReceipt"] = {
                    "path": receipt_path,
                    "sha256": hashlib.sha256(receipt_file.read_bytes()).hexdigest(),
                }
                source["producer"] = receipt.get("producer") or args.get("producer")
            else:
                source["rootContentSha256s"] = [content_digest]
                source["producer"] = args.get("producer")
            if not source.get("producer"):
                raise SceneError(f"EVIDENCE_PRODUCER_REQUIRED:{args['path']}")
            source["lineageId"] = _canonical_digest(
                {"rootContentSha256s": source["rootContentSha256s"]}
            )
            supplied_lineage = args.get("lineageId")
            if supplied_lineage and supplied_lineage != source["lineageId"]:
                raise SceneError("EVIDENCE_LINEAGE_CALLER_OVERRIDE_FORBIDDEN")
        elif not args.get("allow_missing"):
            raise SceneError(f"EVIDENCE_FILE_MISSING:{args['path']} (relative to the scene directory)")
    elif args.get("allow_missing") and args.get("type") != "inference-basis":
        raise SceneError("MISSING_EVIDENCE_ONLY_ALLOWED_FOR_INFERENCE_BASIS")
    if args.get("note"):
        source["note"] = args["note"]
    if entry.get("status") != "candidate":
        _invalidate_nodes(scene, {node_id}, "evidence set changed")
    entry.setdefault("sources", []).append(source)
    return entry


def op_accept(scene: dict, scene_path: Path, args: dict) -> dict:
    node_id = args["id"]
    node = scene["nodes"].get(node_id)
    if node is None:
        raise SceneError(f"NODE_MISSING:{node_id}")
    entry = scene.setdefault("evidence", {}).setdefault(node_id, {"status": "candidate", "sources": []})
    author_identity = _author_identity(scene, node)
    sources = entry.get("sources", [])
    try:
        reviewer = execution_identity_api.require_independent_reviewer(
            author_identity,
            args.get("reviewerIdentity"),
            severity=args.get("severity"),
            required_input_digests={claim_hash(scene, node_id), _accepted_source_digest(sources)},
        )
    except execution_identity_api.IdentityError as error:
        raise SceneError(str(error)) from error
    _register_execution(scene, reviewer)

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
            if source.get("contentSha256") != digest or source.get("sha256") != digest:
                raise SceneError(f"EVIDENCE_HASH_STALE:{path_text} for {node_id}")
            if not source.get("lineageId") or not source.get("rootContentSha256s") or not source.get("producer"):
                raise SceneError(f"EVIDENCE_PROVENANCE_INCOMPLETE:{path_text} for {node_id}")
            verified += 1
        if verified < 1:
            raise SceneError(f"MEASURED_NEEDS_VERIFIED_SOURCE:{node_id} has no existing measurement evidence file")
        entry["status"] = "accepted-measured"
    elif mode == "inferred":
        verified_sources = []
        for source in sources:
            path_text = source.get("path")
            if not path_text:
                continue
            evidence_file = resolve_evidence_file(scene_path, path_text)
            if not evidence_file.is_file():
                raise SceneError(f"EVIDENCE_FILE_MISSING:{path_text} for {node_id}")
            digest = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
            if source.get("contentSha256") != digest:
                raise SceneError(f"EVIDENCE_HASH_STALE:{path_text} for {node_id}")
            verified_sources.append(source)
        lineages = {source.get("lineageId") for source in verified_sources}
        contents = {source.get("contentSha256") for source in verified_sources}
        if len(lineages) < 2 or len(contents) < 2 or not has_two_independent_sources(verified_sources):
            raise SceneError(
                f"INFERRED_NEEDS_TWO_DISTINCT_SOURCES:{node_id} has "
                f"{len(lineages)} lineages/{len(contents)} contents"
            )
        if not args.get("reason"):
            raise SceneError(f"INFERRED_NEEDS_REASON:{node_id}")
        entry["status"] = "accepted-inferred"
        entry["reason"] = args["reason"]
    else:
        raise SceneError(f"UNKNOWN_ACCEPT_MODE:{mode}")
    entry["reviewer"] = reviewer
    entry["acceptedAt"] = now_iso()
    entry["claimSnapshot"] = claim_payload(scene, node_id)
    entry["claimHash"] = _canonical_digest(entry["claimSnapshot"])
    entry["acceptedSourceDigest"] = _accepted_source_digest(sources)
    return entry


def op_reject(scene: dict, args: dict) -> dict:
    node_id = args["id"]
    if node_id not in scene["nodes"]:
        raise SceneError(f"NODE_MISSING:{node_id}")
    entry = scene.setdefault("evidence", {}).setdefault(node_id, {"status": "candidate", "sources": []})
    node = scene["nodes"][node_id]
    author_identity = _author_identity(scene, node)
    sources = entry.get("sources", [])
    try:
        reviewer = execution_identity_api.require_independent_reviewer(
            author_identity,
            args.get("reviewerIdentity"),
            severity=args.get("severity"),
            required_input_digests={claim_hash(scene, node_id), _accepted_source_digest(sources)},
        )
    except execution_identity_api.IdentityError as error:
        raise SceneError(str(error)) from error
    _register_execution(scene, reviewer)
    entry["status"] = "rejected"
    entry["reviewer"] = reviewer
    entry["reason"] = args["reason"]
    entry["claimSnapshot"] = claim_payload(scene, node_id)
    entry["claimHash"] = _canonical_digest(entry["claimSnapshot"])
    return entry


def strict_actor_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", normalized):
        raise SceneError("ACTOR_ID_INVALID:use 3-64 ASCII letters, digits, dot, underscore or hyphen")
    return normalized


def op_open_issue(scene: dict, args: dict) -> dict:
    issues = scene.setdefault("review", {}).setdefault("issues", [])
    issue_id = str(args.get("id") or "").strip()
    if not issue_id:
        raise SceneError("ISSUE_ID_REQUIRED")
    if any(row.get("id") == issue_id for row in issues):
        raise SceneError(f"ISSUE_EXISTS:{issue_id}")
    severity = args.get("severity")
    if severity not in {"P0", "P1", "P2", "P3"}:
        raise SceneError(f"ISSUE_SEVERITY_INVALID:{severity}")
    summary = str(args.get("summary") or "").strip()
    kind = str(args.get("kind") or "").strip()
    if not summary or not kind:
        raise SceneError(f"ISSUE_FIELDS_REQUIRED:{issue_id}")
    execution = _require_operation(args.get("execution"), "pipeline:open-issue", {"author"})
    _register_execution(scene, execution)
    opened_by = strict_actor_key(str(args.get("openedBy") or execution["actorId"]))
    if opened_by != execution["actorId"]:
        raise SceneError("EXECUTION_ACTOR_MISMATCH:open-issue")
    targets = list(args.get("targetNodeIds") or [])
    missing = [node_id for node_id in targets if node_id not in scene.get("nodes", {})]
    if missing:
        raise SceneError(f"ISSUE_TARGET_MISSING:{issue_id}:{','.join(missing)}")
    issue = {
        "id": issue_id, "status": "OPEN", "severity": severity, "kind": kind,
        "summary": summary, "openedBy": opened_by, "openedByRunId": execution["runId"],
        "targetNodeIds": targets,
        "openedAt": now_iso(),
    }
    if args.get("area"):
        issue["area"] = args["area"]
    issues.append(issue)
    return issue


def op_transition_issue(scene: dict, scene_path: Path, args: dict) -> dict:
    issue = next((row for row in scene.get("review", {}).get("issues", []) if row.get("id") == args["id"]), None)
    if issue is None:
        raise SceneError(f"ISSUE_MISSING:{args['id']}")
    expected = args.get("expectedStatus")
    if not expected or issue.get("status") != expected:
        raise SceneError(f"ISSUE_STATUS_STALE:{args['id']} expected={expected} actual={issue.get('status')}")
    status = args.get("status")
    if status not in {"PATCHED", "RESOLVED", "FAIL"}:
        raise SceneError(f"ISSUE_STATUS_INVALID:{status}")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise SceneError(f"ISSUE_REASON_REQUIRED:{args['id']}")
    author_run_id = issue.get("openedByRunId")
    author_identity = scene.get("meta", {}).get("executions", {}).get(author_run_id)
    if not isinstance(author_identity, dict):
        raise SceneError(f"AUTHOR_EXECUTION_IDENTITY_REQUIRED:{args['id']}")
    receipt_path = str(args.get("receiptPath") or "").strip()
    if not receipt_path:
        raise SceneError(f"ISSUE_RECEIPT_REQUIRED:{args['id']}")
    receipt_file = resolve_evidence_file(scene_path, receipt_path)
    if not receipt_file.is_file():
        raise SceneError(f"ISSUE_RECEIPT_MISSING:{receipt_path}")
    digest = hashlib.sha256(receipt_file.read_bytes()).hexdigest()
    supplied_digest = str(args.get("receiptSha256") or "").lower()
    if supplied_digest and supplied_digest != digest:
        raise SceneError(f"ISSUE_RECEIPT_HASH_STALE:{receipt_path}")
    try:
        reviewer = execution_identity_api.require_independent_reviewer(
            author_identity,
            args.get("reviewerIdentity"),
            severity=issue.get("severity"),
            required_input_digests={digest},
        )
    except execution_identity_api.IdentityError as error:
        raise SceneError(str(error)) from error
    _register_execution(scene, reviewer)
    issue["status"] = status
    issue["resolution"] = {
        "previousStatus": expected,
        "reviewer": reviewer,
        "reason": reason,
        "resolvedAt": now_iso(),
        "receipt": {"path": receipt_path, "sha256": digest},
    }
    return issue


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
        # Levels are scene containers, not reviewable geometry. Treating a
        # level without an evidence entry as a candidate creates a permanent
        # false blocker even when every geometric node has been adjudicated.
        if node.get("type") == "level":
            continue
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

def apply_ops(
    scene: dict,
    scene_path: Path,
    ops: list[dict],
    actor: str,
    execution: dict | None = None,
) -> list[dict]:
    results = []
    for op in ops:
        kind = op.get("op")
        payload = {k: v for k, v in op.items() if k != "op"}
        if execution is not None:
            if kind in {
                "create_wall", "add_door", "add_window", "add_opening", "add_item",
                "add_slab", "add_ceiling", "add_zone", "add_column",
            }:
                payload["execution"] = execution
            elif kind in {"accept", "reject", "transition_issue"}:
                payload["reviewerIdentity"] = execution
            elif kind == "open_issue":
                payload["execution"] = execution
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
        elif kind == "transition_issue":
            results.append(op_transition_issue(scene, scene_path, payload))
        elif kind == "open_issue":
            results.append(op_open_issue(scene, payload))
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
    parser.add_argument("--actor", help="display actor id; must match --identity when supplied")
    parser.add_argument("--identity", type=Path, help="execution identity JSON required for mutations")
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
    p.add_argument("--source-role")
    p.add_argument("--producer")
    p.add_argument("--provenance-receipt")
    p.add_argument("--note")
    p.add_argument("--allow-missing", action="store_true")

    p = sub.add_parser("accept")
    p.add_argument("--id", required=True)
    p.add_argument("--mode", required=True, choices=["measured", "inferred"])
    p.add_argument("--reason")

    p = sub.add_parser("reject")
    p.add_argument("--id", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("transition-issue", help="change an issue state using an independent hash-bound receipt")
    p.add_argument("--id", required=True)
    p.add_argument("--expected-status", required=True, choices=["OPEN", "PATCHED", "FAIL"])
    p.add_argument("--status", required=True, choices=["PATCHED", "RESOLVED", "FAIL"])
    p.add_argument("--reason", required=True)
    p.add_argument("--receipt-path", required=True)
    p.add_argument("--receipt-sha256")

    p = sub.add_parser("open-issue", help="open a fail-closed review issue, optionally bound to existing nodes")
    p.add_argument("--id", required=True)
    p.add_argument("--severity", required=True, choices=["P0", "P1", "P2", "P3"])
    p.add_argument("--kind", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--opened-by", required=True)
    p.add_argument("--target-node-id", action="append")
    p.add_argument("--area")

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
        execution = None
        if args.identity:
            try:
                execution = execution_identity_api.load_identity(args.identity.resolve())
            except execution_identity_api.IdentityError as error:
                raise SceneError(str(error)) from error
            if args.actor and strict_actor_key(args.actor) != execution["actorId"]:
                raise SceneError("EXECUTION_ACTOR_MISMATCH:cli")
            args.actor = execution["actorId"]
        elif args.command not in {"find", "measure", "validate", "summary"}:
            raise SceneError("EXECUTION_IDENTITY_REQUIRED:use --identity <execution-identity.json>")
        args.actor = args.actor or "query-agent"

        operation_by_command = {
            "init": "scene:create",
            "create-wall": "scene:mutate", "add-door": "scene:mutate",
            "add-window": "scene:mutate", "add-opening": "scene:mutate",
            "add-item": "scene:mutate", "add-slab": "scene:mutate",
            "add-ceiling": "scene:mutate", "add-zone": "scene:mutate",
            "add-column": "scene:mutate", "update-node": "scene:mutate",
            "delete-node": "scene:delete", "undo": "scene:undo",
            "attach-evidence": "evidence:attach", "open-issue": "pipeline:open-issue",
            "accept": "pipeline:submit-verdict", "reject": "pipeline:submit-verdict",
            "transition-issue": "pipeline:submit-verdict", "apply-patch": "scene:mutate",
        }
        if args.command in operation_by_command:
            _require_operation(execution, operation_by_command[args.command])

        if args.command == "init":
            if scene_path.is_file():
                raise SceneError(f"SCENE_EXISTS:{scene_path} (refusing to overwrite; delete it first)")
            scene = new_scene(args.dataset, args.level_height, args.level_elevation, args.actor, execution)
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
            forbidden = {"accept", "reject", "transition_issue"}.intersection(
                {str(row.get("op")) for row in ops if isinstance(row, dict)}
            )
            if forbidden:
                raise SceneError("REVIEW_VERDICT_BATCH_FORBIDDEN:use dedicated reviewer commands")
            results = apply_ops(scene, scene_path, ops, args.actor, execution)
            save_scene(scene_path, scene, args.actor)
            emit({"ok": True, "applied": len(results)})
            return 0

        op_map = {
            "create-wall": lambda: op_create_wall(scene, {
                "start": args.start, "end": args.end, "kind": args.kind,
                "baseHeight": args.base_height, "material": material_from_args(args), "id": args.id,
                **({"height": args.height} if args.height is not None else {}),
                **({"thickness": args.thickness} if args.thickness is not None else {}),
                "execution": execution,
            }, args.actor),
            "add-door": lambda: op_add_opening(
                scene, "door", {**vars_for_opening(args), "execution": execution}, args.actor,
            ),
            "add-window": lambda: op_add_opening(
                scene, "window", {**vars_for_opening(args), "execution": execution}, args.actor,
            ),
            "add-opening": lambda: op_add_opening(
                scene, "opening", {**vars_for_opening(args), "execution": execution}, args.actor,
            ),
            "add-item": lambda: op_add_item(scene, {
                "category": args.category, "center": args.center,
                "yaw": math.radians(args.yaw_deg), "size": args.size, "color": args.color,
                "layout": json.loads(args.layout) if args.layout else None,
                "confidence": args.confidence, "id": args.id, "execution": execution,
            }, args.actor),
            "add-slab": lambda: op_add_polygon_node(
                scene, "slab", {**vars_for_polygon(args), "execution": execution}, args.actor,
            ),
            "add-ceiling": lambda: op_add_polygon_node(
                scene, "ceiling", {**vars_for_polygon(args), "execution": execution}, args.actor,
            ),
            "add-zone": lambda: op_add_polygon_node(
                scene, "zone", {**vars_for_polygon(args), "execution": execution}, args.actor,
            ),
            "add-column": lambda: op_add_column(scene, {
                "center": args.center, "size": args.size, "height": args.height,
                "yaw": math.radians(args.yaw_deg), "id": args.id, "execution": execution,
            }, args.actor),
            "update-node": lambda: op_update_node(scene, {
                "id": args.id,
                "updates": {key: json.loads(raw) for key, raw in (pair.split("=", 1) for pair in args.set)},
            }),
            "delete-node": lambda: {"deleted": op_delete_node(scene, args.id)},
            "attach-evidence": lambda: op_attach_evidence(scene, scene_path, {
                "id": args.id, "type": args.type, "path": args.path,
                "sourceRole": args.source_role, "producer": args.producer,
                "provenanceReceipt": args.provenance_receipt,
                "note": args.note, "allow_missing": args.allow_missing,
            }),
            "accept": lambda: op_accept(scene, scene_path, {
                "id": args.id, "mode": args.mode, "reviewerIdentity": execution,
                "reason": args.reason,
            }),
            "reject": lambda: op_reject(scene, {
                "id": args.id, "reviewerIdentity": execution, "reason": args.reason,
            }),
            "transition-issue": lambda: op_transition_issue(scene, scene_path, {
                "id": args.id, "expectedStatus": args.expected_status, "status": args.status,
                "reviewerIdentity": execution, "reason": args.reason,
                "receiptPath": args.receipt_path, "receiptSha256": args.receipt_sha256,
            }),
            "open-issue": lambda: op_open_issue(scene, {
                "id": args.id, "severity": args.severity, "kind": args.kind,
                "summary": args.summary, "openedBy": args.opened_by, "execution": execution,
                "targetNodeIds": args.target_node_id or [], "area": args.area,
            }),
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
