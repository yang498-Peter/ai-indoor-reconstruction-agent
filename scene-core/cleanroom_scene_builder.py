#!/usr/bin/env python3
"""Build fresh scene layers from a raw-derived geometry workspace.

This module has no legacy-scene input.  It consumes only CaptureIndex,
structural proposals, the raw-derived level receipt, and an optional raw
``transforms.json``.  Algorithmic consolidation stays candidate in authority;
the hypothesis and presentation layers remain explicitly inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Any

import cv2
import numpy as np

from capture_index import CaptureIndex
from scene_api import evidence_lineage_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _root_content_sha256(index: CaptureIndex) -> str:
    """Content hash of the raw capture every derived artifact descends from."""
    identity = index.manifest.get("sourceIdentity") or {}
    return identity.get("contentSha256") or index.manifest["indexFingerprint"]


def _relative_scene_path(target: Path, scene_dir: Path) -> str:
    """Evidence paths stored in scene JSON must survive moving the bundle."""
    return Path(os.path.relpath(Path(target).resolve(), Path(scene_dir).resolve())).as_posix()


def _evidence_source(
    source_type: str,
    path: str,
    content_sha256: str,
    producer: str,
    root_content_sha256s: list[str] | None = None,
    generator_parameters: object = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Fully provenance-bound evidence source (quality_report_v2 contract).

    ``sha256`` and ``contentSha256`` are intentionally both written: legacy
    consumers read ``sha256`` while quality_report_v2 verifies both keys.
    """
    roots = sorted(set(root_content_sha256s or [content_sha256]))
    source: dict[str, Any] = {
        "type": source_type, "sourceRole": source_type, "path": path,
        "sha256": content_sha256, "contentSha256": content_sha256,
        "producer": producer, "rootContentSha256s": roots,
        "lineageId": evidence_lineage_id(roots, producer, generator_parameters),
    }
    if generator_parameters is not None:
        source["generatorParameters"] = generator_parameters
    if note:
        source["note"] = note
    return source


def _dataset_id(index: CaptureIndex) -> str:
    source = (index.manifest.get("sourceIdentity") or {}).get("path")
    if source:
        return f"{Path(source).stem}-cleanroom"
    return f"{index.manifest['indexFingerprint'][:16]}-cleanroom"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _angle_delta(a: float, b: float) -> float:
    delta = abs((a - b) % math.pi)
    return min(delta, math.pi - delta)


def _floor_envelope(index: CaptureIndex, floor_z: float, cell: float = 0.18) -> tuple[list[list[float]], dict[str, float]]:
    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / 2_000_000)))
    query = index.query_all(z_min=floor_z - 0.07, z_max=floor_z + 0.14, every=stride)
    if query.point_count < 100:
        raise ValueError("raw floor band has too few points")
    min_x, max_x = np.percentile(query.x, [0.5, 99.5])
    min_y, max_y = np.percentile(query.y, [0.5, 99.5])
    width = max(1, int(math.ceil((max_x - min_x) / cell)) + 1)
    height = max(1, int(math.ceil((max_y - min_y) / cell)) + 1)
    col = np.clip(((query.x - min_x) / cell).astype(np.int32), 0, width - 1)
    row = np.clip(((max_y - query.y) / cell).astype(np.int32), 0, height - 1)
    counts = np.zeros((height, width), dtype=np.uint16)
    np.add.at(counts, (row, col), 1)
    occupied = (counts >= 2).astype(np.uint8) * 255
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(occupied, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("raw floor band has no connected envelope")
    contour = max(contours, key=cv2.contourArea)
    epsilon = max(1.5, 0.008 * cv2.arcLength(contour, True))
    simplified = cv2.approxPolyDP(contour, epsilon, True)[:, 0, :]
    if len(simplified) < 3:
        simplified = cv2.convexHull(contour)[:, 0, :]
    polygon = [
        [round(float(min_x + x * cell), 5), round(float(max_y - y * cell), 5)]
        for x, y in simplified
    ]
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    bounds = {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}
    return polygon, bounds


def _axes(proposals: dict[str, Any]) -> list[float]:
    result: list[float] = []
    for family in proposals.get("axisFamilies", []):
        angle = math.radians(float(family["angleDeg"])) % math.pi
        if all(_angle_delta(angle, existing) > math.radians(8.0) for existing in result):
            result.append(angle)
    return result[:4]


def _candidate_records(proposals: dict[str, Any], bounds: dict[str, float]) -> list[dict[str, Any]]:
    axes = _axes(proposals)
    records: list[dict[str, Any]] = []
    pad = 1.5
    for candidate in proposals.get("wallCandidates", []):
        if float(candidate.get("lengthM", 0.0)) < 0.75 or float(candidate.get("confidence", 0.0)) < 0.48:
            continue
        if float(candidate.get("fitResidualP90M", 1.0)) > 0.16:
            continue
        line = candidate.get("suggestedCenterline") or candidate.get("rawCenterline")
        start = np.asarray(line["start"], dtype=np.float64)
        end = np.asarray(line["end"], dtype=np.float64)
        midpoint = (start + end) * 0.5
        if not (bounds["minX"] - pad <= midpoint[0] <= bounds["maxX"] + pad):
            continue
        if not (bounds["minY"] - pad <= midpoint[1] <= bounds["maxY"] + pad):
            continue
        vector = end - start
        length = float(np.linalg.norm(vector))
        angle = math.atan2(float(vector[1]), float(vector[0])) % math.pi
        family_index = min(range(len(axes)), key=lambda index: _angle_delta(angle, axes[index])) if axes else 0
        family_angle = axes[family_index] if axes and _angle_delta(angle, axes[family_index]) <= math.radians(6.0) else angle
        unit = np.asarray([math.cos(family_angle), math.sin(family_angle)])
        if float(np.dot(vector, unit)) < 0:
            unit = -unit
        normal = np.asarray([-unit[1], unit[0]])
        offset = float(np.dot(midpoint, normal))
        along = sorted((float(np.dot(start, unit)), float(np.dot(end, unit))))
        records.append({
            "proposal": candidate,
            "family": family_index,
            "angle": family_angle,
            "unit": unit,
            "normal": normal,
            "offset": offset,
            "startAlong": along[0],
            "endAlong": along[1],
            "length": length,
        })
    return records


# Half a typical partition thickness: two parallel walls one partition apart
# must stay two walls instead of being welded into a point-count-weighted blend.
_OFFSET_MERGE_TOLERANCE_M = 0.08
_OFFSET_SPREAD_REVIEW_M = 0.05
# Along-axis gaps below this are scan breakage and bridge silently; anything
# wider that still gets bridged may be a doorway, so it must be recorded.
_SILENT_BRIDGE_GAP_M = 0.25
_MAX_BRIDGE_GAP_M = 1.5


def _consolidate_walls(proposals: dict[str, Any], bounds: dict[str, float]) -> list[dict[str, Any]]:
    records = _candidate_records(proposals, bounds)
    offset_groups: list[list[dict[str, Any]]] = []
    for record in sorted(records, key=lambda item: (item["family"], item["offset"], item["startAlong"])):
        target = None
        for group in reversed(offset_groups):
            if group[0]["family"] != record["family"]:
                continue
            if abs(float(np.median([item["offset"] for item in group])) - record["offset"]) <= _OFFSET_MERGE_TOLERANCE_M:
                target = group
                break
        (target if target is not None else offset_groups.append([]) or offset_groups[-1]).append(record)

    walls: list[dict[str, Any]] = []
    for group in offset_groups:
        group.sort(key=lambda item: item["startAlong"])
        batches: list[list[dict[str, Any]]] = []
        for record in group:
            if not batches or record["startAlong"] - max(item["endAlong"] for item in batches[-1]) > _MAX_BRIDGE_GAP_M:
                batches.append([record])
            else:
                batches[-1].append(record)
        for batch in batches:
            start_along = min(item["startAlong"] for item in batch)
            end_along = max(item["endAlong"] for item in batch)
            if end_along - start_along < 0.8:
                continue
            # Opening-aware bridging: bridged gaps wide enough to be a doorway
            # are kept on the wall record so downstream review or
            # opening_candidates can turn them into opening candidates.
            bridged_gaps: list[dict[str, float]] = []
            covered_end = None
            for item in batch:
                if covered_end is not None and item["startAlong"] - covered_end >= _SILENT_BRIDGE_GAP_M:
                    bridged_gaps.append({
                        "alongStartM": round(covered_end - start_along, 4),
                        "alongEndM": round(item["startAlong"] - start_along, 4),
                        "widthM": round(item["startAlong"] - covered_end, 4),
                    })
                covered_end = item["endAlong"] if covered_end is None else max(covered_end, item["endAlong"])
            weights = np.asarray([max(1.0, float(item["proposal"].get("supportPointCount", 1))) for item in batch])
            offsets = np.asarray([item["offset"] for item in batch])
            offset = float(np.average(offsets, weights=weights))
            unit, normal = batch[0]["unit"], batch[0]["normal"]
            start = unit * start_along + normal * offset
            end = unit * end_along + normal * offset
            source_ids = sorted({str(item["proposal"]["id"]) for item in batch})
            paired = [item for item in batch if item["proposal"].get("wallMode") == "paired-faces"]
            thicknesses = [float(item["proposal"].get("thicknessM", 0.12)) for item in batch]
            wall = {
                "start": start,
                "end": end,
                "thickness": float(np.clip(np.median(thicknesses), 0.08, 0.35)),
                "confidence": max(float(item["proposal"].get("confidence", 0.0)) for item in batch),
                "residualP90M": min(float(item["proposal"].get("fitResidualP90M", 1.0)) for item in batch),
                "supportPointCount": sum(int(item["proposal"].get("supportPointCount", 0)) for item in batch),
                "sourceProposalIds": source_ids,
                "pairedFaceSupport": bool(paired),
                "bridgedGaps": bridged_gaps,
            }
            offset_spread = float(max(offsets) - min(offsets))
            if offset_spread > _OFFSET_SPREAD_REVIEW_M:
                wall["mergeSpreadM"] = round(offset_spread, 4)
            walls.append(wall)
    walls.sort(key=lambda wall: (-float(np.linalg.norm(wall["end"] - wall["start"])), wall["start"][0], wall["start"][1]))
    return walls


def _table_items(index: CaptureIndex, floor_z: float, bounds: dict[str, float]) -> list[dict[str, Any]]:
    query = index.query_bbox(
        bounds["minX"], bounds["minY"], bounds["maxX"], bounds["maxY"],
        z_min=floor_z + 0.62, z_max=floor_z + 0.92, every=3,
    )
    if query.point_count < 100:
        return []
    cell = 0.10
    width = max(1, int(math.ceil((bounds["maxX"] - bounds["minX"]) / cell)) + 1)
    height = max(1, int(math.ceil((bounds["maxY"] - bounds["minY"]) / cell)) + 1)
    col = np.clip(((query.x - bounds["minX"]) / cell).astype(np.int32), 0, width - 1)
    row = np.clip(((bounds["maxY"] - query.y) / cell).astype(np.int32), 0, height - 1)
    counts = np.zeros((height, width), dtype=np.uint16)
    np.add.at(counts, (row, col), 1)
    mask = (counts >= 2).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    components, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    items: list[dict[str, Any]] = []
    for label in range(1, components):
        ys, xs = np.nonzero(labels == label)
        if len(xs) < 12:
            continue
        rect = cv2.minAreaRect(np.column_stack((xs, ys)).astype(np.float32))
        (cx, cy), (size_a, size_b), angle_deg = rect
        long_side, short_side = sorted((size_a * cell, size_b * cell), reverse=True)
        if not (0.55 <= long_side <= 5.5 and 0.42 <= short_side <= 2.2 and long_side * short_side <= 10.0):
            continue
        if short_side < 0.48 and long_side > 2.0:
            continue
        yaw = math.radians(-angle_deg if size_a >= size_b else 90.0 - angle_deg)
        items.append({
            "center": [round(bounds["minX"] + cx * cell, 5), round(bounds["maxY"] - cy * cell, 5)],
            "yaw": yaw,
            "size": [round(long_side, 3), round(short_side, 3), 0.74],
            "category": "workstation" if long_side > 2.4 else "table",
            "confidence": 0.56,
            "supportCells": int(len(xs)),
        })
    return sorted(items, key=lambda item: (item["center"][1], item["center"][0]))[:80]


def _load_photos(transforms_path: Path | None) -> list[dict[str, Any]]:
    if transforms_path is None:
        return []
    raw = json.loads(transforms_path.read_text(encoding="utf-8"))
    model = raw.get("undistort_camera_model", {})
    result = []
    for frame in raw.get("frames", []):
        result.append({
            "id": str(frame.get("file_path", "" )).replace("\\", "/"),
            "path": str(frame.get("file_path", "" )).replace("\\", "/"),
            "poseClass": "posed-perspective-exact",
            "transformMatrix": frame.get("transform_matrix"),
            "cameraModel": model,
        })
    return result


def _base_scene(dataset: str, layer: str, floor_z: float, ceiling_z: float, source_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "dataset": dataset,
        "sceneLayer": layer,
        "coordinateFrame": {
            "authority": "source-plan-meters-z-up",
            "display": "three-y-up",
            "sourceToDisplay": "display = [x-centerX, z-floorZ, -(y-centerY)]",
        },
        "nodes": {
            "level_main": {
                "id": "level_main", "type": "level", "parentId": None, "children": [],
                "name": "Clean-room Level", "elevation": floor_z, "height": ceiling_z - floor_z,
            }
        },
        "rootNodeIds": ["level_main"],
        "evidence": {},
        "review": {"issues": [], "qualityLoops": [], "topology": {"endpointToleranceM": 0.04, "spaces": []}},
        "meta": {"source": source_meta, "pipeline": [], "photos": [], "cameraPath": []},
        "revision": {"counter": 1, "updatedBy": "cleanroom-builder"},
    }


def _add(scene: dict[str, Any], node: dict[str, Any], evidence: dict[str, Any]) -> None:
    node_id = node["id"]
    scene["nodes"][node_id] = node
    scene["nodes"][node["parentId"]]["children"].append(node_id)
    scene["evidence"][node_id] = evidence


def _scene_layers(
    dataset: str,
    walls: list[dict[str, Any]],
    floor_polygon: list[list[float]],
    items: list[dict[str, Any]],
    floor_z: float,
    ceiling_z: float,
    source_meta: dict[str, Any],
    evidence_path: str,
    evidence_sha: str,
    proposal_sha: str,
    photos: list[dict[str, Any]],
    proposal_path: str = "structural-proposals.json",
    root_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    authority = _base_scene(dataset, "authority", floor_z, ceiling_z, source_meta)
    hypothesis = _base_scene(dataset, "hypothesis", floor_z, ceiling_z, source_meta)
    presentation = _base_scene(dataset, "presentation", floor_z, ceiling_z, source_meta)
    hypothesis["meta"]["photos"] = photos
    presentation["meta"]["photos"] = photos
    for index, wall in enumerate(walls, 1):
        wall_id = f"wall_clean{index:03d}"
        common = {
            "id": wall_id, "type": "wall", "parentId": "level_main", "children": [],
            "wallKind": "solid",
            "start": [round(float(value), 5) for value in wall["start"]],
            "end": [round(float(value), 5) for value in wall["end"]],
            "height": round(ceiling_z - floor_z, 4),
            "thickness": round(float(wall["thickness"]), 4),
            "baseHeight": 0.0,
            "material": {"color": "#d8d2c7", "roughness": 0.82},
            "meta": {
                "createdBy": "cleanroom-builder",
                "note": wall.get("inferenceReason", "consolidated only from current raw-derived proposals"),
                "sourceProposalIds": wall["sourceProposalIds"],
            },
        }
        # Bridged gaps are candidate door openings; surface them for review
        # instead of leaving the wall silently welded shut.
        if wall.get("bridgedGaps"):
            common["meta"]["bridgedGaps"] = wall["bridgedGaps"]
        if wall.get("mergeSpreadM") is not None:
            common["meta"]["mergeSpreadM"] = wall["mergeSpreadM"]
        roots = [root_sha256] if root_sha256 else None
        sources = [
            _evidence_source(
                "high-structure-slice", evidence_path, evidence_sha,
                "indexed-pointcloud-evidence", roots,
                generator_parameters={"artifact": "band-walls"},
            ),
            _evidence_source(
                "inference-basis", proposal_path, proposal_sha,
                "structural-proposals", roots,
                note="dominant-axis and collinear interval consolidation",
            ),
        ]
        _add(authority, json.loads(json.dumps(common)), {
            "status": "candidate", "sources": sources,
            "gates": {"pairedFaceSupport": wall["pairedFaceSupport"], "fitResidualP90M": wall["residualP90M"],
                      "supportPointCount": wall["supportPointCount"], "agentReview": "NOT_RUN"},
        })
        inferred = json.loads(json.dumps(common))
        inferred["inference"] = {
            "confidenceClass": "supported-inferred", "confidence": round(float(wall["confidence"]), 4),
            "confidenceIntervalM": wall.get("confidenceIntervalM", [0.04, 0.18]),
            "inferenceReason": wall.get(
                "inferenceReason",
                "collinear raw structural returns consolidated on a current-capture axis family",
            ),
            "authorityRefs": [wall_id],
        }
        _add(hypothesis, inferred, {"status": "accepted-inferred", "sources": sources, "reviewer": "cleanroom-builder"})
        display = json.loads(json.dumps(inferred))
        display["material"] = {"color": "#e6dfd2", "roughness": 0.88}
        _add(presentation, display, {"status": "accepted-inferred", "sources": sources, "reviewer": "cleanroom-builder"})

    for scene in (hypothesis, presentation):
        _add(scene, {
            "id": "slab_visual01", "type": "slab", "parentId": "level_main", "children": [],
            "polygon": floor_polygon, "thickness": 0.08, "elevation": 0.0,
            "name": "Raw-supported visual floor", "material": {"color": "#aaa69f", "roughness": 0.94},
            "inference": {
                "confidenceClass": "visual-inferred", "confidence": 0.72,
                "confidenceIntervalM": [0.10, 0.30],
                "inferenceReason": "largest connected raw floor-band component",
                "authorityRefs": [],
            },
        }, {"status": "accepted-inferred", "sources": [_evidence_source(
            "overview", evidence_path, evidence_sha, "indexed-pointcloud-evidence",
            [root_sha256] if root_sha256 else None,
            generator_parameters={"artifact": "band-walls"},
        )]})

    for index, item in enumerate(items, 1):
        item_id = f"item_clean{index:03d}"
        node = {
            "id": item_id, "type": "item", "parentId": "level_main", "children": [],
            "category": item["category"], "center": item["center"], "yaw": item["yaw"], "size": item["size"],
            "elevation": 0.0, "color": "#88715d", "confidence": item["confidence"],
            "name": f"Raw height-band object {index}",
            "inference": {
                "confidenceClass": "visual-inferred", "confidence": item["confidence"],
                "confidenceIntervalM": [0.10, 0.35],
                "inferenceReason": "connected tabletop-height return; semantic type remains provisional",
                "authorityRefs": [],
            },
        }
        for scene in (hypothesis, presentation):
            _add(scene, json.loads(json.dumps(node)), {
                "status": "accepted-inferred",
                "sources": [_evidence_source(
                    "tabletop", evidence_path, evidence_sha, "indexed-pointcloud-evidence",
                    [root_sha256] if root_sha256 else None,
                    generator_parameters={"artifact": "band-walls"},
                    note=f"supportCells={item['supportCells']}",
                )],
            })
    return authority, hypothesis, presentation


def _export_points(index: CaptureIndex, output: Path, bounds: dict[str, float], floor_z: float, ceiling_z: float, max_points: int) -> dict[str, Any]:
    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / max_points)))
    query = index.query_bbox(
        bounds["minX"] - 1.0, bounds["minY"] - 1.0, bounds["maxX"] + 1.0, bounds["maxY"] + 1.0,
        z_min=floor_z - 0.15, z_max=ceiling_z + 0.35, every=stride,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(struct.pack("<4sIII", b"LRPC", 1, query.point_count, 16))
            for x, y, z, rgb in zip(query.x, query.y, query.z, query.rgb):
                stream.write(struct.pack(
                    "<fffBBBB", float(x), float(y), float(z),
                    int(rgb[0]), int(rgb[1]), int(rgb[2]), 0,
                ))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "schemaVersion": 1, "kind": "cleanroom-viewer-points", "format": "LRPC",
        "version": 1, "recordBytes": 16,
        "layout": "16-byte header; float32 x,y,z; uint8 r,g,b,pad; little-endian", "pointCount": query.point_count,
        "stride": stride, "binary": output.name, "binarySha256": _sha256(output),
        "indexFingerprint": index.manifest["indexFingerprint"], "floorZ": floor_z,
        "bounds": bounds, "queryStats": query.stats,
    }


def build(workspace: Path, output: Path, level_receipt: Path, transforms: Path | None = None, max_viewer_points: int = 300_000) -> dict[str, Any]:
    workspace, output = workspace.resolve(), output.resolve()
    if output.exists():
        raise ValueError("clean-room scene output already exists; choose a fresh directory")
    proposals_path = workspace / "structural-proposals.json"
    evidence_path = workspace / "evidence" / "band-walls.png"
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    levels = json.loads(level_receipt.read_text(encoding="utf-8"))
    index = CaptureIndex.open(workspace / "capture-index", validate_source=True)
    if proposals.get("indexFingerprint") != index.manifest.get("indexFingerprint"):
        raise ValueError("structural proposals do not match the current CaptureIndex")
    floor_z, ceiling_z = float(levels["floorZ"]), float(levels["ceilingZ"])
    floor_polygon, bounds = _floor_envelope(index, floor_z)
    walls = _consolidate_walls(proposals, bounds)
    if len(walls) < 4:
        raise ValueError("too few coherent raw-derived walls for a macro hypothesis")
    items = _table_items(index, floor_z, bounds)
    source_meta = {
        "inputPointCount": index.manifest["inputPointCount"],
        "indexFingerprint": index.manifest["indexFingerprint"],
        "captureFingerprint": (index.manifest.get("captureBinding") or {}).get("captureFingerprint"),
        "floorZ": floor_z, "ceilingZ": ceiling_z,
        "cleanroomPolicy": "no prior scene, coordinates, object list, or legacy generated asset was an input",
    }
    photos = _load_photos(transforms)
    authority, hypothesis, presentation = _scene_layers(
        _dataset_id(index), walls, floor_polygon, items, floor_z, ceiling_z,
        source_meta,
        _relative_scene_path(evidence_path, output), _sha256(evidence_path),
        _sha256(proposals_path), photos,
        proposal_path=_relative_scene_path(proposals_path, output),
        root_sha256=_root_content_sha256(index),
    )
    output.mkdir(parents=True)
    _atomic_json(output / "scene-authority.json", authority)
    authority_sha = _sha256(output / "scene-authority.json")
    hypothesis["bindings"] = {"authoritySha256": authority_sha}
    _atomic_json(output / "scene-hypothesis.json", hypothesis)
    presentation["bindings"] = {"authoritySha256": authority_sha, "hypothesisSha256": _sha256(output / "scene-hypothesis.json")}
    point_meta = _export_points(index, output / "pointcloud-cleanroom.lrpc", bounds, floor_z, ceiling_z, max_viewer_points)
    _atomic_json(output / "pointcloud-cleanroom.json", point_meta)
    presentation["meta"]["artifacts"] = {"pointCloud": "pointcloud-cleanroom.lrpc"}
    presentation["meta"]["focusEnvelope"] = {
        "minX": bounds["minX"], "maxX": bounds["maxX"], "minY": bounds["minY"], "maxY": bounds["maxY"],
        "centerX": (bounds["minX"] + bounds["maxX"]) * 0.5,
        "centerY": (bounds["minY"] + bounds["maxY"]) * 0.5,
    }
    _atomic_json(output / "scene-presentation.json", presentation)
    report = {
        "schemaVersion": 1, "kind": "cleanroom-scene-build", "status": "READY_FOR_INDEPENDENT_REVIEW",
        "workspace": str(workspace), "indexFingerprint": index.manifest["indexFingerprint"],
        "wallCount": len(walls), "visualFloorVertexCount": len(floor_polygon), "provisionalItemCount": len(items),
        "posedPhotoCount": len(photos), "authoritySha256": authority_sha,
        "hypothesisSha256": _sha256(output / "scene-hypothesis.json"),
        "presentationSha256": _sha256(output / "scene-presentation.json"),
        "pointcloud": point_meta,
        "gates": {"authorityReview": "NOT_RUN", "visualReview": "NOT_RUN", "oldAnswerComparison": "LOCKED_UNTIL_FREEZE"},
    }
    _atomic_json(output / "cleanroom-build-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--level-receipt", required=True, type=Path)
    parser.add_argument("--transforms", type=Path)
    parser.add_argument("--max-viewer-points", type=int, default=300_000)
    args = parser.parse_args()
    result = build(args.workspace, args.output, args.level_receipt, args.transforms, args.max_viewer_points)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
