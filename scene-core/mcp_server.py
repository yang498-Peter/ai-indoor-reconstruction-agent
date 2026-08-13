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


AGENT_GUIDE = """# Semantic Scene V2 - agent workflow

Authoritative coordinates are SOURCE plan meters (x, y), Z-up. All lengths,
widths, heights, thicknesses and offsets in this tool surface are meters.
Display mapping (`display = [x, elevation, -y]`) is owned by the renderer --
never pre-apply it here.

## Standard loop

1. `init_scene` once per dataset (refuses to overwrite an existing scene).
2. `create_wall` for structure, using source plan meters.
3. `add_door` / `add_window` / `add_opening` hosted on a wall, where
   `offset` is the distance from the wall start to the opening CENTER.
4. `add_slab` / `add_ceiling` / `add_zone` / `add_column` / `add_item` for the
   rest of the level.
5. `attach_evidence` to bind each claim to a file next to the scene; the
   sha256 is computed and stored at attach time.
6. `accept_node` as a reviewer who is NOT the node author.
7. `find_nodes` / `measure` / `get_node` / `get_scene_summary` /
   `validate_scene` to inspect; `undo` to roll back one revision.

Use `apply_patch` when several edits must land together: the whole batch is
validated before anything is written, so a bad op leaves the scene untouched.

## Invariants enforced by the API (not by convention)

- An opening must fit fully inside its host wall, must not be taller than the
  wall, and must not overlap another opening on the same wall.
- `accept_node` with mode `measured` needs at least one evidence file that
  exists and whose sha256 still matches the ledger.
- `accept_node` with mode `inferred` needs a reason plus at least two distinct
  sources. Inference can never masquerade as measurement.
- The reviewer must differ from the node author (`meta.createdBy`).
- Editing the geometry of an accepted node demotes it back to `candidate`.
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


def h_apply_patch(scene: dict, scene_path: Path, actor: str, args: dict) -> Any:
    results = api.apply_ops(scene, scene_path, args["ops"], actor)
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
        "sources. The reviewer must differ from the node author.",
        {
            "id": NODE_ID,
            "mode": {"type": "string", "enum": ["measured", "inferred"], "description": "Acceptance mode."},
            "reviewer": {"type": "string", "description": "Reviewer id; must differ from the node's meta.createdBy."},
            "reason": {"type": "string", "description": "Why the inference holds. Required for mode \"inferred\"."},
            "allow_self": {"type": "boolean",
                           "description": "Bypass the independent-reviewer gate. Default false; avoid it."},
        },
        ["id", "mode", "reviewer"],
    ),
    tool(
        "reject_node", "mutate", h_reject_node,
        "Reject a node with a mandatory reason. The node stays in the scene but is marked rejected "
        "in the evidence ledger.",
        {
            "id": NODE_ID,
            "reviewer": {"type": "string", "description": "Reviewer id."},
            "reason": {"type": "string", "description": "Why the node is rejected."},
        },
        ["id", "reviewer", "reason"],
    ),
    tool(
        "apply_patch", "mutate", h_apply_patch,
        "Apply an atomic batch of operations. Each op is an object with an \"op\" key plus that "
        "operation's arguments, e.g. {\"op\": \"create_wall\", \"start\": [0, 0], \"end\": [8.5, 0]}. "
        "Supported ops: create_wall, add_door, add_window, add_opening, add_item, add_slab, "
        "add_ceiling, add_zone, add_column, update_node, delete_node, attach_evidence, accept, "
        "reject. If any op or the final validation fails, nothing is written.",
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


def public_tools() -> list[dict]:
    return [
        {"name": spec["name"], "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for spec in TOOL_SPECS
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def call_tool(scene_path: Path, actor: str, name: str, arguments: dict) -> dict:
    """Run one tool and return the MCP tools/call result payload.

    Failures come back as isError results rather than JSON-RPC errors: a gate
    rejection is a normal, actionable outcome for the agent, and the protocol
    layer stays reserved for genuinely malformed traffic.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return error_result(f"UNKNOWN_TOOL:{name}")
    args = arguments if isinstance(arguments, dict) else {}
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

def handle_message(message: dict, scene_path: Path, actor: str) -> dict | None:
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
        result = {"tools": public_tools()}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call_tool(scene_path, actor, params.get("name"), params.get("arguments") or {})
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


def serve(scene_path: Path, actor: str, stdin, stdout) -> int:
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
        response = handle_message(message, scene_path, actor)
        if response is not None:
            write_message(stdout, response)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP stdio server for the Semantic Scene V2 API")
    parser.add_argument("--scene", required=True, type=Path, help="scene V2 JSON path (created by init_scene)")
    parser.add_argument("--actor", default="mcp-agent", help="stable actor id recorded on mutations")
    args = parser.parse_args(argv)

    # The transport is a byte-exact JSON stream; a locale-dependent console
    # codec on Windows would mangle non-ASCII names before the client sees them.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")

    return serve(args.scene, args.actor, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
