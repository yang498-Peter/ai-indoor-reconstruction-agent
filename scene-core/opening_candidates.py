#!/usr/bin/env python3
"""Propose door/window opening candidates on one wall from a CaptureIndex.

The wall face is rasterised into an along-wall x height occupancy grid and
openings are detected as low-density holes.  Glass returns sparse points, so a
cell counts as void when its density falls below a fraction of the face mean
density, never only when it is exactly empty.  Every candidate stays in
``candidate`` status: acceptance is an agent/review decision, not ours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from capture_index import CaptureIndex
from scene_api import evidence_lineage_id

PRODUCER = "opening-candidates"

ALONG_BIN_M = 0.05
HEIGHT_BIN_M = 0.05
# Holes touching the last 0.2 m of the wall are the wall simply ending, not
# an opening in it.
END_MARGIN_M = 0.20
CANDIDATE_MERGE_GAP_M = 0.15
# Glass still returns sparse points, so "void" is relative to the face mean
# density instead of absolute zero.
VOID_DENSITY_RATIO = 0.15
DOOR_MIN_CLEAR_M = 1.9
DOOR_WIDTH_RANGE_M = (0.6, 1.4)
WINDOW_SILL_MIN_M = 0.3
WINDOW_HEAD_MAX_M = 2.4
WINDOW_WIDTH_RANGE_M = (0.4, 3.0)
WINDOW_MIN_HEIGHT_M = 0.3


def _canonical_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _root_content_sha256(index: Any) -> str:
    """Same root-identity rule as cleanroom_scene_builder: raw content hash
    when known, index fingerprint otherwise."""
    identity = index.manifest.get("sourceIdentity") or {}
    return identity.get("contentSha256") or index.manifest["indexFingerprint"]


def _floor_height(x: np.ndarray, y: np.ndarray, floor_z: float | None, floor_plane: dict[str, float] | None) -> np.ndarray:
    if floor_plane is not None:
        return floor_plane["a"] * x + floor_plane["b"] * y + floor_plane["c"]
    assert floor_z is not None
    return np.full_like(x, float(floor_z))


def _occupancy_grid(
    index: Any,
    start: np.ndarray,
    end: np.ndarray,
    thickness_m: float,
    floor_z: float | None,
    floor_plane: dict[str, float] | None,
    *,
    top_m: float,
    lateral_margin_m: float,
    every: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length < 0.2:
        raise ValueError("wall centerline is degenerate")
    unit = vector / length
    normal = np.asarray([-unit[1], unit[0]])
    half = thickness_m * 0.5 + lateral_margin_m
    corners = np.asarray([
        start + normal * half, start - normal * half,
        end + normal * half, end - normal * half,
    ])
    # z window: cover the whole floor band variation across the corridor.
    corner_floor = _floor_height(corners[:, 0], corners[:, 1], floor_z, floor_plane)
    query = index.query_bbox(
        float(corners[:, 0].min()), float(corners[:, 1].min()),
        float(corners[:, 0].max()), float(corners[:, 1].max()),
        z_min=float(corner_floor.min()) - 0.05,
        z_max=float(corner_floor.max()) + top_m + 0.05,
        every=every,
    )
    n_cols = max(1, int(math.ceil(length / ALONG_BIN_M)))
    n_rows = max(1, int(math.ceil(top_m / HEIGHT_BIN_M)))
    counts = np.zeros((n_rows, n_cols), dtype=np.float64)
    stats = dict(query.stats)
    if query.point_count:
        dx = query.x - start[0]
        dy = query.y - start[1]
        along = dx * unit[0] + dy * unit[1]
        lateral = dx * normal[0] + dy * normal[1]
        height = query.z - _floor_height(query.x, query.y, floor_z, floor_plane)
        keep = (
            (np.abs(lateral) <= half)
            & (along >= 0.0) & (along <= length)
            & (height >= 0.0) & (height < top_m)
        )
        col = np.minimum((along[keep] / ALONG_BIN_M).astype(np.int64), n_cols - 1)
        row = np.minimum((height[keep] / HEIGHT_BIN_M).astype(np.int64), n_rows - 1)
        np.add.at(counts, (row, col), 1.0)
        stats["corridorPointCount"] = int(np.count_nonzero(keep))
    else:
        stats["corridorPointCount"] = 0
    return counts, {"lengthM": length, "unit": unit, "normal": normal, "queryStats": stats}


def _void_intervals(void_column: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous void runs as [lo, hi) row spans; a single occupied cell
    inside a run (a stray glass return) does not break it."""
    intervals: list[tuple[int, int]] = []
    run_start: int | None = None
    rows = len(void_column)
    for row in range(rows):
        if void_column[row]:
            if run_start is None:
                run_start = row
        elif run_start is not None:
            intervals.append((run_start, row))
            run_start = None
    if run_start is not None:
        intervals.append((run_start, rows))
    merged: list[tuple[int, int]] = []
    for interval in intervals:
        if merged and interval[0] - merged[-1][1] <= 1:
            merged[-1] = (merged[-1][0], interval[1])
        else:
            merged.append(interval)
    return merged


def _classify_columns(counts: np.ndarray, void_threshold: float, top_m: float) -> list[dict[str, Any] | None]:
    # Vertical-only smoothing keeps sparse glass columns continuous without
    # bleeding wall density sideways into the opening (which would shrink
    # measured widths).
    smoothed = cv2.blur(counts.astype(np.float32), (1, 3))
    void = smoothed < max(void_threshold, 1e-9)
    columns: list[dict[str, Any] | None] = []
    for col in range(counts.shape[1]):
        intervals = _void_intervals(void[:, col])
        record: dict[str, Any] | None = None
        for lo, hi in intervals:
            clear = (hi - lo) * HEIGHT_BIN_M
            sill = lo * HEIGHT_BIN_M
            head = min(hi * HEIGHT_BIN_M, top_m)
            if lo == 0 and clear >= DOOR_MIN_CLEAR_M:
                record = {"type": "door", "sill": 0.0, "head": head}
                break
            if (
                record is None
                and sill > WINDOW_SILL_MIN_M
                and head < WINDOW_HEAD_MAX_M
                and clear >= WINDOW_MIN_HEIGHT_M
            ):
                record = {"type": "window", "sill": sill, "head": head}
        columns.append(record)
    return columns


def _group_columns(columns: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for col, record in enumerate(columns):
        if record is None:
            continue
        if groups and groups[-1]["type"] == record["type"] and col - groups[-1]["cols"][-1] - 1 < int(round(CANDIDATE_MERGE_GAP_M / ALONG_BIN_M)):
            groups[-1]["cols"].append(col)
            groups[-1]["records"].append(record)
        else:
            groups.append({"type": record["type"], "cols": [col], "records": [record]})
    return groups


def detect_openings(
    index: Any,
    wall_start: tuple[float, float],
    wall_end: tuple[float, float],
    thickness_m: float,
    *,
    floor_z: float | None = None,
    floor_plane: dict[str, float] | None = None,
    top_m: float = 2.7,
    lateral_margin_m: float = 0.15,
    every: int = 1,
) -> dict[str, Any]:
    if (floor_z is None) == (floor_plane is None):
        raise ValueError("exactly one of floor_z / floor_plane is required")
    start = np.asarray(wall_start, dtype=np.float64)
    end = np.asarray(wall_end, dtype=np.float64)
    counts, geometry = _occupancy_grid(
        index, start, end, thickness_m, floor_z, floor_plane,
        top_m=top_m, lateral_margin_m=lateral_margin_m, every=every,
    )
    length = geometry["lengthM"]
    occupied = counts[counts > 0]
    face_mean = float(occupied.mean()) if occupied.size else 0.0
    void_threshold = face_mean * VOID_DENSITY_RATIO
    candidates: list[dict[str, Any]] = []
    if face_mean > 0.0:
        columns = _classify_columns(counts, void_threshold, top_m)
        for group in _group_columns(columns):
            start_along = group["cols"][0] * ALONG_BIN_M
            end_along = min((group["cols"][-1] + 1) * ALONG_BIN_M, length)
            width = end_along - start_along
            lo, hi = DOOR_WIDTH_RANGE_M if group["type"] == "door" else WINDOW_WIDTH_RANGE_M
            if not (lo <= width <= hi):
                continue
            if start_along < END_MARGIN_M or end_along > length - END_MARGIN_M:
                continue
            sill = float(np.median([record["sill"] for record in group["records"]]))
            head = float(np.median([record["head"] for record in group["records"]]))
            sill_row = int(sill / HEIGHT_BIN_M)
            head_row = max(sill_row + 1, min(int(math.ceil(head / HEIGHT_BIN_M)), counts.shape[0]))
            rect = counts[sill_row:head_row, group["cols"][0]:group["cols"][-1] + 1]
            contrast = float(rect.mean() / face_mean) if rect.size else 0.0
            confidence = float(np.clip(0.35 + 0.6 * (1.0 - contrast / VOID_DENSITY_RATIO), 0.05, 0.95))
            candidates.append({
                "type": group["type"],
                "status": "candidate",
                "hostOffsetM": round((start_along + end_along) * 0.5, 4),
                "widthM": round(width, 4),
                "sillM": round(sill, 4),
                "headM": round(head, 4),
                "densityContrast": round(contrast, 4),
                "confidence": round(confidence, 4),
            })
    candidates.sort(key=lambda item: item["hostOffsetM"])
    parameters = {
        "alongBinM": ALONG_BIN_M, "heightBinM": HEIGHT_BIN_M,
        "endMarginM": END_MARGIN_M, "mergeGapM": CANDIDATE_MERGE_GAP_M,
        "voidDensityRatio": VOID_DENSITY_RATIO,
        "doorMinClearM": DOOR_MIN_CLEAR_M, "doorWidthRangeM": list(DOOR_WIDTH_RANGE_M),
        "windowSillMinM": WINDOW_SILL_MIN_M, "windowHeadMaxM": WINDOW_HEAD_MAX_M,
        "windowWidthRangeM": list(WINDOW_WIDTH_RANGE_M), "windowMinHeightM": WINDOW_MIN_HEIGHT_M,
        "topM": top_m, "lateralMarginM": lateral_margin_m, "every": every,
        "wall": {
            "start": [float(start[0]), float(start[1])],
            "end": [float(end[0]), float(end[1])],
            "thicknessM": float(thickness_m),
        },
        "floorZ": floor_z, "floorPlane": floor_plane,
    }
    root = _root_content_sha256(index)
    return {
        "schemaVersion": 1,
        "kind": "opening-candidates",
        "producer": PRODUCER,
        "indexFingerprint": index.manifest["indexFingerprint"],
        "wall": {
            "start": [round(float(start[0]), 5), round(float(start[1]), 5)],
            "end": [round(float(end[0]), 5), round(float(end[1]), 5)],
            "thicknessM": round(float(thickness_m), 4),
            "lengthM": round(length, 4),
        },
        "parameters": parameters,
        "parametersFingerprint": _canonical_fingerprint(parameters),
        "lineageId": evidence_lineage_id([root], PRODUCER, parameters),
        "rootContentSha256s": [root],
        "faceMeanDensityPerCell": round(face_mean, 4),
        "voidThresholdPerCell": round(void_threshold, 4),
        "queryStats": geometry["queryStats"],
        "candidates": candidates,
    }


def _parse_pair(text: str) -> tuple[float, float]:
    parts = [float(part) for part in text.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected X,Y")
    return parts[0], parts[1]


def _parse_plane(text: str) -> dict[str, float]:
    parts = [float(part) for part in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected a,b,c for z = a*x + b*y + c")
    return {"a": parts[0], "b": parts[1], "c": parts[2]}


def main() -> int:
    # Negative values must be passed as --flag=value (e.g. --floor-z=-1.2).
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--wall-start", required=True, type=_parse_pair, metavar="X,Y")
    parser.add_argument("--wall-end", required=True, type=_parse_pair, metavar="X,Y")
    parser.add_argument("--thickness", type=float, default=0.12)
    floor = parser.add_mutually_exclusive_group(required=True)
    floor.add_argument("--floor-z", type=float)
    floor.add_argument("--floor-plane", type=_parse_plane, metavar="A,B,C")
    parser.add_argument("--top", type=float, default=2.7)
    parser.add_argument("--lateral-margin", type=float, default=0.15)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--validate-source", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    report = detect_openings(
        index, args.wall_start, args.wall_end, args.thickness,
        floor_z=args.floor_z, floor_plane=args.floor_plane,
        top_m=args.top, lateral_margin_m=args.lateral_margin, every=args.every,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
