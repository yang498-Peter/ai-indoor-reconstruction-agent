#!/usr/bin/env python3
"""Build a leveled clean-room macro scene from raw-derived artifacts only.

The builder deliberately creates a coherent presentation hypothesis before
fine authority review.  It never reads a legacy scene, coordinate list, or
object inventory.  Raw source coordinates remain in the scene graph while a
separate display transform levels the floor for Three.js.
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

import cleanroom_scene_builder as base
from capture_index import CaptureIndex


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plane_z(plane: dict[str, float], x: np.ndarray | float, y: np.ndarray | float):
    return float(plane["a"]) * x + float(plane["b"]) * y + float(plane["c"])


def _dominant_basis(proposals: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, list[float]]:
    families = proposals.get("axisFamilies", [])
    if not families:
        raise ValueError("structural proposals contain no axis family")
    primary = math.radians(float(families[0]["angleDeg"])) % math.pi
    secondary_candidates = [
        math.radians(float(item["angleDeg"])) % math.pi
        for item in families[1:]
        if math.radians(65.0) <= base._angle_delta(
            primary, math.radians(float(item["angleDeg"])) % math.pi
        ) <= math.radians(115.0)
    ]
    secondary = secondary_candidates[0] if secondary_candidates else (primary + math.pi / 2) % math.pi
    u = np.asarray([math.cos(primary), math.sin(primary)], dtype=np.float64)
    v = np.asarray([-u[1], u[0]], dtype=np.float64)
    if float(np.dot(v, np.asarray([math.cos(secondary), math.sin(secondary)]))) < 0:
        v = -v
    return u, v, [primary, math.atan2(float(v[1]), float(v[0])) % math.pi]


def _main_floor_rectangle(
    index: CaptureIndex,
    floor_plane: dict[str, float],
    u: np.ndarray,
    v: np.ndarray,
    output: Path,
    cell: float = 0.12,
) -> tuple[list[list[float]], dict[str, float]]:
    bounds = index.manifest["bounds"]
    corners = [
        (bounds["minX"], bounds["minY"]), (bounds["minX"], bounds["maxY"]),
        (bounds["maxX"], bounds["minY"]), (bounds["maxX"], bounds["maxY"]),
    ]
    floor_values = [_plane_z(floor_plane, x, y) for x, y in corners]
    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / 3_000_000)))
    query = index.query_all(
        z_min=min(floor_values) - 0.10,
        z_max=max(floor_values) + 0.24,
        every=stride,
    )
    residual = query.z - _plane_z(floor_plane, query.x, query.y)
    keep = (residual >= -0.065) & (residual <= 0.16)
    x, y = query.x[keep], query.y[keep]
    if x.size < 500:
        raise ValueError("too few plane-relative floor points")

    min_x, max_x = np.percentile(x, [0.25, 99.75])
    min_y, max_y = np.percentile(y, [0.25, 99.75])
    width = max(1, int(math.ceil((max_x - min_x) / cell)) + 1)
    height = max(1, int(math.ceil((max_y - min_y) / cell)) + 1)
    col = np.clip(((x - min_x) / cell).astype(np.int32), 0, width - 1)
    row = np.clip(((max_y - y) / cell).astype(np.int32), 0, height - 1)
    counts = np.zeros((height, width), dtype=np.uint16)
    np.add.at(counts, (row, col), 1)
    mask = (counts >= 2).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if component_count <= 1:
        raise ValueError("floor support has no connected component")
    component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    main = (labels == component).astype(np.uint8) * 255
    main = cv2.morphologyEx(main, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    contours, _ = cv2.findContours(main, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contour = max(contours, key=cv2.contourArea)[:, 0, :]
    source = np.column_stack((min_x + contour[:, 0] * cell, max_y - contour[:, 1] * cell))
    component_u = source @ u
    component_v = source @ v
    # A real office floor can be split into several dense components by desks,
    # glass, door thresholds and sparse scanning.  The single largest component
    # is useful for rejecting thin exterior rays, but it must not crop the two
    # occupied wings.  Robust point quantiles recover those wings while still
    # discarding the very sparse lines visible outside the building.
    all_plan = np.column_stack((x, y))
    all_u = all_plan @ u
    all_v = all_plan @ v
    u0, u1 = np.percentile(all_u, [1.0, 99.0])
    v0, v1 = np.percentile(all_v, [1.0, 99.0])
    component_bounds = {
        "uMin": float(np.min(component_u)), "uMax": float(np.max(component_u)),
        "vMin": float(np.min(component_v)), "vMax": float(np.max(component_v)),
    }
    u0 = min(float(u0), component_bounds["uMin"])
    u1 = max(float(u1), component_bounds["uMax"])
    v0 = min(float(v0), component_bounds["vMin"])
    v1 = max(float(v1), component_bounds["vMax"])
    pad = 0.18
    u0, u1, v0, v1 = float(u0 - pad), float(u1 + pad), float(v0 - pad), float(v1 + pad)
    polygon = [
        (u * u0 + v * v0).tolist(),
        (u * u1 + v * v0).tolist(),
        (u * u1 + v * v1).tolist(),
        (u * u0 + v * v1).tolist(),
    ]
    debug = cv2.cvtColor(main, cv2.COLOR_GRAY2BGR)
    cv2.imwrite(str(output / "evidence" / "cleanroom-floor-support.png"), debug)
    return [[round(float(a), 5), round(float(b), 5)] for a, b in polygon], {
        "uMin": u0, "uMax": u1, "vMin": v0, "vMax": v1,
        "width": u1 - u0, "depth": v1 - v0,
        "supportPointCount": int(x.size), "componentCellCount": int(stats[component, cv2.CC_STAT_AREA]),
        "largestComponentBounds": component_bounds,
    }


def _interval_walls(
    proposals: dict[str, Any],
    u: np.ndarray,
    v: np.ndarray,
    axes: list[float],
    envelope: dict[str, float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    basis = [u, v]
    normal = [v, -u]
    for candidate in proposals.get("wallCandidates", []):
        mode = candidate.get("wallMode")
        confidence = float(candidate.get("confidence", 0.0))
        residual = float(candidate.get("fitResidualP90M", 1.0))
        length = float(candidate.get("lengthM", 0.0))
        support = int(candidate.get("supportPointCount", 0))
        paired_ok = mode == "paired-faces" and confidence >= 0.70 and residual <= 0.09 and length >= 0.80
        single_ok = mode == "single-face" and confidence >= 0.74 and residual <= 0.07 and length >= 1.40 and support >= 900
        if not (paired_ok or single_ok):
            continue
        line = candidate.get("rawCenterline")
        start = np.asarray(line["start"], dtype=np.float64)
        end = np.asarray(line["end"], dtype=np.float64)
        vector = end - start
        angle = math.atan2(float(vector[1]), float(vector[0])) % math.pi
        family = min(range(2), key=lambda index: base._angle_delta(angle, axes[index]))
        if base._angle_delta(angle, axes[family]) > math.radians(4.0):
            continue
        mid = (start + end) * 0.5
        mid_u, mid_v = float(np.dot(mid, u)), float(np.dot(mid, v))
        if not (envelope["uMin"] - 0.35 <= mid_u <= envelope["uMax"] + 0.35):
            continue
        if not (envelope["vMin"] - 0.35 <= mid_v <= envelope["vMax"] + 0.35):
            continue
        axis = basis[family]
        axis_normal = normal[family]
        along = sorted((float(np.dot(start, axis)), float(np.dot(end, axis))))
        records.append({
            "candidate": candidate, "family": family, "axis": axis, "normal": axis_normal,
            "offset": float(np.dot(mid, axis_normal)), "start": along[0], "end": along[1],
        })

    groups: list[list[dict[str, Any]]] = []
    for record in sorted(records, key=lambda item: (item["family"], item["offset"], item["start"])):
        target = next((group for group in reversed(groups)
                       if group[0]["family"] == record["family"]
                       and abs(float(np.median([item["offset"] for item in group])) - record["offset"]) <= 0.16), None)
        if target is None:
            target = []
            groups.append(target)
        target.append(record)

    walls: list[dict[str, Any]] = []
    for group in groups:
        intervals = sorted(group, key=lambda item: item["start"])
        batches: list[list[dict[str, Any]]] = []
        for record in intervals:
            if not batches or record["start"] - max(item["end"] for item in batches[-1]) > 0.14:
                batches.append([record])
            else:
                batches[-1].append(record)
        for batch in batches:
            begin = min(item["start"] for item in batch)
            finish = max(item["end"] for item in batch)
            if finish - begin < 0.85:
                continue
            paired = [item for item in batch if item["candidate"].get("wallMode") == "paired-faces"]
            if not paired and len(batch) < 2:
                continue
            weights = np.asarray([max(1, int(item["candidate"].get("supportPointCount", 1))) for item in batch])
            offset = float(np.average([item["offset"] for item in batch], weights=weights))
            axis, axis_normal = batch[0]["axis"], batch[0]["normal"]
            start = axis * begin + axis_normal * offset
            end = axis * finish + axis_normal * offset
            walls.append({
                "start": start, "end": end,
                "thickness": float(np.clip(np.median([
                    float(item["candidate"].get("thicknessM", 0.12)) for item in batch
                ]), 0.08, 0.28)),
                "confidence": max(float(item["candidate"].get("confidence", 0.0)) for item in batch),
                "residualP90M": min(float(item["candidate"].get("fitResidualP90M", 1.0)) for item in batch),
                "supportPointCount": int(sum(int(item["candidate"].get("supportPointCount", 0)) for item in batch)),
                "sourceProposalIds": sorted({str(item["candidate"]["id"]) for item in batch}),
                "pairedFaceSupport": bool(paired), "role": "interior-candidate",
            })
    walls.sort(key=lambda item: (-item["pairedFaceSupport"], -item["supportPointCount"],
                                 -float(np.linalg.norm(item["end"] - item["start"]))))
    return walls[:36]


def _shell_walls(polygon: list[list[float]]) -> list[dict[str, Any]]:
    result = []
    for index, start_raw in enumerate(polygon):
        start = np.asarray(start_raw, dtype=np.float64)
        end = np.asarray(polygon[(index + 1) % len(polygon)], dtype=np.float64)
        result.append({
            "start": start, "end": end, "thickness": 0.16, "confidence": 0.68,
            "residualP90M": 0.18, "supportPointCount": 0,
            "sourceProposalIds": [f"floor-envelope-edge-{index + 1}"],
            "pairedFaceSupport": False, "role": "scan-bounded-shell",
        })
    return result


def _author_walls(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    walls: list[dict[str, Any]] = []
    for segment in payload.get("segments", []):
        start = np.asarray(segment["start"], dtype=np.float64)
        end = np.asarray(segment["end"], dtype=np.float64)
        wall = {
            "start": start, "end": end, "thickness": float(segment["thickness"]),
            "height": float(segment["height"]), "confidence": float(segment["confidence"]),
            "residualP90M": float(segment.get("residualP90M", 0.10)),
            "supportPointCount": int(segment.get("supportPointCount", 0)),
            "sourceProposalIds": [str(value) for value in segment.get(
                "sourceProposalIds", [segment["id"]]
            )],
            "pairedFaceSupport": bool(segment.get("pairedFaceSupport", False)),
            "role": str(segment["role"]), "wallKind": str(segment["wallKind"]),
            "authorityStatus": str(segment["authorityStatus"]),
            "presentationStatus": str(segment["presentationStatus"]),
            "evidence": segment["evidence"], "openings": segment.get("openings", []),
            "frameSpacingM": segment.get("frameSpacingM"),
        }
        if segment.get("inferenceReason") is not None:
            wall["inferenceReason"] = segment["inferenceReason"]
        if segment.get("confidenceIntervalM") is not None:
            wall["confidenceIntervalM"] = segment["confidenceIntervalM"]
        walls.append(wall)
    return walls, payload


def _room_dividers(
    proposals: dict[str, Any],
    authored: list[dict[str, Any]],
    u: np.ndarray,
    v: np.ndarray,
    envelope: dict[str, float],
    evidence_path: Path,
    evidence_rel: str | None = None,
    root_sha: str | None = None,
) -> list[dict[str, Any]]:
    glass = next((wall for wall in authored if wall.get("wallKind") == "glass"), None)
    if glass is None:
        return []
    glass_v = float(np.dot((glass["start"] + glass["end"]) * 0.5, v))
    if glass_v - envelope["vMin"] < 2.0:
        return []
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for candidate in proposals.get("wallCandidates", []):
        if float(candidate.get("confidence", 0.0)) < 0.64:
            continue
        if float(candidate.get("fitResidualP90M", 1.0)) > 0.10:
            continue
        line = candidate.get("rawCenterline")
        start = np.asarray(line["start"], dtype=np.float64)
        end = np.asarray(line["end"], dtype=np.float64)
        vector = end - start
        angle = math.atan2(float(vector[1]), float(vector[0])) % math.pi
        v_angle = math.atan2(float(v[1]), float(v[0])) % math.pi
        if base._angle_delta(angle, v_angle) > math.radians(5.0):
            continue
        midpoint = (start + end) * 0.5
        center_u, center_v = float(np.dot(midpoint, u)), float(np.dot(midpoint, v))
        if not (envelope["uMin"] + 0.6 <= center_u <= envelope["uMax"] - 0.6):
            continue
        if not (envelope["vMin"] - 0.4 <= center_v <= glass_v + 0.5):
            continue
        candidates.append((center_u, float(candidate.get("confidence", 0.0)), candidate))
    selected: list[tuple[float, dict[str, Any]]] = []
    for center_u, _confidence, candidate in sorted(candidates, key=lambda item: (-item[1], item[0])):
        if any(abs(center_u - existing_u) < 1.35 for existing_u, _ in selected):
            continue
        selected.append((center_u, candidate))
    result: list[dict[str, Any]] = []
    for center_u, candidate in sorted(selected, key=lambda item: item[0]):
        start = u * center_u + v * envelope["vMin"]
        end = u * center_u + v * glass_v
        result.append({
            "start": start, "end": end, "thickness": float(np.clip(candidate.get("thicknessM", 0.12), 0.09, 0.18)),
            "height": 3.09, "confidence": min(0.72, float(candidate.get("confidence", 0.0))),
            "residualP90M": float(candidate.get("fitResidualP90M", 0.1)),
            "supportPointCount": int(candidate.get("supportPointCount", 0)),
            "sourceProposalIds": [str(candidate["id"])], "pairedFaceSupport": candidate.get("wallMode") == "paired-faces",
            "role": "north-room-divider", "wallKind": "solid",
            "authorityStatus": "candidate", "presentationStatus": "accepted-inferred",
            "evidence": {
                "path": evidence_rel or "../geometry-workspace/evidence/band-walls.png",
                "sha256": _sha256(evidence_path),
                "observation": "Raw perpendicular return inside the repeated north room band; endpoints are topology completion.",
                "producer": "indexed-pointcloud-evidence",
                "rootContentSha256s": [root_sha] if root_sha else None,
            },
            "openings": [], "frameSpacingM": None,
        })
    return result[:12]


def _panorama_room_band(
    payload: dict[str, Any],
    authored: list[dict[str, Any]],
    proposals: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    config = payload.get("panoramaRoomBand") or {}
    if config.get("enabled") is not True:
        return authored, False
    source_role = str(config.get("sourceRole", "north-room-glass-spine"))
    facade = next((wall for wall in authored if wall.get("role") == source_role), None)
    if facade is None:
        raise ValueError(f"panoramaRoomBand source role not found: {source_role}")
    start, end = np.asarray(facade["start"], dtype=np.float64), np.asarray(facade["end"], dtype=np.float64)
    length = float(np.linalg.norm(end - start))
    direction = (end - start) / length
    normal = np.asarray([-direction[1], direction[0]], dtype=np.float64)
    if str(config.get("roomSide", "left")) == "right":
        normal *= -1.0
    pier_width = float(config.get("pierWidthM", 0.34))
    panorama = config["evidence"]
    completion = config.get("coherentCompletion") or {}
    complete_rooms = completion.get("enabled") is True

    def record(a: np.ndarray, b: np.ndarray, *, role: str, kind: str, thickness: float,
               height: float, confidence: float, frame_spacing: float | None = None,
               presentation_status: str = "accepted-inferred",
               inference_reason: str | None = None,
               confidence_interval: list[float] | None = None) -> dict[str, Any]:
        return {
            "start": a, "end": b, "thickness": thickness,
            "confidence": confidence, "residualP90M": float(facade.get("residualP90M", 0.18)),
            "supportPointCount": int(facade.get("supportPointCount", 0)),
            "pairedFaceSupport": False, "sourceProposalIds": ["panorama-room-band"],
            "role": role, "wallKind": kind, "height": height,
            "authorityStatus": "candidate", "presentationStatus": presentation_status,
            "inferenceReason": inference_reason or (
                "panorama-visible construction family aligned to current-capture structural proposals"
            ),
            "confidenceIntervalM": confidence_interval or [0.08, 0.24],
            "evidence": {
                "path": panorama["path"], "sha256": panorama["sha256"],
                "observation": panorama["observation"],
            },
            "openings": [], "frameSpacingM": frame_spacing,
        }

    candidate_by_id = {str(item["id"]): item for item in proposals.get("wallCandidates", [])}
    divider_specs: list[tuple[float, np.ndarray, np.ndarray, dict[str, Any]]] = []
    for proposal_id in config.get("dividerProposalIds", []):
        candidate = candidate_by_id.get(str(proposal_id))
        if candidate is None:
            raise ValueError(f"panorama divider proposal not found: {proposal_id}")
        divider_start = np.asarray(candidate["suggestedCenterline"]["start"], dtype=np.float64)
        divider_end = np.asarray(candidate["suggestedCenterline"]["end"], dtype=np.float64)
        if abs(float(np.dot(divider_end - start, normal))) < abs(float(np.dot(divider_start - start, normal))):
            divider_start, divider_end = divider_end, divider_start
        offset = float(np.dot(divider_start - start, direction))
        divider_specs.append((offset, divider_start, divider_end, candidate))
    divider_specs.sort(key=lambda item: item[0])

    result = [wall for wall in authored if wall is not facade]
    gaps = [spec[0] for spec in divider_specs if pier_width < spec[0] < length - pier_width]
    cursor = 0.0
    for index, offset in enumerate(gaps + [length], 1):
        span_end = length if offset == length else offset - pier_width / 2.0
        if span_end - cursor >= 0.5:
            result.append(record(
                start + direction * cursor, start + direction * span_end,
                role=f"panorama-glass-span-{index}", kind="glass", thickness=0.06,
                height=2.85, confidence=0.84, frame_spacing=1.35,
            ))
        if offset != length:
            pier_start = start + direction * (offset - pier_width / 2.0)
            pier_end = start + direction * (offset + pier_width / 2.0)
            result.append(record(
                pier_start, pier_end, role=f"panorama-opaque-pier-{index}", kind="solid",
                thickness=0.16, height=3.09, confidence=0.72,
            ))
            _, divider_start, divider_end, candidate = divider_specs[index - 1]
            divider = record(
                divider_start, divider_end,
                role=f"panorama-rear-divider-{index}", kind="solid", thickness=0.12,
                height=3.09, confidence=float(candidate.get("confidence", 0.55)),
                presentation_status=str(completion.get("rawDividerStatus", "candidate")) if complete_rooms else "candidate",
            )
            divider["sourceProposalIds"] = [str(candidate["id"])]
            divider["residualP90M"] = float(candidate.get("fitResidualP90M", 0.18))
            divider["supportPointCount"] = int(candidate.get("supportPointCount", 0))
            result.append(divider)
            cursor = offset + pier_width / 2.0
    for index, proposal_id in enumerate(config.get("rearProposalIds", []), 1):
        candidate = candidate_by_id.get(str(proposal_id))
        if candidate is None:
            raise ValueError(f"panorama rear proposal not found: {proposal_id}")
        rear = record(
            np.asarray(candidate["suggestedCenterline"]["start"], dtype=np.float64),
            np.asarray(candidate["suggestedCenterline"]["end"], dtype=np.float64),
            role=f"panorama-rear-glazing-{index}", kind="glass", thickness=0.06,
            height=2.85, confidence=float(candidate.get("confidence", 0.55)),
            presentation_status=str(completion.get("rawRearStatus", "candidate")) if complete_rooms else "candidate",
        )
        rear["sourceProposalIds"] = [str(candidate["id"])]
        rear["residualP90M"] = float(candidate.get("fitResidualP90M", 0.18))
        rear["supportPointCount"] = int(candidate.get("supportPointCount", 0))
        result.append(rear)
    if complete_rooms:
        rear_samples: list[float] = []
        for proposal_id in config.get("rearProposalIds", []):
            candidate = candidate_by_id[str(proposal_id)]
            line = candidate["suggestedCenterline"]
            for point in (line["start"], line["end"]):
                rear_samples.append(float(np.dot(np.asarray(point, dtype=np.float64) - start, normal)))
        configured_depth = completion.get("rearDepthM")
        rear_depth = float(configured_depth) if configured_depth is not None else float(np.median(rear_samples))
        rear_depth = float(np.clip(rear_depth, 1.8, 5.5))
        completion_reason = (
            "AI coherent-room completion: a robust rear-plane offset from raw rear fragments is extended "
            "across scan gaps; this is presentation inference, not measured authority geometry"
        )
        rear_wall = record(
            start + normal * rear_depth, end + normal * rear_depth,
            role="panorama-inferred-rear-envelope", kind=str(completion.get("rearWallKind", "glass")),
            thickness=float(completion.get("rearThicknessM", 0.10)), height=3.09,
            confidence=float(completion.get("confidence", 0.67)), frame_spacing=1.35,
            presentation_status=str(completion.get("rearEnvelopeStatus", "candidate")),
            inference_reason=completion_reason, confidence_interval=[0.25, 0.45],
        )
        rear_wall["sourceProposalIds"] = [str(value) for value in config.get("rearProposalIds", [])]
        result.append(rear_wall)

        internal_offsets = sorted({
            float(np.clip(spec[0], 0.0, length)) for spec in divider_specs
            if pier_width < spec[0] < length - pier_width
        })
        for index, offset in enumerate(internal_offsets, 1):
            completed = record(
                start + direction * offset,
                start + direction * offset + normal * rear_depth,
                role=f"panorama-inferred-full-divider-{index}", kind="solid", thickness=0.12,
                height=3.09, confidence=float(completion.get("dividerConfidence", 0.64)),
                inference_reason=completion_reason, confidence_interval=[0.20, 0.42],
            )
            nearest = min(divider_specs, key=lambda spec: abs(spec[0] - offset))
            completed["sourceProposalIds"] = [str(nearest[3]["id"])]
            result.append(completed)

        if completion.get("closeBandEnds", True):
            for role, offset in (("west", 0.0), ("east", length)):
                result.append(record(
                    start + direction * offset,
                    start + direction * offset + normal * rear_depth,
                    role=f"panorama-inferred-{role}-sidewall", kind="solid", thickness=0.14,
                    height=3.09, confidence=float(completion.get("sidewallConfidence", 0.58)),
                    presentation_status=str(completion.get("sidewallStatus", "candidate")),
                    inference_reason=completion_reason, confidence_interval=[0.28, 0.55],
                ))
    return result, True


def _furniture_items(
    index: CaptureIndex,
    floor_plane: dict[str, float],
    u: np.ndarray,
    v: np.ndarray,
    envelope: dict[str, float],
    output: Path,
    cell: float = 0.08,
) -> list[dict[str, Any]]:
    polygon_uv = np.asarray([
        [envelope["uMin"], envelope["vMin"]], [envelope["uMax"], envelope["vMin"]],
        [envelope["uMax"], envelope["vMax"]], [envelope["uMin"], envelope["vMax"]],
    ])
    source = polygon_uv[:, :1] * u + polygon_uv[:, 1:] * v
    min_x, min_y = np.min(source, axis=0)
    max_x, max_y = np.max(source, axis=0)
    # The tabletop band lives 0.66-0.88 m above the floor PLANE, so the raw
    # z window must be derived from the plane over the query rectangle; an
    # absolute window silently returns nothing when the vertical datum moves.
    band_lo, band_hi = 0.66, 0.88
    corner_z = [
        _plane_z(floor_plane, corner_x, corner_y)
        for corner_x in (float(min_x), float(max_x))
        for corner_y in (float(min_y), float(max_y))
    ]
    query = index.query_bbox(
        min_x, min_y, max_x, max_y,
        z_min=min(corner_z) + band_lo - 0.05,
        z_max=max(corner_z) + band_hi + 0.05,
        every=2,
    )
    height = query.z - _plane_z(floor_plane, query.x, query.y)
    keep = (height >= band_lo) & (height <= band_hi)
    plan = np.column_stack((query.x[keep], query.y[keep]))
    if plan.shape[0] < 100:
        return []
    local_u, local_v = plan @ u, plan @ v
    width = int(math.ceil((envelope["uMax"] - envelope["uMin"]) / cell)) + 1
    depth = int(math.ceil((envelope["vMax"] - envelope["vMin"]) / cell)) + 1
    col = np.clip(((local_u - envelope["uMin"]) / cell).astype(np.int32), 0, width - 1)
    row = np.clip(((envelope["vMax"] - local_v) / cell).astype(np.int32), 0, depth - 1)
    counts = np.zeros((depth, width), dtype=np.uint16)
    np.add.at(counts, (row, col), 1)
    mask = (counts >= 2).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    debug = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    items: list[dict[str, Any]] = []
    for label in range(1, component_count):
        if int(stats[label, cv2.CC_STAT_AREA]) < 18:
            continue
        rows, cols = np.nonzero(labels == label)
        occupied = np.column_stack((
            envelope["uMin"] + cols * cell,
            envelope["vMax"] - rows * cell,
        ))
        center_uv = np.mean(occupied, axis=0)
        covariance = np.cov(occupied - center_uv, rowvar=False)
        values, vectors = np.linalg.eigh(covariance)
        long_axis = vectors[:, int(np.argmax(values))]
        short_axis = np.asarray([-long_axis[1], long_axis[0]])
        long_projection = occupied @ long_axis
        short_projection = occupied @ short_axis
        long_size = float(np.max(long_projection) - np.min(long_projection) + cell)
        short_size = float(np.max(short_projection) - np.min(short_projection) + cell)
        if long_size < short_size:
            long_size, short_size = short_size, long_size
            long_axis = short_axis
        if not (0.62 <= long_size <= 5.2 and 0.48 <= short_size <= 2.2):
            continue
        if long_size * short_size > 8.5:
            continue
        angle_local = math.atan2(float(long_axis[1]), float(long_axis[0]))
        nearest = min((0.0, math.pi / 2), key=lambda angle: base._angle_delta(angle_local % math.pi, angle))
        if base._angle_delta(angle_local % math.pi, nearest) <= math.radians(12.0):
            angle_local = nearest
        source_direction = u * math.cos(angle_local) + v * math.sin(angle_local)
        yaw = math.atan2(float(source_direction[1]), float(source_direction[0]))
        center_source = u * center_uv[0] + v * center_uv[1]
        item = {
            "center": [round(float(center_source[0]), 5), round(float(center_source[1]), 5)],
            "yaw": yaw, "size": [round(long_size, 3), 0.74, round(short_size, 3)],
            "category": "workstation" if long_size >= 2.2 else "table",
            "confidence": 0.58, "supportCells": int(stats[label, cv2.CC_STAT_AREA]),
        }
        items.append(item)
        box_local = np.asarray([
            center_uv + long_axis * long_size / 2 + np.asarray([-long_axis[1], long_axis[0]]) * short_size / 2,
            center_uv - long_axis * long_size / 2 + np.asarray([-long_axis[1], long_axis[0]]) * short_size / 2,
            center_uv - long_axis * long_size / 2 - np.asarray([-long_axis[1], long_axis[0]]) * short_size / 2,
            center_uv + long_axis * long_size / 2 - np.asarray([-long_axis[1], long_axis[0]]) * short_size / 2,
        ])
        box_px = np.column_stack((
            (box_local[:, 0] - envelope["uMin"]) / cell,
            (envelope["vMax"] - box_local[:, 1]) / cell,
        )).astype(np.int32)
        cv2.polylines(debug, [box_px], True, (0, 180, 255), 2, cv2.LINE_AA)
    cv2.imwrite(str(output / "evidence" / "furniture-support.png"), debug)
    return sorted(items, key=lambda item: (item["center"][1], item["center"][0]))[:28]


def _furniture_evidence_sources(
    furniture_sha: str,
    picks_payload: dict[str, Any] | None,
    root_sha: str,
) -> list[dict[str, Any]]:
    """Furniture evidence comes only from real inputs.

    The raw height-band support raster always exists; a corroborating photo is
    included only when the author picks actually supply one.  A missing photo
    means the source is omitted, never substituted with a dataset-specific
    placeholder.
    """
    sources = [base._evidence_source(
        "tabletop-height-support", "evidence/furniture-support.png", furniture_sha,
        "cleanroom-macro-builder", [root_sha],
        generator_parameters={"artifact": "furniture-support"},
        note="instance position is a raw height-band proposal; not a measured furniture receipt",
    )]
    photo = (picks_payload or {}).get("furnitureEvidence")
    if isinstance(photo, dict) and photo.get("path") and photo.get("sha256"):
        sources.append(base._evidence_source(
            "posed-photo-family", str(photo["path"]), str(photo["sha256"]),
            "author-picks",
            note=str(photo.get("note") or
                     "photo proves a repeated furniture family, not this exact instance"),
        ))
    return sources


def _north_room_topology(walls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    role_to_id = {
        wall["role"]: f"wall_clean{index:03d}" for index, wall in enumerate(walls, 1)
        if wall.get("presentationStatus") == "accepted-inferred"
    }
    front_roles = sorted(
        (role for role in role_to_id if role.startswith("panorama-glass-span-")),
        key=lambda role: int(role.rsplit("-", 1)[1]),
    )
    divider_roles = ["panorama-inferred-west-sidewall"] + sorted(
        (role for role in role_to_id if role.startswith("panorama-inferred-full-divider-")),
        key=lambda role: int(role.rsplit("-", 1)[1]),
    ) + ["panorama-inferred-east-sidewall"]
    rear_role = "panorama-inferred-rear-envelope"
    if rear_role not in role_to_id or len(divider_roles) != len(front_roles) + 1:
        return []
    if any(role not in role_to_id for role in divider_roles):
        return []
    return [{
        "id": f"north_room_{index:02d}",
        "boundaryNodeIds": [
            role_to_id[front_role], role_to_id[divider_roles[index]],
            role_to_id[rear_role], role_to_id[divider_roles[index - 1]],
        ],
        "status": "accepted-inferred", "confidence": 0.62,
        "reason": "coherent panorama room-band completion; boundary depth remains inferred",
    } for index, front_role in enumerate(front_roles, 1)]


def _conservative_display_items(
    proposals: list[dict[str, Any]],
    walls: list[dict[str, Any]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in sorted(proposals, key=lambda value: int(value.get("supportCells", 0)), reverse=True):
        long_size, _, short_size = item["size"]
        if long_size > 2.5 or short_size > 1.2:
            continue
        center = np.asarray(item["center"], dtype=np.float64)
        radius = 0.5 * math.hypot(float(long_size), float(short_size))
        intersects = False
        for wall in walls:
            start, end = np.asarray(wall["start"]), np.asarray(wall["end"])
            segment = end - start
            denominator = float(np.dot(segment, segment))
            fraction = 0.0 if denominator <= 1e-9 else float(np.clip(np.dot(center - start, segment) / denominator, 0.0, 1.0))
            distance = float(np.linalg.norm(center - (start + segment * fraction)))
            if distance <= radius + float(wall["thickness"]) / 2.0 + 0.12:
                intersects = True
                break
        if not intersects:
            kept.append(item)
        if len(kept) >= limit:
            break
    return sorted(kept, key=lambda item: (item["center"][1], item["center"][0]))


def _export_lrpc(
    index: CaptureIndex,
    output: Path,
    polygon: list[list[float]],
    envelope: dict[str, float],
    u: np.ndarray,
    v: np.ndarray,
    floor_plane: dict[str, float],
    center: np.ndarray,
    max_points: int,
) -> dict[str, Any]:
    source = np.asarray(polygon, dtype=np.float64)
    min_x, min_y = np.min(source, axis=0) - 0.8
    max_x, max_y = np.max(source, axis=0) + 0.8
    indexed = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed / max_points)))
    # Export band is plane-relative (-0.18..3.45 m over the floor plane); the
    # raw z window is derived from the plane so a shifted vertical datum keeps
    # the full room instead of silently clipping it.
    height_lo, height_hi = -0.18, 3.45
    corner_z = [
        _plane_z(floor_plane, corner_x, corner_y)
        for corner_x in (float(min_x), float(max_x))
        for corner_y in (float(min_y), float(max_y))
    ]
    query = index.query_bbox(
        min_x, min_y, max_x, max_y,
        z_min=min(corner_z) + height_lo - 0.10,
        z_max=max(corner_z) + height_hi + 0.10,
        every=stride,
    )
    plan = np.column_stack((query.x, query.y))
    along_u, along_v = plan @ u, plan @ v
    height = query.z - _plane_z(floor_plane, query.x, query.y)
    keep = (
        (along_u >= envelope["uMin"] - 0.6) & (along_u <= envelope["uMax"] + 0.6)
        & (along_v >= envelope["vMin"] - 0.6) & (along_v <= envelope["vMax"] + 0.6)
        & (height >= height_lo) & (height <= height_hi)
    )
    x, y, h, rgb = query.x[keep], query.y[keep], height[keep], query.rgb[keep]
    payload = bytearray(struct.pack("<4sIII", b"LRPC", 1, int(x.size), 16))
    for px, py, pz, color in zip(x, y, h, rgb):
        payload.extend(struct.pack(
            "<fffBBBB", float(px - center[0]), float(pz), float(-(py - center[1])),
            int(color[0]), int(color[1]), int(color[2]), 0,
        ))
    _atomic_bytes(output, bytes(payload))
    return {
        "format": "LRPC", "version": 1, "stride": 16, "pointCount": int(x.size),
        "sourceSamplingStride": stride, "sha256": _sha256(output), "displayLeveled": True,
    }


def build(
    workspace: Path,
    output: Path,
    survey_path: Path,
    transforms_path: Path | None,
    author_picks_path: Path | None,
    max_points: int,
) -> dict[str, Any]:
    workspace, output = workspace.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence").mkdir(exist_ok=True)
    proposals_path = workspace / "structural-proposals.json"
    evidence_path = workspace / "evidence" / "band-walls.png"
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    survey = json.loads(survey_path.read_text(encoding="utf-8"))
    floor_plane, ceiling_plane = survey["floor"], survey["ceiling"]
    index = CaptureIndex.open(workspace / "capture-index", validate_source=True)
    if proposals.get("indexFingerprint") != index.manifest.get("indexFingerprint"):
        raise ValueError("structural proposals and CaptureIndex fingerprints differ")
    if survey["source"]["indexFingerprint"] != index.manifest.get("indexFingerprint"):
        raise ValueError("level survey and CaptureIndex fingerprints differ")

    u, v, axes = _dominant_basis(proposals)
    root_sha = base._root_content_sha256(index)
    evidence_rel = base._relative_scene_path(evidence_path, output)
    proposals_rel = base._relative_scene_path(proposals_path, output)
    survey_rel = base._relative_scene_path(survey_path, output)
    floor_polygon, envelope = _main_floor_rectangle(index, floor_plane, u, v, output)
    center = np.mean(np.asarray(floor_polygon, dtype=np.float64), axis=0)
    withheld_shell = _shell_walls(floor_polygon)
    picks_payload: dict[str, Any] | None = None
    if author_picks_path is not None:
        interiors, picks_payload = _author_walls(author_picks_path)
        if picks_payload.get("indexFingerprint") != index.manifest["indexFingerprint"]:
            raise ValueError("author picks and CaptureIndex fingerprints differ")
        interiors, panorama_band_enabled = _panorama_room_band(picks_payload, interiors, proposals)
        dividers = (_room_dividers(proposals, interiors, u, v, envelope, evidence_path,
                                   evidence_rel=evidence_rel, root_sha=root_sha)
                    if picks_payload.get("allowInferredRoomDividers") is True else [])
        interiors.extend(dividers)
    else:
        panorama_band_enabled = False
        dividers = []
        interiors = _interval_walls(proposals, u, v, axes, envelope)
    walls = interiors
    furniture_proposals = _furniture_items(index, floor_plane, u, v, envelope, output)
    items: list[dict[str, Any]] = []
    photos = base._load_photos(transforms_path)
    source_meta = {
        "samplePointCount": int(index.manifest["inputPointCount"]),
        "inputPointCount": int(index.manifest["inputPointCount"]),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "cleanroomPolicy": "raw LAS, raw photos and raw poses only; legacy answers excluded",
        "floorPlane": floor_plane, "ceilingPlane": ceiling_plane,
    }
    authority, hypothesis, presentation = base._scene_layers(
        base._dataset_id(index), walls, floor_polygon, items,
        float(floor_plane["c"]), float(ceiling_plane["c"]), source_meta,
        evidence_rel, _sha256(evidence_path), _sha256(proposals_path), photos,
        proposal_path=proposals_rel, root_sha256=root_sha,
    )
    pipeline = [
        {"id": "intake", "label": "Clean-room raw capture", "status": "PASS"},
        {"id": "evidence", "label": "Indexed raw evidence", "status": "PASS"},
        {"id": "macro-hypothesis", "label": "Leveled macro hypothesis", "status": "REVIEW"},
        {"id": "author", "label": "Walls, openings and furniture", "status": "REVIEW"},
        {"id": "presentation-review", "label": "Live presentation review", "status": "REVIEW"},
        {"id": "global-review", "label": "Independent global review", "status": "NOT_RUN"},
    ]
    for scene in (authority, hypothesis, presentation):
        scene["coordinateFrame"]["sourceToDisplay"] = (
            "display_x=x-centerX; display_y=z-(floor.a*x+floor.b*y+floor.c); "
            "display_z=-(y-centerY)"
        )
        scene["meta"]["displayOffset"] = [round(float(center[0]), 6), round(float(-center[1]), 6)]
        scene["meta"]["pipeline"] = pipeline
        scene["meta"]["focusEnvelope"] = {
            "width": round(float(envelope["width"]), 4),
            "depth": round(float(envelope["depth"]), 4),
            "centerX": round(float(center[0]), 6), "centerY": round(float(center[1]), 6),
        }
        scene["meta"]["cleanroomStage"] = "MACRO_HYPOTHESIS_WIP"
        if scene is not authority:
            scene["nodes"]["slab_visual01"]["name"] = "Approximate leveled presentation floor"
            scene["nodes"]["slab_visual01"]["inference"]["inferenceReason"] = (
                "horizontal presentation envelope requested by the user; not an authority slab boundary"
            )
            scene["evidence"]["slab_visual01"]["status"] = "accepted-inferred"
            scene["evidence"]["slab_visual01"]["sources"] = [
                base._evidence_source(
                    "floor-support", "evidence/cleanroom-floor-support.png",
                    _sha256(output / "evidence" / "cleanroom-floor-support.png"),
                    "cleanroom-macro-builder", [root_sha],
                    generator_parameters={"artifact": "floor-support"},
                ),
                base._evidence_source(
                    "level-survey", survey_rel, _sha256(survey_path),
                    "level-survey", [root_sha],
                ),
            ]
            scene["evidence"]["slab_visual01"]["reason"] = (
                "display context only; source floor plane and sparse-support caveat remain preserved"
            )
        scene["review"]["issues"].append({
            "id": "cr-live-001", "severity": "P1", "status": "OPEN",
            "summary": "Interior walls, openings and furniture require regional review",
        })
        for wall_index in range(1, len(walls) + 1):
            node = scene["nodes"][f"wall_clean{wall_index:03d}"]
            wall = walls[wall_index - 1]
            node["meta"]["role"] = wall["role"]
            node["wallKind"] = wall.get("wallKind", "solid")
            node["height"] = round(float(wall.get("height", 3.09)), 4)
            if node["wallKind"] == "glass":
                node["material"] = {"color": "#8bc5cf", "opacity": 0.38, "roughness": 0.22}
            if wall.get("evidence"):
                evidence_status = wall.get("authorityStatus", "candidate") if scene is authority else wall.get("presentationStatus", "accepted-inferred")
                existing_sources = list((scene["evidence"].get(node["id"]) or {}).get("sources", []))
                # An author pick's photo/elevation is itself a raw capture, so
                # by default it is its own provenance root - a lineage
                # genuinely independent of the LAS-derived sources.  Walls
                # whose evidence is LAS-derived (e.g. inferred dividers) carry
                # their own producer/root instead.
                local_source = base._evidence_source(
                    "local-elevation-or-panorama", wall["evidence"]["path"],
                    wall["evidence"]["sha256"],
                    wall["evidence"].get("producer") or "author-picks",
                    wall["evidence"].get("rootContentSha256s"),
                    note=wall["evidence"]["observation"],
                )
                scene["evidence"][node["id"]] = {
                    "status": evidence_status,
                    "sources": existing_sources + [local_source],
                    "reason": wall.get(
                        "inferenceReason",
                        "agent-authored clean-room macro interpretation; authority remains conservative",
                    ),
                }
                for opening in wall.get("openings", []):
                    opening_id = str(opening["id"])
                    base._add(scene, {
                        "id": opening_id, "type": "opening", "parentId": node["id"], "children": [],
                        "name": opening_id, "hostOffsetM": float(opening["offsetM"]),
                        "width": float(opening["width"]), "height": float(opening["height"]), "sillHeight": 0.0,
                    }, {
                        "status": evidence_status,
                        "sources": [local_source, base._evidence_source(
                            "inference-basis", proposals_rel, _sha256(proposals_path),
                            "structural-proposals", [root_sha],
                            note="host wall axis and current-capture proposal ledger",
                        )],
                        "reason": "panorama/elevation-supported opening hypothesis; final dimensions remain under review",
                    })
        if scene is not authority:
            for wall_index, wall in enumerate(walls, 1):
                if not wall.get("frameSpacingM"):
                    continue
                start, end = wall["start"], wall["end"]
                length = float(np.linalg.norm(end - start))
                direction = (end - start) / length
                frame_count = max(2, int(round(length / float(wall["frameSpacingM"]))) + 1)
                for frame_index, distance in enumerate(np.linspace(0.0, length, frame_count), 1):
                    center_plan = start + direction * distance
                    frame_id = f"glass_frame_{wall_index:02d}_{frame_index:02d}"
                    base._add(scene, {
                        "id": frame_id, "type": "column", "parentId": "level_main", "children": [],
                        "name": "Glazing frame", "center": [round(float(value), 5) for value in center_plan],
                        "size": [0.11, 0.11], "yaw": 0.0, "height": float(wall.get("height", 2.85)),
                        "baseHeight": 0.0, "material": {"color": "#25343c", "roughness": 0.52},
                    }, {
                        "status": "accepted-inferred",
                        "sources": [base._evidence_source(
                            "posed-photo-and-elevation", wall["evidence"]["path"],
                            wall["evidence"]["sha256"], "author-picks",
                            note="repeated dark glazing frame family",
                        ), base._evidence_source(
                            "inference-basis", proposals_rel, _sha256(proposals_path),
                            "structural-proposals", [root_sha],
                            note="current-capture glass facade axis",
                        )],
                        "reason": "repeated glazing-frame family inferred from panorama and indexed structural alignment",
                    })
    for scene in (hypothesis, presentation):
        zone_id = "zone_main_scan"
        base._add(scene, {
            "id": zone_id, "type": "zone", "parentId": "level_main", "children": [],
            "name": "Main scanned office envelope", "polygon": floor_polygon,
            "zoneKind": "occupied-office", "confidence": 0.70,
        }, {"status": "accepted-inferred", "sources": [
            base._evidence_source(
                "floor-support", "evidence/cleanroom-floor-support.png",
                _sha256(output / "evidence" / "cleanroom-floor-support.png"),
                "cleanroom-macro-builder", [root_sha],
                generator_parameters={"artifact": "floor-support"},
            ),
            base._evidence_source(
                "indexed-evidence-manifest",
                base._relative_scene_path(workspace / "evidence" / "evidence-manifest.json", output),
                _sha256(workspace / "evidence" / "evidence-manifest.json"),
                "indexed-pointcloud-evidence", [root_sha],
                generator_parameters={"artifact": "evidence-manifest"},
            ),
        ], "reason": "display-only leveled scan envelope; not a closed authority space"})
        furniture_sha = _sha256(output / "evidence" / "furniture-support.png")
        furniture_sources = _furniture_evidence_sources(furniture_sha, picks_payload, root_sha)
        for item_index in range(1, len(items) + 1):
            item_id = f"item_clean{item_index:03d}"
            scene["evidence"][item_id] = {
                "status": "accepted-inferred",
                "sources": json.loads(json.dumps(furniture_sources)),
                "reason": "visual-inferred furniture pending instance-level raw fit and collision review",
            }

    inferred_spaces = _north_room_topology(walls)
    for scene in (hypothesis, presentation):
        scene["review"]["topology"]["spaces"] = inferred_spaces

    base._atomic_json(output / "scene-authority.json", authority)
    authority_sha = _sha256(output / "scene-authority.json")
    hypothesis["bindings"] = {"authoritySha256": authority_sha}
    base._atomic_json(output / "scene-hypothesis.json", hypothesis)
    hypothesis_sha = _sha256(output / "scene-hypothesis.json")
    presentation["bindings"] = {"authoritySha256": authority_sha, "hypothesisSha256": hypothesis_sha}
    cloud_path = output / "pointcloud-cleanroom.lrpc"
    cloud = _export_lrpc(index, cloud_path, floor_polygon, envelope, u, v, floor_plane, center, max_points)
    presentation["meta"]["artifacts"] = {"pointCloud": cloud_path.name}
    base._atomic_json(output / "scene-presentation.json", presentation)
    report = {
        "schemaVersion": 1, "kind": "cleanroom-live-macro-build", "status": "WIP_READY_FOR_LIVE_VIEW",
        "indexFingerprint": index.manifest["indexFingerprint"], "surveySha256": _sha256(survey_path),
        "authoritySha256": authority_sha, "hypothesisSha256": hypothesis_sha,
        "presentationSha256": _sha256(output / "scene-presentation.json"),
        "shellWallCount": 0, "withheldShellWallCount": len(withheld_shell),
        "interiorCandidateWallCount": len(interiors),
        "authorPicksSha256": _sha256(author_picks_path) if author_picks_path else None,
        "withheldSegmentCount": len((picks_payload or {}).get("withheld", [])),
        "provisionalFurnitureCount": len(furniture_proposals),
        "displayFurnitureCount": len(items),
        "inferredRoomDividerCount": len(dividers),
        "panoramaRoomBandEnabled": panorama_band_enabled,
        "floorEnvelope": envelope, "pointCloud": cloud,
        "gates": {"macroHypothesis": "REVIEW", "authorityReview": "NOT_RUN", "presentationReview": "NOT_RUN"},
    }
    base._atomic_json(output / "cleanroom-build-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--survey", required=True, type=Path)
    parser.add_argument("--transforms", type=Path)
    parser.add_argument("--author-picks", type=Path)
    parser.add_argument("--max-viewer-points", type=int, default=450_000)
    args = parser.parse_args()
    result = build(args.workspace, args.output, args.survey, args.transforms, args.author_picks, args.max_viewer_points)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
