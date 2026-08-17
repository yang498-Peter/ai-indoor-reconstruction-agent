#!/usr/bin/env python3
"""Quantitatively compare Scene V2 wall geometry with indexed point data.

This is the point-cloud counterpart of a deterministic visual evaluator.  It
computes wall-surface residual P50/P90, along-wall support coverage, unsupported
length, and hard-gate failures.  Accepted measured walls are blocking;
accepted-inferred and candidate walls remain advisory.  The report binds the
exact scene hash and CaptureIndex fingerprint and never edits scene geometry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from capture_index import CaptureIndex


def _canonical_scene_hash(scene: dict[str, Any]) -> str:
    payload = json.dumps(scene, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finite_point(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value)
    )


def _level_elevation(scene: dict[str, Any], wall: dict[str, Any]) -> float:
    parent = scene.get("nodes", {}).get(wall.get("parentId"), {})
    if isinstance(parent, dict) and parent.get("type") == "level":
        value = parent.get("elevation", 0.0)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return 0.0


def _opening_intervals(scene: dict[str, Any], wall: dict[str, Any]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    nodes = scene.get("nodes", {})
    if not isinstance(nodes, dict):
        return intervals
    for child_id in wall.get("children", []):
        child = nodes.get(child_id)
        if not isinstance(child, dict) or child.get("type") not in {"door", "window", "opening"}:
            continue
        offset = child.get("hostOffsetM")
        width = child.get("width")
        if (
            isinstance(offset, (int, float))
            and isinstance(width, (int, float))
            and math.isfinite(offset)
            and math.isfinite(width)
            and width > 0
        ):
            intervals.append((float(offset) - float(width) * 0.5, float(offset) + float(width) * 0.5))
    return intervals


def evaluate_wall(
    scene: dict[str, Any],
    wall: dict[str, Any],
    index: CaptureIndex,
    *,
    residual_p90_max_m: float,
    support_ratio_min: float,
    support_tolerance_m: float,
    bin_size_m: float,
) -> dict[str, Any]:
    wall_id = str(wall.get("id", "<unknown>"))
    start_value, end_value = wall.get("start"), wall.get("end")
    height, thickness = wall.get("height"), wall.get("thickness")
    if (
        not _finite_point(start_value)
        or not _finite_point(end_value)
        or not isinstance(height, (int, float))
        or not isinstance(thickness, (int, float))
        or not math.isfinite(height)
        or not math.isfinite(thickness)
        or height <= 0
        or thickness <= 0
    ):
        return {"id": wall_id, "status": "FAIL", "hardGateFailures": ["invalid-wall-geometry"]}
    start = np.asarray(start_value, dtype=np.float64)
    end = np.asarray(end_value, dtype=np.float64)
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length < 0.05:
        return {"id": wall_id, "status": "FAIL", "hardGateFailures": ["wall-too-short"]}
    unit = vector / length
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    half_thickness = float(thickness) * 0.5
    query_padding = max(0.35, half_thickness + 0.25)
    base_z = _level_elevation(scene, wall) + float(wall.get("baseHeight", 0.0) or 0.0)
    lower_z = base_z + min(0.25, float(height) * 0.1)
    upper_z = base_z + float(height) - min(0.05, float(height) * 0.02)
    query = index.query_bbox(
        float(min(start[0], end[0]) - query_padding),
        float(min(start[1], end[1]) - query_padding),
        float(max(start[0], end[0]) + query_padding),
        float(max(start[1], end[1]) + query_padding),
        z_min=lower_z,
        z_max=upper_z,
    )
    if query.point_count == 0:
        return {
            "id": wall_id,
            "status": "NOT_RUN",
            "lengthM": round(length, 4),
            "pointCount": 0,
            "queryStats": query.stats,
            "hardGateFailures": ["no-local-structural-points"],
        }
    xy = np.column_stack((query.x, query.y))
    relative = xy - start
    along = relative @ unit
    lateral = relative @ normal
    local = (along >= 0.0) & (along <= length) & (np.abs(lateral) <= half_thickness + 0.30)
    along = along[local]
    lateral = lateral[local]
    if len(along) == 0:
        return {
            "id": wall_id,
            "status": "NOT_RUN",
            "lengthM": round(length, 4),
            "pointCount": 0,
            "queryStats": query.stats,
            "hardGateFailures": ["no-points-near-wall-solid"],
        }
    residual = np.abs(np.abs(lateral) - half_thickness)
    residual_sample = residual[residual <= 0.30]
    if len(residual_sample) == 0:
        return {
            "id": wall_id,
            "status": "FAIL",
            "lengthM": round(length, 4),
            "pointCount": int(len(along)),
            "queryStats": query.stats,
            "hardGateFailures": ["wall-has-no-surface-support"],
        }

    bin_count = max(1, int(math.ceil(length / bin_size_m)))
    expected = np.ones(bin_count, dtype=bool)
    bin_centres = (np.arange(bin_count, dtype=np.float64) + 0.5) * length / bin_count
    for opening_start, opening_end in _opening_intervals(scene, wall):
        expected &= ~((bin_centres >= opening_start) & (bin_centres <= opening_end))
    support = np.zeros(bin_count, dtype=bool)
    support_points = residual <= support_tolerance_m
    if np.any(support_points):
        bins = np.minimum((along[support_points] / length * bin_count).astype(np.int32), bin_count - 1)
        support[np.unique(bins)] = True
    expected_count = int(expected.sum())
    supported_count = int((support & expected).sum())
    support_ratio = supported_count / expected_count if expected_count else 1.0
    unsupported_length = length * (1.0 - support_ratio)
    p50 = float(np.percentile(residual_sample, 50))
    p90 = float(np.percentile(residual_sample, 90))
    failures: list[str] = []
    if p90 > residual_p90_max_m:
        failures.append(f"residual-p90>{residual_p90_max_m:.3f}m")
    if support_ratio < support_ratio_min:
        failures.append(f"support-ratio<{support_ratio_min:.3f}")
    return {
        "id": wall_id,
        "status": "PASS" if not failures else "FAIL",
        "lengthM": round(length, 4),
        "thicknessM": round(float(thickness), 5),
        "pointCount": int(len(along)),
        "residualSampleCount": int(len(residual_sample)),
        "residualP50M": round(p50, 6),
        "residualP90M": round(p90, 6),
        "supportRatio": round(support_ratio, 6),
        "supportedBinCount": supported_count,
        "expectedBinCount": expected_count,
        "unsupportedLengthM": round(unsupported_length, 4),
        "excludedOpeningIntervals": [[round(a, 4), round(b, 4)] for a, b in _opening_intervals(scene, wall)],
        "queryStats": query.stats,
        "hardGateFailures": failures,
    }


def evaluate_scene(
    scene: dict[str, Any],
    index: CaptureIndex,
    *,
    residual_p90_max_m: float = 0.08,
    support_ratio_min: float = 0.70,
    support_tolerance_m: float = 0.06,
    bin_size_m: float = 0.10,
    scene_sha256: str | None = None,
) -> dict[str, Any]:
    if residual_p90_max_m <= 0 or not 0 < support_ratio_min <= 1 or support_tolerance_m <= 0 or bin_size_m <= 0:
        raise ValueError("geometry metric thresholds are invalid")
    nodes = scene.get("nodes", {})
    evidence = scene.get("evidence", {})
    if not isinstance(nodes, dict) or not isinstance(evidence, dict):
        raise ValueError("point-cloud scene metrics require Semantic Scene V2 nodes and evidence")
    wall_metrics: list[dict[str, Any]] = []
    blocking_failures: list[str] = []
    measured_count = 0
    for wall_id, wall in sorted(nodes.items()):
        if not isinstance(wall, dict) or wall.get("type") != "wall" or wall.get("wallKind", "solid") == "elevated-band":
            continue
        evidence_status = evidence.get(wall_id, {}).get("status", "candidate") if isinstance(evidence.get(wall_id, {}), dict) else "candidate"
        metric = evaluate_wall(
            scene,
            wall,
            index,
            residual_p90_max_m=residual_p90_max_m,
            support_ratio_min=support_ratio_min,
            support_tolerance_m=support_tolerance_m,
            bin_size_m=bin_size_m,
        )
        metric["evidenceStatus"] = evidence_status
        metric["blocking"] = evidence_status == "accepted-measured"
        wall_metrics.append(metric)
        if evidence_status == "accepted-measured":
            measured_count += 1
            for failure in metric.get("hardGateFailures", []):
                blocking_failures.append(f"{wall_id}:{failure}")

    if measured_count == 0:
        status = "NOT_RUN"
        blocking_failures.append("no-accepted-measured-walls")
    else:
        status = "FAIL" if blocking_failures else "PASS"
    index_manifest = index.root / "capture-index.json"
    return {
        "schemaVersion": 1,
        "kind": "pointcloud-scene-metrics",
        "status": status,
        "sceneSha256": scene_sha256 or _canonical_scene_hash(scene),
        "sceneRevision": scene.get("revision"),
        "index": str(index.root),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "indexManifestSha256": hashlib.sha256(index_manifest.read_bytes()).hexdigest(),
        "thresholds": {
            "residualP90MaxM": residual_p90_max_m,
            "supportRatioMin": support_ratio_min,
            "supportToleranceM": support_tolerance_m,
            "binSizeM": bin_size_m,
        },
        "acceptedMeasuredWallCount": measured_count,
        "evaluatedWallCount": len(wall_metrics),
        "wallMetrics": wall_metrics,
        "hardGateFailures": blocking_failures,
        "scope": "modeled-wall support only; global omission review and visual semantics remain separate gates",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--residual-p90-max", type=float, default=0.08)
    parser.add_argument("--support-ratio-min", type=float, default=0.70)
    parser.add_argument("--support-tolerance", type=float, default=0.06)
    parser.add_argument("--bin-size", type=float, default=0.10)
    parser.add_argument("--validate-source", action="store_true")
    args = parser.parse_args()
    raw_scene = args.scene.read_bytes()
    scene = json.loads(raw_scene.decode("utf-8"))
    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    report = evaluate_scene(
        scene,
        index,
        residual_p90_max_m=args.residual_p90_max,
        support_ratio_min=args.support_ratio_min,
        support_tolerance_m=args.support_tolerance,
        bin_size_m=args.bin_size,
        scene_sha256=hashlib.sha256(raw_scene).hexdigest(),
    )
    report["scene"] = str(args.scene.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
