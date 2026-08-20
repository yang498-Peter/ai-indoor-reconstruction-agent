#!/usr/bin/env python3
"""MCP stdio server exposing the Semantic Scene V2 mutation API as tools.

Transport is MCP stdio: newline-delimited JSON-RPC 2.0 on stdin/stdout, no
Content-Length framing. Only stderr is free for logging -- anything written to
stdout that is not a JSON-RPC message corrupts the session.

This is a thin wrapper. Every gate (opening fit, evidence hashes, independent
reviewer, structural validation) lives in scene_api.py and stays there; the
server never reimplements a rule, so an agent driving MCP and an operator
driving the CLI hit exactly the same fail-closed behaviour.

Each mutating tool call is its own transaction: load -> mutate -> save. Reload
per call means a rejected batch cannot leak a half-applied scene into the next
call, because the failed in-memory copy is discarded with the request.

Coordinates are SOURCE plan meters (x, y), Z-up -- identical to scene_api.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "semantic-scene-v2"
SERVER_VERSION = "1.0.0"

CORE_DIR = Path(__file__).resolve().parent


def load_scene_api():
    """Import scene_api.py by path.

    The directory is named `scene-core`, which is not a legal package name, so
    a plain import only works when the caller happens to run from inside it.
    Loading by explicit path makes the server independent of cwd and sys.path.
    """
    existing = sys.modules.get("scene_api")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("scene_api", CORE_DIR / "scene_api.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_LOAD_SCENE_API:{CORE_DIR / 'scene_api.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scene_api"] = module
    spec.loader.exec_module(module)
    return module


api = load_scene_api()
SceneError = api.SceneError


def load_fit_service():
    """Import fit_service.py by path (same rationale as load_scene_api).

    Loaded lazily on first fit-tool call: fit_service pulls in numpy/laspy via
    capture_index, and the scene-authoring tool surface must keep working on a
    host without the point-cloud stack installed.
    """
    existing = sys.modules.get("fit_service")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("fit_service", CORE_DIR / "fit_service.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_LOAD_FIT_SERVICE:{CORE_DIR / 'fit_service.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fit_service"] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as error:
        del sys.modules["fit_service"]
        raise SceneError(f"FIT_SERVICE_UNAVAILABLE:{error}")
    return module


def load_semantic_candidates():
    """Import semantic_candidates.py by path (same rationale as load_scene_api).

    Loaded lazily on first semantic-tool call: it pulls in numpy via
    photo_projection, and the scene-authoring tool surface must keep working
    on a host without the numeric stack installed.
    """
    existing = sys.modules.get("semantic_candidates")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "semantic_candidates", CORE_DIR / "semantic_candidates.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_LOAD_SEMANTIC_CANDIDATES:{CORE_DIR / 'semantic_candidates.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["semantic_candidates"] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as error:
        del sys.modules["semantic_candidates"]
        raise SceneError(f"SEMANTIC_CANDIDATES_UNAVAILABLE:{error}")
    return module


# ---------------------------------------------------------------------------
# Shared JSON Schema fragments
# ---------------------------------------------------------------------------

POINT = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
    "description": "Plan point [x, y] in source plan meters.",
}

POLYGON = {
    "type": "array",
    "items": dict(POINT, description="Polygon vertex [x, y] in meters."),
    "minItems": 3,
    "description": "Closed plan polygon as [[x, y], ...] in meters; do not repeat the first vertex.",
}

MATERIAL = {
    "type": "object",
    "properties": {
        "color": {"type": "string", "description": "Hex color such as \"#c8c4bc\"."},
        "description": {"type": "string", "description": "Free-text material note, e.g. \"painted gypsum\"."},
    },
    "additionalProperties": True,
    "description": "Optional appearance hints; never affects geometry or gates.",
}

NODE_ID = {"type": "string", "description": "Existing node id, e.g. \"wall_south\"."}
OPTIONAL_ID = {
    "type": "string",
    "description": "Optional stable id to assign. Omit to get a generated id such as \"wall_1a2b3c\".",
}
LEVEL_ID = {
    "type": "string",
    "description": "Parent level id. Omit to use the scene's default level.",
}

OPENING_PROPERTIES = {
    "wall": dict(NODE_ID, description="Host wall id. The opening becomes a child of this wall."),
    "offset": {
        "type": "number",
        "description": (
            "Distance in meters from the wall START point to the CENTER of the opening, "
            "measured along the wall. Not an edge offset. The full span "
            "[offset - width/2, offset + width/2] must lie inside the wall or the save is rejected."
        ),
    },
    "width": {"type": "number", "description": "Opening width in meters, measured along the wall."},
    "height": {"type": "number", "description": "Opening height in meters. sill + height must not exceed the wall height."},
    "sill": {
        "type": "number",
        "description": "Sill height in meters above the wall base. Defaults to 0 for doors/openings and 0.9 for windows.",
    },
    "material": MATERIAL,
    "id": OPTIONAL_ID,
}
OPENING_REQUIRED = ["wall", "offset", "width", "height"]

FLOOR_PLANE = {
    "type": "object",
    "properties": {
        "a": {"type": "number"}, "b": {"type": "number"}, "c": {"type": "number"},
    },
    "required": ["a", "b", "c"],
    "additionalProperties": False,
    "description": "Floor plane z = a*x + b*y + c in source meters (from level_survey.py).",
}

FIT_PROPERTIES = {
    "index": {
        "type": "string",
        "description": "Path to the capture-index directory (contains capture-index.json).",
    },
    "floor_z": {"type": "number",
                "description": "Flat floor elevation in meters. Provide this or floor_plane."},
    "floor_plane": FLOOR_PLANE,
    "band_min": {"type": "number",
                 "description": "Structural band lower bound in meters above the floor. Default 0.5."},
    "band_max": {"type": "number",
                 "description": "Structural band upper bound in meters above the floor. Default 2.4."},
    "corridor": {"type": "number",
                 "description": "Half-width in meters of the fit corridor around the line. Default 0.35."},
    "robust": {"type": "boolean",
               "description": "Use RANSAC + IRLS outlier-resistant fitting. Default true."},
    "seed": {"type": "integer", "description": "Deterministic RANSAC seed. Default 0."},
}


AGENT_GUIDE = """# Semantic Scene V2 - agent workflow

Authoritative coordinates are SOURCE plan meters (x, y), Z-up. All lengths,
widths, heights, thicknesses and offsets in this tool surface are meters.
Display mapping (`display = [x, elevation, -y]`) is owned by the renderer --
never pre-apply it here.

## Standard loop

1. `init_scene` once per dataset (refuses to overwrite an existing scene).
2. `create_wall` for structure, using source plan meters. When a capture index
   is available, draw a rough line from visual evidence and let `propose_wall`
   refine it first (measurement only); `refine_wall_line` re-checks an existing
   wall the same way. Both tools never write -- authoring stays with you.
3. `add_door` / `add_window` / `add_opening` hosted on a wall, where
   `offset` is the distance from the wall start to the opening CENTER.
   When you (or a VLM/SAM pass) spot something in a photo, submit the pixel
   box via `submit_semantic_observations` to land it on geometry first: the
   result is always a candidate estimate to confirm, never a coordinate truth,
   and the tool never writes.
4. `add_slab` / `add_ceiling` / `add_zone` / `add_column` / `add_item` for the
   rest of the level.
5. `attach_evidence` to bind each claim to a file next to the scene; the
   sha256 is computed and stored at attach time.
6. `accept_node` from a registered read-only reviewer execution that is
   independent of the author execution. P0/P1 claims require a regional or
   adversarial reviewer class.
7. `find_nodes` / `measure` / `get_node` / `get_scene_summary` /
   `validate_scene` to inspect; `undo` to roll back one revision.

Use `apply_patch` when several edits must land together: the whole batch is
validated before anything is written, so a bad op leaves the scene untouched.

## Invariants enforced by the API (not by convention)

- An opening must fit fully inside its host wall, must not be taller than the
  wall, and must not overlap another opening on the same wall.
- `accept_node` with mode `measured` needs at least one evidence file that
  exists and whose sha256 still matches the ledger.
- `accept_node` with mode `inferred` needs a reason plus at least two verified
  files with distinct content hashes and disjoint root lineages. Repeating one
  file or a derived crop of the same root is still one source.
- The execution run, actor and checked-in tool policy are immutable. A reviewer
  policy cannot mutate authority or evidence.
- Acceptance stores a geometry/topology `claimHash`; editing the node, its host
  wall, hosted openings or coordinate frame demotes affected claims to
  `candidate`.
- Every write runs structural validation first; a failure writes nothing, and
  every successful write snapshots the previous revision for `undo`.

## Working habits

- Prefer explicit `id` values for nodes you will reference later.
- A tool result with `isError: true` means the scene on disk is unchanged;
  read the error code (e.g. `OPENING_OUTSIDE_WALL`, `SELF_REVIEW_FORBIDDEN`),
  fix the argument, and retry rather than working around the gate.
- Call `get_scene_summary` after a batch to confirm node counts and evidence
  statuses match your intent.
"""


# ---------------------------------------------------------------------------
# Tool handlers
#
# Mutating handlers receive an already-loaded scene and return whatever should
# land in the tool result; the dispatcher owns load/save so no handler can
# forget to persist (or persist a query by accident).
# ---------------------------------------------------------------------------

def h_init_scene(scene_path: Path, actor: str, args: dict) -> dict:
    if scene_path.is_file():
        raise SceneError(f"SCENE_EXISTS:{scene_path.name} (refusing to overwrite an existing scene)")
    scene = api.new_scene(
        args["dataset"],
        float(args.get("level_height", 3.05)),
        float(args.get("level_elevation", 0.0)),
        actor,
        args["__execution"],
    )
    api.save_scene(scene_path, scene, actor)
    return {"dataset": args["dataset"], "levelId": api.default_level_id(scene)}


def h_undo(scene_path: Path, actor: str, args: dict) -> dict:
    scene = api.undo(scene_path)
    return {"restoredRevision": scene.get("revision", {}).get("counter")}


def h_agent_guide(scene_path: Path, actor: str, args: dict) -> dict:
    return {"guide": AGENT_GUIDE}


def h_create_wall(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_create_wall(scene, args, actor)


def h_add_door(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_opening(scene, "door", args, actor)


def h_add_window(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_opening(scene, "window", args, actor)


def h_add_opening(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_opening(scene, "opening", args, actor)


def h_add_item(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_item(scene, args, actor)


def h_add_slab(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_polygon_node(scene, "slab", args, actor)


def h_add_ceiling(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_polygon_node(scene, "ceiling", args, actor)


def h_add_zone(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_polygon_node(scene, "zone", args, actor)


def h_add_column(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_add_column(scene, args, actor)


def h_update_node(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_update_node(scene, args)


def h_delete_node(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return {"deleted": api.op_delete_node(scene, args["id"])}


def h_attach_evidence(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_attach_evidence(scene, scene_path, args)


def h_accept_node(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_accept(scene, scene_path, args)


def h_reject_node(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_reject(scene, args)


def h_transition_issue(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_transition_issue(scene, scene_path, args)


def h_open_issue(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_open_issue(scene, args)


def h_apply_patch(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    results = api.apply_ops(scene, scene_path, args["ops"], actor, args["__execution"])
    return {"applied": len(results), "results": results}


def h_find_nodes(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_find(scene, {"type": args.get("type"), "status": args.get("status")})


def h_measure(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    # op_measure indexes "from"/"to" directly, so always hand it all three keys.
    return api.op_measure(scene, {"id": args.get("id"), "from": args.get("from"), "to": args.get("to")})


def h_summary(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    return api.op_summary(scene)


def h_get_node(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    node_id = args["id"]
    node = scene["nodes"].get(node_id)
    if node is None:
        raise SceneError(f"NODE_MISSING:{node_id}")
    return {
        "node": node,
        "evidence": scene.get("evidence", {}).get(node_id, {"status": "candidate", "sources": []}),
    }


def h_validate(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    api.validate_scene(scene)
    return {"valid": True, "sceneSha256": api.scene_sha256(scene)}


def _open_capture_index(args: dict):
    fit = load_fit_service()
    try:
        return fit.CaptureIndex.open(Path(args["index"]))
    except fit.CaptureIndexError as error:
        raise SceneError(f"CAPTURE_INDEX_ERROR:{error}")


def _run_fit(index, start, end, args: dict) -> dict:
    fit = load_fit_service()
    band = (float(args.get("band_min", 0.5)), float(args.get("band_max", 2.4)))
    try:
        return fit.refine_wall_line(
            index, start, end,
            floor_z=args.get("floor_z"),
            floor_plane=args.get("floor_plane"),
            band=band,
            corridor_m=float(args.get("corridor", 0.35)),
            robust=bool(args.get("robust", True)),
            seed=int(args.get("seed", 0)),
        )
    except fit.CaptureIndexError as error:
        raise SceneError(f"CAPTURE_INDEX_ERROR:{error}")


def h_propose_wall(scene_path: Path, actor: str, args: dict) -> dict:
    # Measurement only: the refined line is returned to the Agent, never
    # written. Writing stays with create_wall/update_node so authorship and
    # gates keep a single path.
    index = _open_capture_index(args)
    proposal = _run_fit(index, args["start"], args["end"], args)
    return {"written": False, "proposal": proposal}


def h_refine_wall_line(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    wall_id = args["id"]
    node = scene["nodes"].get(wall_id)
    if node is None or node.get("type") != "wall":
        raise SceneError(f"HOST_NOT_A_WALL:{wall_id}")
    index = _open_capture_index(args)
    refinement = _run_fit(index, node["start"], node["end"], args)
    result: dict = {
        "written": False,
        "wallId": wall_id,
        "storedCenterline": {"start": node["start"], "end": node["end"]},
        "storedThicknessM": node.get("thickness"),
        "refinement": refinement,
        # The fitted line is a wall FACE; against a stored CENTERLINE the
        # honest comparison is the paired centerline when both faces exist.
        "faceDeviation": refinement.get("deviationFromInput"),
    }
    paired = refinement.get("doubleSided", {}).get("pairedCenterline")
    if paired:
        result["centerlineDeviation"] = load_fit_service().line_deviation(
            node["start"], node["end"], paired["start"], paired["end"],
        )
    return result


def h_submit_semantic_observations(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    # Measurement only: pixel boxes are landed on existing scene geometry by
    # ray casting and returned as candidates. Writing stays with the normal
    # add tools so authorship and gates keep a single path, and coordinate
    # truth stays with geometry, never with the semantic observer.
    semantic = load_semantic_candidates()
    transforms_path = Path(args["transforms"])
    if not transforms_path.is_file():
        raise SceneError(f"TRANSFORMS_MISSING:{transforms_path}")
    frames = semantic.pp.load_frames(transforms_path)
    reports: dict[str, dict] = {}
    for wall_id, report_path in (args.get("geometry_reports") or {}).items():
        path = Path(report_path)
        if not path.is_file():
            raise SceneError(f"GEOMETRY_REPORT_MISSING:{wall_id}:{path}")
        reports[wall_id] = json.loads(path.read_text(encoding="utf-8"))
    try:
        report = semantic.ground_observations(
            scene, frames, args["observations"],
            ground_z=float(args["ground_z"]),
            geometry_reports=reports,
            merge_tolerance_m=float(
                args.get("merge_tolerance", semantic.DEFAULT_MERGE_TOLERANCE_M)
            ),
        )
    except semantic.ObservationError as error:
        raise SceneError(str(error))
    return {"written": False, "report": report}


# ---------------------------------------------------------------------------
# Tool table
#
# mode "mutate" -> load, run, save;  "query" -> load, run;  "raw" -> no scene
# is loaded (the tool owns the file itself, or needs no file at all).
# ---------------------------------------------------------------------------

def tool(name: str, mode: str, handler: Callable, description: str,
         properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "mode": mode,
        "handler": handler,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOL_SPECS: list[dict] = [
    tool(
        "init_scene", "raw", h_init_scene,
        "Create a blank Semantic Scene V2 file with a single level. Refuses to overwrite an "
        "existing scene. Run this once before any other mutating tool.",
        {
            "dataset": {"type": "string", "description": "Capture/dataset identifier recorded in the scene."},
            "level_height": {"type": "number", "description": "Default level (storey) height in meters. Default 3.05."},
            "level_elevation": {"type": "number", "description": "Level base elevation in meters. Default 0."},
        },
        ["dataset"],
    ),
    tool(
        "create_wall", "mutate", h_create_wall,
        "Create a wall on the level from a start point to an end point. Coordinates are source "
        "plan meters (x, y); thickness, height and baseHeight are meters. The wall centerline is "
        "start->end, and hosted openings are positioned by distance from start.",
        {
            "start": dict(POINT, description="Wall start point [x, y] in meters."),
            "end": dict(POINT, description="Wall end point [x, y] in meters."),
            "height": {"type": "number", "description": "Wall height in meters. Defaults to the level height."},
            "thickness": {"type": "number", "description": "Wall thickness in meters. Default 0.12 solid, 0.045 glass."},
            "kind": {"type": "string", "enum": ["solid", "glass"], "description": "Wall kind. Default \"solid\"."},
            "baseHeight": {"type": "number", "description": "Wall base elevation in meters above the level. Default 0."},
            "material": MATERIAL,
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["start", "end"],
    ),
    tool(
        "add_door", "mutate", h_add_door,
        "Add a door hosted on a wall. offset is the distance in meters from the wall START point "
        "to the door CENTER along the wall. The door must fit fully inside the wall, must not be "
        "taller than the wall, and must not overlap another opening.",
        dict(OPENING_PROPERTIES), list(OPENING_REQUIRED),
    ),
    tool(
        "add_window", "mutate", h_add_window,
        "Add a window hosted on a wall. offset is the distance in meters from the wall START point "
        "to the window CENTER along the wall; sill defaults to 0.9 m. sill + height must not exceed "
        "the wall height.",
        dict(OPENING_PROPERTIES), list(OPENING_REQUIRED),
    ),
    tool(
        "add_opening", "mutate", h_add_opening,
        "Add a generic wall opening (doorless pass-through, cased opening) hosted on a wall. "
        "offset is the distance in meters from the wall START point to the opening CENTER.",
        dict(OPENING_PROPERTIES), list(OPENING_REQUIRED),
    ),
    tool(
        "add_item", "mutate", h_add_item,
        "Add a furniture / movable object on the level. center is [x, y] in source plan meters and "
        "size is [width, depth, height] in meters.",
        {
            "category": {"type": "string", "description": "Item category, e.g. \"table\", \"chair\", \"cabinet\"."},
            "center": dict(POINT, description="Item plan center [x, y] in meters."),
            "size": {
                "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                "description": "Bounding size [width, depth, height] in meters.",
            },
            "yaw": {"type": "number", "description": "Plan rotation in RADIANS, counter-clockwise. Default 0."},
            "elevation": {"type": "number", "description": "Base elevation in meters above the level. Default 0."},
            "color": {"type": "string", "description": "Hex color. Default \"#b9b4a8\"."},
            "layout": {"type": "object", "additionalProperties": True,
                       "description": "Optional structured layout hints, e.g. {\"seatCount\": 8}."},
            "confidence": {"type": "number", "description": "Optional detection confidence in [0, 1]."},
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["category", "center", "size"],
    ),
    tool(
        "add_slab", "mutate", h_add_slab,
        "Add a floor slab polygon on the level. Polygon vertices are [x, y] in source plan meters; "
        "thickness and elevation are meters.",
        {
            "polygon": POLYGON,
            "thickness": {"type": "number", "description": "Slab thickness in meters. Default 0.05."},
            "elevation": {"type": "number", "description": "Slab top elevation in meters. Default 0."},
            "name": {"type": "string", "description": "Optional human-readable name."},
            "material": MATERIAL,
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["polygon"],
    ),
    tool(
        "add_ceiling", "mutate", h_add_ceiling,
        "Add a ceiling polygon on the level. Polygon vertices are [x, y] in source plan meters; "
        "elevation is meters above the level base.",
        {
            "polygon": POLYGON,
            "elevation": {"type": "number", "description": "Ceiling elevation in meters. Default 2.95."},
            "name": {"type": "string", "description": "Optional human-readable name."},
            "material": MATERIAL,
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["polygon"],
    ),
    tool(
        "add_zone", "mutate", h_add_zone,
        "Add a zone / room polygon on the level (a semantic region, not rendered geometry). "
        "Polygon vertices are [x, y] in source plan meters.",
        {
            "polygon": POLYGON,
            "name": {"type": "string", "description": "Zone name, e.g. \"meeting room\"."},
            "material": MATERIAL,
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["polygon"],
    ),
    tool(
        "add_column", "mutate", h_add_column,
        "Add a structural column on the level. center is [x, y] and size is [width, depth]; all "
        "values are meters.",
        {
            "center": dict(POINT, description="Column plan center [x, y] in meters."),
            "size": dict(POINT, description="Column plan size [width, depth] in meters. Default [0.4, 0.4]."),
            "height": {"type": "number", "description": "Column height in meters. Default 3.05."},
            "yaw": {"type": "number", "description": "Plan rotation in RADIANS. Default 0."},
            "baseHeight": {"type": "number", "description": "Base elevation in meters above the level. Default 0."},
            "level": LEVEL_ID,
            "id": OPTIONAL_ID,
        },
        ["center"],
    ),
    tool(
        "update_node", "mutate", h_update_node,
        "Update fields of an existing node using dotted keys (e.g. \"height\", \"material.color\"). "
        "Lengths remain meters. id/type/parentId/children are protected. Updating the geometry of an "
        "accepted node demotes it back to candidate.",
        {
            "id": NODE_ID,
            "updates": {
                "type": "object", "additionalProperties": True,
                "description": "Dotted key -> new value, e.g. {\"height\": 2.9, \"material.color\": \"#aabbcc\"}.",
            },
        },
        ["id", "updates"],
    ),
    tool(
        "delete_node", "mutate", h_delete_node,
        "Delete a node and all of its descendants (deleting a wall also deletes its hosted "
        "openings). Returns the ids that were removed.",
        {"id": NODE_ID},
        ["id"],
    ),
    tool(
        "attach_evidence", "mutate", h_attach_evidence,
        "Attach an evidence source to a node's ledger entry. path is relative to the scene "
        "directory; the file's sha256 is computed and stored so later tampering is detected. "
        "A missing file is rejected unless allow_missing is true (only valid for reasoning-only "
        "sources such as \"inference-basis\").",
        {
            "id": NODE_ID,
            "type": {"type": "string",
                     "description": "Evidence kind, e.g. \"high-structure-slice\", \"elevation\", \"photo\", \"inference-basis\"."},
            "path": {"type": "string", "description": "Evidence file path relative to the scene directory."},
            "sourceRole": {"type": "string", "description": "Semantic role of this source."},
            "producer": {"type": "string", "description": "Tool or operator that produced the evidence file."},
            "provenanceReceipt": {"type": "string", "description": "Optional receipt binding a derived file to root input hashes."},
            "note": {"type": "string", "description": "Short note on what the source shows."},
            "allow_missing": {"type": "boolean",
                              "description": "Allow a source without an on-disk file. Default false."},
        },
        ["id", "type"],
    ),
    tool(
        "accept_node", "mutate", h_accept_node,
        "Accept a node. mode \"measured\" requires at least one attached evidence file that exists "
        "and still matches its stored sha256. mode \"inferred\" requires a reason plus at least two "
        "verified content hashes with disjoint root lineages. The server identity must be an "
        "independent read-only reviewer execution.",
        {
            "id": NODE_ID,
            "mode": {"type": "string", "enum": ["measured", "inferred"], "description": "Acceptance mode."},
            "reason": {"type": "string", "description": "Why the inference holds. Required for mode \"inferred\"."},
        },
        ["id", "mode"],
    ),
    tool(
        "reject_node", "mutate", h_reject_node,
        "Reject a node with a mandatory reason. The node stays in the scene but is marked rejected "
        "in the evidence ledger.",
        {
            "id": NODE_ID,
            "reason": {"type": "string", "description": "Why the node is rejected."},
        },
        ["id", "reason"],
    ),
    tool(
        "transition_issue", "mutate", h_transition_issue,
        "Transition an existing review issue using optimistic old-status matching and an independent, "
        "hash-bound receipt. This never infers that an issue is resolved from geometry state alone.",
        {
            "id": {"type": "string", "description": "Existing issue id."},
            "expectedStatus": {"type": "string", "enum": ["OPEN", "PATCHED", "FAIL"]},
            "status": {"type": "string", "enum": ["PATCHED", "RESOLVED", "FAIL"]},
            "reason": {"type": "string", "description": "Evidence-bounded transition reason."},
            "receiptPath": {"type": "string", "description": "Receipt relative to scene/work root."},
            "receiptSha256": {"type": "string", "description": "Optional pinned receipt SHA-256."},
        },
        ["id", "expectedStatus", "status", "reason", "receiptPath"],
    ),
    tool(
        "open_issue", "mutate", h_open_issue,
        "Open an explicit review issue. Target ids, when provided, must already exist; duplicate issue ids fail.",
        {
            "id": {"type": "string"},
            "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "kind": {"type": "string"},
            "summary": {"type": "string"},
            "targetNodeIds": {"type": "array", "items": {"type": "string"}},
            "area": {"type": "string"},
        },
        ["id", "severity", "kind", "summary"],
    ),
    tool(
        "apply_patch", "mutate", h_apply_patch,
        "Apply an atomic batch of operations. Each op is an object with an \"op\" key plus that "
        "operation's arguments, e.g. {\"op\": \"create_wall\", \"start\": [0, 0], \"end\": [8.5, 0]}. "
        "Supported ops: create_wall, add_door, add_window, add_opening, add_item, add_slab, "
        "add_ceiling, add_zone, add_column, update_node, delete_node, attach_evidence, accept, "
        "reject, open_issue, transition_issue. If any op or the final validation fails, nothing is written.",
        {
            "ops": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True,
                          "properties": {"op": {"type": "string", "description": "Operation name."}},
                          "required": ["op"]},
                "description": "Ordered operations applied as one transaction.",
            },
        },
        ["ops"],
    ),
    tool(
        "find_nodes", "query", h_find_nodes,
        "List nodes with their evidence status, optionally filtered by node type and/or status. "
        "Read-only.",
        {
            "type": {"type": "string",
                     "description": "Node type filter: level, wall, door, window, opening, column, slab, ceiling, zone, item."},
            "status": {"type": "string",
                       "enum": ["candidate", "accepted-measured", "accepted-inferred", "rejected"],
                       "description": "Evidence status filter."},
        },
    ),
    tool(
        "measure", "query", h_measure,
        "Measure the scene in meters. Pass id for a single wall's length, or from+to for the plan "
        "distance between two nodes' plan centers. Read-only.",
        {
            "id": dict(NODE_ID, description="Wall id to measure the length of, in meters."),
            "from": dict(NODE_ID, description="First node id for a plan distance measurement."),
            "to": dict(NODE_ID, description="Second node id for a plan distance measurement."),
        },
    ),
    tool(
        "get_scene_summary", "query", h_summary,
        "Return dataset, revision counter, scene sha256, node counts per type, evidence status "
        "counts and open review issues. Read-only; call it to confirm a batch landed as intended.",
        {},
    ),
    tool(
        "get_node", "query", h_get_node,
        "Return one node's full record plus its evidence ledger entry (status, sources, reviewer, "
        "reason). Read-only.",
        {"id": NODE_ID},
        ["id"],
    ),
    tool(
        "validate_scene", "query", h_validate,
        "Run structural validation without mutating anything: hierarchy consistency, wall "
        "dimensions, opening fit/overlap, polygon size and evidence-ledger rules. Returns the "
        "scene sha256 when clean.",
        {},
    ),
    tool(
        "propose_wall", "raw", h_propose_wall,
        "Refine a rough Agent-drawn wall line (allowed to be off by ~0.3 m and ~8 degrees) against "
        "raw CaptureIndex points using robust RANSAC/IRLS fitting inside a structural height band. "
        "Returns the refined start/end plus evidence: supportPointCount, inlierRatio, "
        "residualP50M/residualP90M, double-face thickness candidates and a FIT_OK / LOW_SUPPORT / "
        "AMBIGUOUS status with reason. NEVER writes the scene -- review the measurement, then use "
        "create_wall to author the wall yourself.",
        {
            "start": dict(POINT, description="Rough wall line start [x, y] in source plan meters."),
            "end": dict(POINT, description="Rough wall line end [x, y] in source plan meters."),
            **FIT_PROPERTIES,
        },
        ["start", "end", "index"],
    ),
    tool(
        "refine_wall_line", "query", h_refine_wall_line,
        "Re-fit an EXISTING wall node's centerline against raw CaptureIndex points and return a "
        "deviation report (face and paired-centerline deltas in meters and degrees) plus the same "
        "fit evidence as propose_wall. Read-only: apply any correction yourself with update_node.",
        {
            "id": dict(NODE_ID, description="Existing wall node id to re-fit."),
            **FIT_PROPERTIES,
        },
        ["id", "index"],
    ),
    tool(
        "submit_semantic_observations", "query", h_submit_semantic_observations,
        "Ground VLM/SAM pixel-box observations (semantic-observations-v1 payload) into candidates "
        "by ray casting against the scene's existing walls and floor. Door/window/opening labels "
        "must land on a wall and come back with hostOffsetM/widthM/sillM/headM estimates; free "
        "labels become floor-contact item or wall-surface estimates. Every result stays status "
        "\"candidate\" with coordinateSource \"ray-cast-estimate\" and requiresGeometryConfirmation "
        "true unless a supplied opening-candidates geometry report corroborates it "
        "(corroboration \"geometry+semantic\", geometry dimensions win). NEVER writes the scene -- "
        "confirm with propose_wall/opening-candidates evidence, then author via the add tools.",
        {
            "observations": {
                "type": "object", "additionalProperties": True,
                "description": (
                    "Full semantic-observations-v1 payload: {schemaVersion: \"1.0\", "
                    "captureFingerprint, observations: [{frameId, bbox: [x0, y0, x1, y1] pixels, "
                    "label, labelConfidence, observer, note?}]}. Pixel boxes only -- world "
                    "coordinates in the payload are rejected by schema."
                ),
            },
            "transforms": {
                "type": "string",
                "description": "Path to the capture's transforms.json (posed undistorted frames).",
            },
            "ground_z": {
                "type": "number",
                "description": "LAS z of scene elevation zero (las_z = elevation + ground_z), "
                               "e.g. -0.5 for rtk-house-2.",
            },
            "geometry_reports": {
                "type": "object", "additionalProperties": {"type": "string"},
                "description": "Optional wall node id -> path of an opening-candidates JSON report "
                               "used to corroborate semantic openings on that wall.",
            },
            "merge_tolerance": {
                "type": "number",
                "description": "hostOffset agreement window in meters for geometry corroboration. "
                               "Default 0.4.",
            },
        },
        ["observations", "transforms", "ground_z"],
    ),
    tool(
        "undo", "raw", h_undo,
        "Restore the previous revision snapshot, discarding the most recent write. Snapshots are "
        "taken automatically before every successful write.",
        {},
    ),
    tool(
        "get_agent_guide", "raw", h_agent_guide,
        "Return a short markdown guide to the standard scene-authoring workflow and the invariants "
        "the API enforces. Read this before the first edit of a session.",
        {},
    ),
]

TOOLS: dict[str, dict] = {spec["name"]: spec for spec in TOOL_SPECS}
TOOL_OPERATIONS = {
    "init_scene": "scene:create",
    "create_wall": "scene:mutate", "add_door": "scene:mutate",
    "add_window": "scene:mutate", "add_opening": "scene:mutate",
    "add_item": "scene:mutate", "add_slab": "scene:mutate",
    "add_ceiling": "scene:mutate", "add_zone": "scene:mutate",
    "add_column": "scene:mutate", "update_node": "scene:mutate",
    "delete_node": "scene:delete", "attach_evidence": "evidence:attach",
    "accept_node": "pipeline:submit-verdict", "reject_node": "pipeline:submit-verdict",
    "transition_issue": "pipeline:submit-verdict", "open_issue": "pipeline:open-issue",
    "apply_patch": "scene:mutate", "undo": "scene:undo",
}


def public_tools(identity: dict | None = None) -> list[dict]:
    operations = api.execution_identity_api.policy_operations(identity) if identity else None
    return [
        {"name": spec["name"], "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for spec in TOOL_SPECS
        if operations is None or TOOL_OPERATIONS.get(spec["name"]) in operations or spec["name"] not in TOOL_OPERATIONS
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def call_tool(scene_path: Path, identity: dict, name: str, arguments: dict) -> dict:
    """Run one tool and return the MCP tools/call result payload.

    Failures come back as isError results rather than JSON-RPC errors: a gate
    rejection is a normal, actionable outcome for the agent, and the protocol
    layer stays reserved for genuinely malformed traffic.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return error_result(f"UNKNOWN_TOOL:{name}")
    operation = TOOL_OPERATIONS.get(name)
    if operation and operation not in api.execution_identity_api.policy_operations(identity):
        return error_result(f"EXECUTION_OPERATION_FORBIDDEN:{operation}")
    args = dict(arguments) if isinstance(arguments, dict) else {}
    args["__execution"] = identity
    actor = identity["actorId"]
    if name in {
        "create_wall", "add_door", "add_window", "add_opening", "add_item",
        "add_slab", "add_ceiling", "add_zone", "add_column",
    }:
        args["execution"] = identity
    elif name in {"accept_node", "reject_node", "transition_issue"}:
        args["reviewerIdentity"] = identity
    elif name == "open_issue":
        args["execution"] = identity
        args["openedBy"] = identity["actorId"]
    try:
        if spec["mode"] == "raw":
            payload = spec["handler"](scene_path, actor, args)
        else:
            scene = api.load_scene(scene_path)
            payload = spec["handler"](scene, scene_path, actor, args)
            if spec["mode"] == "mutate":
                api.save_scene(scene_path, scene, actor)
        return text_result({"ok": True, "tool": name, "result": payload})
    except SceneError as error:
        return error_result(str(error))
    except KeyError as error:
        return error_result(f"MISSING_ARGUMENT:{error.args[0]} for tool {name}")
    except (TypeError, ValueError) as error:
        return error_result(f"BAD_ARGUMENT:{name}: {error}")
    except OSError as error:
        return error_result(f"IO_FAILED:{name}: {error}")


def text_result(payload: Any, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over newline-delimited stdio
# ---------------------------------------------------------------------------

def handle_message(message: dict, scene_path: Path, identity: dict) -> dict | None:
    method = message.get("method")
    message_id = message.get("id")
    # Absent id means notification: MCP forbids a response, even for errors.
    is_notification = "id" not in message or message_id is None

    if isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "initialize":
        result: Any = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": public_tools(identity)}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call_tool(scene_path, identity, params.get("name"), params.get("arguments") or {})
    else:
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0", "id": message_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def write_message(stream, payload: dict) -> None:
    # json.dumps escapes embedded newlines, so one message is always one line.
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def serve(scene_path: Path, identity: dict, stdin, stdout) -> int:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            write_message(stdout, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {error}"},
            })
            continue
        if not isinstance(message, dict):
            write_message(stdout, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"},
            })
            continue
        response = handle_message(message, scene_path, identity)
        if response is not None:
            write_message(stdout, response)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP stdio server for the Semantic Scene V2 API")
    parser.add_argument("--scene", required=True, type=Path, help="scene V2 JSON path (created by init_scene)")
    parser.add_argument("--identity", required=True, type=Path, help="execution identity and checked-in tool policy")
    args = parser.parse_args(argv)

    try:
        identity = api.execution_identity_api.load_identity(args.identity.resolve())
    except api.execution_identity_api.IdentityError as error:
        print(str(error), file=sys.stderr)
        return 2

    # The transport is a byte-exact JSON stream; a locale-dependent console
    # codec on Windows would mangle non-ASCII names before the client sees them.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")

    return serve(args.scene, identity, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
