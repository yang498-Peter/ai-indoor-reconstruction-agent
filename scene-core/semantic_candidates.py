#!/usr/bin/env python3
"""Ground VLM/SAM pixel-box observations into geometry-anchored candidates.

Permission model (docs/自动重建流程全面优化设计_2026-08-17 P2): semantic models
only ever produce "a labelled pixel box in one photo frame plus a confidence"
(schemas/semantic-observations-v1.schema.json).  Coordinate truth always comes
from geometry.  This module is the bridge that turns those observations into
*candidates* by ray casting against geometry the scene already owns:

* rays are cast from the bbox centre and the four edge midpoints
  (pixel -> camera -> world, via the verified photo_projection conventions:
  scene x/y == LAS x/y, ``las_z = elevation + ground_z``, displayOffset never
  participates);
* opening-like labels must land on an existing wall: the nearest camera-facing
  wall face plane hit by the centre ray becomes the host, and the edge rays
  intersected with that same plane give hostOffsetM / widthM / sillM / headM
  estimates (sill/head are meters above scene elevation zero, matching
  opening_candidates.py when its ``floor_z`` equals ``ground_z``);
* free labels that hit no wall (furniture and friends) are grounded on the
  floor plane ``z = ground_z`` using the bbox BOTTOM edge midpoint ray (the
  object's floor contact), with a coarse size estimate from bbox extent and
  camera depth;
* every candidate stays ``status: candidate`` with
  ``coordinateSource: ray-cast-estimate`` and
  ``requiresGeometryConfirmation: true`` - the agent must confirm with
  fit_service / opening_candidates before authoring, and writing still goes
  through the normal scene tools.

Cross-check: when an opening_candidates.py geometry report exists for the same
wall and holds a candidate within ``merge_tolerance_m`` of the semantic
hostOffset, the two merge into one candidate marked
``corroboration: geometry+semantic`` whose dimensions come from GEOMETRY (the
semantic numbers survive under ``semanticEstimate``) and whose confidence is
boosted.  Semantic-only candidates keep a deliberately low confidence cap.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import photo_projection as pp  # noqa: E402

PRODUCER = "semantic-candidates-v1"
SCHEMA_VERSION = "1.0"

# Semantic-only estimates are deliberately capped low: a pixel box with no
# geometry corroboration must never look as trustworthy as a measured hole.
SEMANTIC_ONLY_CONFIDENCE_CAP = 0.4
CORROBORATION_BOOST = 0.2
CORROBORATED_CONFIDENCE_CAP = 0.95

# hostOffset agreement window for merging a semantic candidate with a
# geometry (opening_candidates) candidate on the same wall.
DEFAULT_MERGE_TOLERANCE_M = 0.4
# Wall extent slack for accepting a centre-ray hit (bbox centres sit inside
# the opening, so generous margins are unnecessary; this absorbs fit noise).
EXTENT_MARGIN_M = 0.3
NEAR_M = 0.05

OPENING_LABEL_PREFIXES = ("door", "window", "opening", "glass", "glazing")


class ObservationError(ValueError):
    """Raised with an OBSERVATIONS_INVALID:<detail> style message."""


# ---------------------------------------------------------------------------
# Observation payload validation (mirrors semantic-observations-v1.schema.json;
# jsonschema is not a repo dependency, so the contract is enforced by hand and
# the schema file stays the human/agent-readable source of truth).
# ---------------------------------------------------------------------------

def validate_observations_payload(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ObservationError("OBSERVATIONS_INVALID:payload must be a JSON object")
    allowed_top = {"schemaVersion", "captureFingerprint", "observations"}
    unknown = set(payload) - allowed_top
    if unknown:
        raise ObservationError(f"OBSERVATIONS_INVALID:unknown top-level keys {sorted(unknown)}")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ObservationError(
            f"OBSERVATIONS_INVALID:schemaVersion must be \"{SCHEMA_VERSION}\""
        )
    fingerprint = payload.get("captureFingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ObservationError("OBSERVATIONS_INVALID:captureFingerprint must be a non-empty string")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ObservationError("OBSERVATIONS_INVALID:observations must be a non-empty array")
    allowed_keys = {"frameId", "bbox", "label", "labelConfidence", "observer", "note"}
    normalized: list[dict] = []
    for index, item in enumerate(observations):
        where = f"observations[{index}]"
        if not isinstance(item, dict):
            raise ObservationError(f"OBSERVATIONS_INVALID:{where} must be an object")
        unknown = set(item) - allowed_keys
        if unknown:
            raise ObservationError(f"OBSERVATIONS_INVALID:{where} unknown keys {sorted(unknown)}")
        for key in ("frameId", "label", "observer"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ObservationError(f"OBSERVATIONS_INVALID:{where}.{key} must be a non-empty string")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list) or len(bbox) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in bbox)
        ):
            raise ObservationError(f"OBSERVATIONS_INVALID:{where}.bbox must be [x0, y0, x1, y1] finite numbers >= 0")
        if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            raise ObservationError(f"OBSERVATIONS_INVALID:{where}.bbox needs x0 < x1 and y0 < y1")
        confidence = item.get("labelConfidence")
        if (
            not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0
        ):
            raise ObservationError(f"OBSERVATIONS_INVALID:{where}.labelConfidence must be in [0, 1]")
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ObservationError(f"OBSERVATIONS_INVALID:{where}.note must be a string")
        normalized.append({
            "frameId": item["frameId"],
            "bbox": [float(v) for v in bbox],
            "label": item["label"],
            "labelConfidence": float(confidence),
            "observer": item["observer"],
            **({"note": note} if note else {}),
        })
    return normalized


# ---------------------------------------------------------------------------
# Ray casting
# ---------------------------------------------------------------------------

def pixel_ray(frame: "pp.Frame", u: float, v: float) -> tuple[np.ndarray, np.ndarray]:
    """Pixel -> (world origin, world unit direction) for a pinhole frame.

    Inverts the pinhole projection: normalized OpenCV direction, flip to the
    OpenGL camera axes stored in ``Frame.c2w``, rotate into the world.
    """
    camera = frame.camera
    if camera.model != "pinhole":
        raise ObservationError(f"FRAME_NOT_PINHOLE:{frame.frame_id} model={camera.model}")
    direction_cv = np.array([(u - camera.cx) / camera.fx, (v - camera.cy) / camera.fy, 1.0])
    direction_gl = direction_cv * np.array([1.0, -1.0, -1.0])
    direction = frame.c2w[:3, :3] @ direction_gl
    return frame.center, direction / np.linalg.norm(direction)


def wall_planes(scene: dict, ground_z: float) -> list[dict]:
    """Wall centerlines expanded into vertical face data in LAS coordinates."""
    planes: list[dict] = []
    for node in scene.get("nodes", {}).values():
        if node.get("type") != "wall":
            continue
        start = np.asarray(node["start"], dtype=np.float64)
        end = np.asarray(node["end"], dtype=np.float64)
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length < 0.05:
            continue
        unit = vector / length
        base_z = ground_z + float(node.get("baseHeight", 0.0))
        planes.append({
            "id": node["id"],
            "start": start,
            "unit": unit,
            "normal": np.array([-unit[1], unit[0]]),
            "lengthM": length,
            "thicknessM": float(node.get("thickness", 0.12)),
            "baseZ": base_z,
            "topZ": base_z + float(node.get("height", 0.0)),
        })
    return planes


def _intersect_wall_face(origin: np.ndarray, direction: np.ndarray, wall: dict):
    """Ray vs the camera-facing wall face plane -> (t, hit, along) or None."""
    side = float(np.dot(wall["normal"], origin[:2] - wall["start"]))
    face_shift = wall["normal"] * (wall["thicknessM"] * 0.5) * (1.0 if side >= 0 else -1.0)
    plane_point = np.array([*(wall["start"] + face_shift), 0.0])
    normal3 = np.array([wall["normal"][0], wall["normal"][1], 0.0])
    denom = float(np.dot(normal3, direction))
    if abs(denom) < 1e-9:
        return None
    t = float(np.dot(normal3, plane_point - origin)) / denom
    if t <= NEAR_M:
        return None
    hit = origin + t * direction
    along = float(np.dot(hit[:2] - wall["start"], wall["unit"]))
    return t, hit, along


def _host_wall_hit(origin: np.ndarray, direction: np.ndarray, planes: list[dict],
                   margin_m: float = EXTENT_MARGIN_M):
    """Nearest wall whose face the ray hits inside the wall extent (occlusion
    by farther walls falls out of the nearest-t choice)."""
    best = None
    for wall in planes:
        result = _intersect_wall_face(origin, direction, wall)
        if result is None:
            continue
        t, hit, along = result
        if not (-margin_m <= along <= wall["lengthM"] + margin_m):
            continue
        if not (wall["baseZ"] - margin_m <= hit[2] <= wall["topZ"] + margin_m):
            continue
        if best is None or t < best[0]:
            best = (t, hit, along, wall)
    return best


def _intersect_ground(origin: np.ndarray, direction: np.ndarray, ground_z: float):
    if abs(direction[2]) < 1e-9:
        return None
    t = (ground_z - origin[2]) / direction[2]
    if t <= NEAR_M:
        return None
    return t, origin + t * direction


def _is_opening_label(label: str) -> bool:
    return label.strip().lower().startswith(OPENING_LABEL_PREFIXES)


def _observation_ref(observation: dict) -> dict:
    return {key: observation[key] for key in ("frameId", "bbox", "label", "labelConfidence", "observer", "note")
            if key in observation}


def _bbox_rays(frame: "pp.Frame", bbox: list[float]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    return {
        "center": pixel_ray(frame, cx, cy),
        "left": pixel_ray(frame, x0, cy),
        "right": pixel_ray(frame, x1, cy),
        "top": pixel_ray(frame, cx, y0),
        "bottom": pixel_ray(frame, cx, y1),
    }


def _ground_opening(rays: dict, planes: list[dict], observation: dict, ground_z: float):
    origin, center_dir = rays["center"]
    host = _host_wall_hit(origin, center_dir, planes)
    if host is None:
        return None, "NO_WALL_HIT"
    _, center_hit, _, wall = host
    edges = {}
    for name in ("left", "right", "top", "bottom"):
        result = _intersect_wall_face(*rays[name], wall)
        if result is None:
            return None, f"EDGE_RAY_MISSED_WALL:{name}"
        edges[name] = result
    along_left, along_right = edges["left"][2], edges["right"][2]
    sill = float(edges["bottom"][1][2]) - ground_z
    head = float(edges["top"][1][2]) - ground_z
    candidate = {
        "kind": "opening",
        "type": observation["label"].strip().lower().split("-")[0].split(" ")[0],
        "status": "candidate",
        "coordinateSource": "ray-cast-estimate",
        "requiresGeometryConfirmation": True,
        "corroboration": "semantic-only",
        "hostWallId": wall["id"],
        "hostOffsetM": round((along_left + along_right) * 0.5, 4),
        "widthM": round(abs(along_right - along_left), 4),
        "sillM": round(sill, 4),
        "headM": round(head, 4),
        "rayHitDistanceM": round(float(np.linalg.norm(center_hit - origin)), 3),
        "confidence": round(min(SEMANTIC_ONLY_CONFIDENCE_CAP, observation["labelConfidence"] * 0.5), 4),
        "observation": _observation_ref(observation),
    }
    return candidate, None


def _ground_free_label(rays: dict, planes: list[dict], observation: dict, ground_z: float, frame: "pp.Frame"):
    """Furniture/material/etc: wall surface when the wall is clearly the first
    thing the centre ray meets, otherwise a floor-contact item estimate."""
    origin, _ = rays["center"]
    wall_hit = _host_wall_hit(origin, rays["center"][1], planes)
    ground_hit = _intersect_ground(*rays["bottom"], ground_z)
    ground_t = ground_hit[0] if ground_hit else math.inf
    if wall_hit is not None and wall_hit[0] < ground_t - 0.5:
        t, hit, along, wall = wall_hit
        candidate = {
            "kind": "wall-surface",
            "status": "candidate",
            "coordinateSource": "ray-cast-estimate",
            "requiresGeometryConfirmation": True,
            "corroboration": "semantic-only",
            "hostWallId": wall["id"],
            "hostOffsetM": round(along, 4),
            "heightAboveZeroM": round(float(hit[2]) - ground_z, 4),
            "rayHitDistanceM": round(t, 3),
            "confidence": round(min(SEMANTIC_ONLY_CONFIDENCE_CAP, observation["labelConfidence"] * 0.5), 4),
            "observation": _observation_ref(observation),
        }
        return candidate, None
    if ground_hit is None:
        return None, "NO_GROUND_HIT"
    t, hit = ground_hit
    x0, y0, x1, y1 = observation["bbox"]
    # Coarse pinhole size estimate at the object's camera depth (OpenCV z of
    # the floor contact): rough by construction, agent-facing hint only.
    depth = float(frame.world_to_camera(hit)[2])
    camera = frame.camera
    candidate = {
        "kind": "item",
        "status": "candidate",
        "coordinateSource": "ray-cast-estimate",
        "requiresGeometryConfirmation": True,
        "corroboration": "semantic-only",
        "planPosition": [round(float(hit[0]), 4), round(float(hit[1]), 4)],
        "sizeEstimateM": {
            "width": round((x1 - x0) * depth / camera.fx, 3),
            "height": round((y1 - y0) * depth / camera.fy, 3),
        },
        "rayHitDistanceM": round(t, 3),
        "confidence": round(min(SEMANTIC_ONLY_CONFIDENCE_CAP, observation["labelConfidence"] * 0.5), 4),
        "observation": _observation_ref(observation),
    }
    return candidate, None


# ---------------------------------------------------------------------------
# Geometry corroboration
# ---------------------------------------------------------------------------

def corroborate(candidates: list[dict], geometry_reports: dict[str, dict],
                merge_tolerance_m: float = DEFAULT_MERGE_TOLERANCE_M) -> list[dict]:
    """Merge semantic opening candidates with opening_candidates.py output.

    ``geometry_reports`` maps wall node id -> the opening-candidates report
    (the JSON produced by ``opening_candidates.detect_openings``).  A merge is
    keyed only on hostOffset agreement; on merge the GEOMETRY dimensions win
    and the semantic numbers move under ``semanticEstimate``.
    """
    merged: list[dict] = []
    for candidate in candidates:
        if candidate.get("kind") != "opening":
            merged.append(candidate)
            continue
        report = geometry_reports.get(candidate.get("hostWallId"))
        rows = (report or {}).get("candidates") or []
        best = None
        for row in rows:
            delta = abs(float(row["hostOffsetM"]) - float(candidate["hostOffsetM"]))
            if delta <= merge_tolerance_m and (best is None or delta < best[0]):
                best = (delta, row)
        if best is None:
            merged.append(candidate)
            continue
        delta, row = best
        confidence = min(
            CORROBORATED_CONFIDENCE_CAP,
            max(float(row.get("confidence", 0.0)), candidate["observation"]["labelConfidence"])
            + CORROBORATION_BOOST,
        )
        merged.append({
            **candidate,
            "type": row.get("type", candidate["type"]),
            "hostOffsetM": row["hostOffsetM"],
            "widthM": row["widthM"],
            "sillM": row["sillM"],
            "headM": row["headM"],
            "coordinateSource": "geometry-grid+ray-cast",
            "corroboration": "geometry+semantic",
            # geometry has already confirmed the hole; acceptance evidence and
            # review gates still apply through the normal scene tools.
            "requiresGeometryConfirmation": False,
            "confidence": round(confidence, 4),
            "geometry": {
                "hostOffsetDeltaM": round(delta, 4),
                "candidate": row,
                "reportLineageId": (report or {}).get("lineageId"),
                "reportFingerprint": (report or {}).get("parametersFingerprint"),
            },
        })
    return merged


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def frames_by_key(frames: list["pp.Frame"]) -> dict[str, "pp.Frame"]:
    lookup: dict[str, pp.Frame] = {}
    for index, frame in enumerate(frames):
        for key in (frame.frame_id, frame.file_path, str(index)):
            lookup.setdefault(key, frame)
    return lookup


def ground_observations(
    scene: dict,
    frames: list["pp.Frame"],
    payload: dict,
    *,
    ground_z: float,
    geometry_reports: dict[str, dict] | None = None,
    merge_tolerance_m: float = DEFAULT_MERGE_TOLERANCE_M,
) -> dict:
    """Validate an observation payload and land it on scene geometry.

    Read-only over the scene; the result is a candidate report the agent must
    confirm and author through the normal scene tools.
    """
    observations = validate_observations_payload(payload)
    planes = wall_planes(scene, ground_z)
    lookup = frames_by_key(frames)
    candidates: list[dict] = []
    unresolved: list[dict] = []
    for observation in observations:
        frame = lookup.get(observation["frameId"])
        if frame is None:
            unresolved.append({"observation": _observation_ref(observation), "reason": "FRAME_NOT_FOUND"})
            continue
        bbox = observation["bbox"]
        if bbox[2] > frame.camera.width or bbox[3] > frame.camera.height:
            unresolved.append({"observation": _observation_ref(observation), "reason": "BBOX_OUTSIDE_IMAGE"})
            continue
        try:
            rays = _bbox_rays(frame, bbox)
        except ObservationError as error:
            unresolved.append({"observation": _observation_ref(observation), "reason": str(error)})
            continue
        if _is_opening_label(observation["label"]):
            candidate, reason = _ground_opening(rays, planes, observation, ground_z)
        else:
            candidate, reason = _ground_free_label(rays, planes, observation, ground_z, frame)
        if candidate is None:
            unresolved.append({"observation": _observation_ref(observation), "reason": reason})
        else:
            candidates.append(candidate)
    candidates = corroborate(candidates, geometry_reports or {}, merge_tolerance_m)
    return {
        "schemaVersion": 1,
        "kind": "semantic-candidates",
        "producer": PRODUCER,
        "captureFingerprint": payload["captureFingerprint"],
        "parameters": {
            "groundZ": float(ground_z),
            "mergeToleranceM": float(merge_tolerance_m),
            "extentMarginM": EXTENT_MARGIN_M,
            "semanticOnlyConfidenceCap": SEMANTIC_ONLY_CONFIDENCE_CAP,
            "geometryReportWallIds": sorted((geometry_reports or {}).keys()),
        },
        "counts": {
            "observations": len(observations),
            "candidates": len(candidates),
            "corroborated": sum(1 for c in candidates if c.get("corroboration") == "geometry+semantic"),
            "unresolved": len(unresolved),
        },
        "candidates": candidates,
        "unresolved": unresolved,
    }


def _parse_report_arg(text: str) -> tuple[str, Path]:
    wall_id, _, path = text.partition("=")
    if not wall_id or not path:
        raise argparse.ArgumentTypeError("expected WALL_ID=path/to/opening-candidates.json")
    return wall_id, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--transforms", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path,
                        help="semantic-observations-v1 JSON payload")
    # Negative values must be passed as --ground-z=-0.5.
    parser.add_argument("--ground-z", required=True, type=float,
                        help="LAS z of scene elevation zero (las_z = elevation + ground_z)")
    parser.add_argument("--geometry-report", action="append", type=_parse_report_arg,
                        default=[], metavar="WALL_ID=PATH",
                        help="opening-candidates report for one wall; repeatable")
    parser.add_argument("--merge-tolerance", type=float, default=DEFAULT_MERGE_TOLERANCE_M)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    scene = json.loads(args.scene.read_text(encoding="utf-8"))
    frames = pp.load_frames(args.transforms)
    payload = json.loads(args.observations.read_text(encoding="utf-8"))
    reports = {
        wall_id: json.loads(path.read_text(encoding="utf-8"))
        for wall_id, path in args.geometry_report
    }
    report = ground_observations(
        scene, frames, payload,
        ground_z=args.ground_z,
        geometry_reports=reports,
        merge_tolerance_m=args.merge_tolerance,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
