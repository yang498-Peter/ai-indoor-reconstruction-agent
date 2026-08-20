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
import os
from pathlib import Path
from typing import Any

import numpy as np

from capture_index import CaptureIndex


def _canonical_scene_hash(scene: dict[str, Any]) -> str:
    payload = json.dumps(scene, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def _opening_masks(scene: dict[str, Any], wall: dict[str, Any], length: float, height: float) -> list[dict[str, Any]]:
    masks: list[dict[str, Any]] = []
    nodes = scene.get("nodes", {})
    if not isinstance(nodes, dict):
        return masks
    for child_id in wall.get("children", []):
        child = nodes.get(child_id)
        if not isinstance(child, dict) or child.get("type") not in {"door", "window", "opening"}:
            continue
        offset, width, opening_height = child.get("hostOffsetM"), child.get("width"), child.get("height")
        sill = child.get("sillHeight", 0.9 if child.get("type") == "window" else 0.0)
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (offset, width, opening_height, sill)
        ) or float(width) <= 0 or float(opening_height) <= 0:
            continue
        s_min = max(0.0, float(offset) - float(width) * 0.5)
        s_max = min(length, float(offset) + float(width) * 0.5)
        h_min = max(0.0, float(sill))
        h_max = min(height, h_min + float(opening_height))
        if s_min < s_max and h_min < h_max:
            masks.append({
                "id": str(child_id),
                "type": str(child.get("type")),
                "sMinM": round(s_min, 4),
                "sMaxM": round(s_max, 4),
                "hMinM": round(h_min, 4),
                "hMaxM": round(h_max, 4),
            })
    return masks


def _boolean_runs(mask: np.ndarray, step_m: float) -> list[list[float]]:
    runs: list[list[float]] = []
    start: int | None = None
    for index, value in enumerate(np.append(mask.astype(bool), False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append([round(start * step_m, 4), round(index * step_m, 4)])
            start = None
    return runs


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
    is_glass = wall.get("wallKind", "solid") == "glass"
    support_profile = "glass-weak-return-v1" if is_glass else "solid-two-face-v1"
    minimum_points_per_cell = 1 if is_glass else 2
    profile_support_ratio_min = min(support_ratio_min, 0.45) if is_glass else support_ratio_min
    vertical_ratio_min = 0.35 if is_glass else 0.65
    maximum_unsupported_run_m = 1.0 if is_glass else 0.60
    profile_residual_p90_max_m = residual_p90_max_m * (1.5 if is_glass else 1.0)
    outlier_fail_ratio = 0.80 if is_glass else 0.65
    query_padding = max(0.35, half_thickness + 0.25)
    base_z = _level_elevation(scene, wall) + float(wall.get("baseHeight", 0.0) or 0.0)
    lower_z = base_z
    upper_z = base_z + float(height)
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
    relative_height = query.z[local] - base_z
    in_height = (relative_height >= 0.0) & (relative_height <= float(height))
    along = along[in_height]
    lateral = lateral[in_height]
    relative_height = relative_height[in_height]

    along_bin_count = max(1, int(math.ceil(length / bin_size_m)))
    height_bin_size_m = max(0.20, bin_size_m)
    height_bin_count = max(1, int(math.ceil(float(height) / height_bin_size_m)))
    along_step_m = length / along_bin_count
    height_step_m = float(height) / height_bin_count
    s_centres = (np.arange(along_bin_count, dtype=np.float64) + 0.5) * along_step_m
    h_centres = (np.arange(height_bin_count, dtype=np.float64) + 0.5) * height_step_m
    expected = np.ones((height_bin_count, along_bin_count), dtype=bool)
    opening_masks = _opening_masks(scene, wall, length, float(height))
    for opening in opening_masks:
        s_mask = (s_centres >= opening["sMinM"]) & (s_centres <= opening["sMaxM"])
        h_mask = (h_centres >= opening["hMinM"]) & (h_centres <= opening["hMaxM"])
        expected[np.ix_(h_mask, s_mask)] = False

    s_bins = np.minimum((along / along_step_m).astype(np.int32), along_bin_count - 1)
    h_bins = np.minimum((relative_height / height_step_m).astype(np.int32), height_bin_count - 1)
    point_expected = expected[h_bins, s_bins]
    signed_residual = np.abs(lateral) - half_thickness
    residual = np.abs(signed_residual)
    residual_sample = residual[point_expected]
    support_points = point_expected & (residual <= support_tolerance_m)
    support_counts = np.zeros(expected.shape, dtype=np.int32)
    positive_counts = np.zeros(expected.shape, dtype=np.int32)
    negative_counts = np.zeros(expected.shape, dtype=np.int32)
    if np.any(support_points):
        np.add.at(support_counts, (h_bins[support_points], s_bins[support_points]), 1)
        positive = support_points & (lateral >= 0.0)
        negative = support_points & (lateral < 0.0)
        np.add.at(positive_counts, (h_bins[positive], s_bins[positive]), 1)
        np.add.at(negative_counts, (h_bins[negative], s_bins[negative]), 1)
    supported = (support_counts >= minimum_points_per_cell) & expected
    expected_count = int(np.count_nonzero(expected))
    supported_count = int(np.count_nonzero(supported))
    coverage_ratio = supported_count / expected_count if expected_count else 1.0
    row_expected = expected.sum(axis=1)
    row_supported = supported.sum(axis=1)
    valid_rows = row_expected > 0
    row_coverage = np.divide(row_supported, row_expected, out=np.ones_like(row_supported, dtype=float), where=valid_rows)
    vertical_ratio = float(np.count_nonzero(valid_rows & (row_coverage >= profile_support_ratio_min))) / max(1, int(np.count_nonzero(valid_rows)))
    column_expected = expected.sum(axis=0)
    column_supported = supported.sum(axis=0)
    column_coverage = np.divide(
        column_supported, column_expected, out=np.ones_like(column_supported, dtype=float), where=column_expected > 0,
    )
    unsupported_columns = (column_expected > 0) & (column_coverage < 0.50)
    unsupported_intervals = _boolean_runs(unsupported_columns, along_step_m)
    max_unsupported_run = max((end - start for start, end in unsupported_intervals), default=0.0)
    unsupported_length = float(np.count_nonzero(unsupported_columns)) * along_step_m
    endpoint_columns = max(1, int(math.ceil(0.25 / along_step_m)))

    def endpoint_ratio(selected: slice) -> float:
        selected_expected = int(np.count_nonzero(expected[:, selected]))
        return float(np.count_nonzero(supported[:, selected])) / selected_expected if selected_expected else 1.0

    two_face = expected & (positive_counts > 0) & (negative_counts > 0)
    two_face_ratio = float(np.count_nonzero(two_face)) / supported_count if supported_count else 0.0
    if len(residual_sample):
        p50, p90, p99 = (float(np.percentile(residual_sample, percentile)) for percentile in (50, 90, 99))
        outlier_ratio = float(np.count_nonzero(residual_sample > support_tolerance_m)) / len(residual_sample)
        signed_mean = float(np.mean(signed_residual[point_expected]))
    else:
        p50 = p90 = p99 = outlier_ratio = signed_mean = 0.0

    failures: list[str] = []
    review_reasons: list[str] = []
    if len(residual_sample) == 0:
        failures.append("wall-has-no-expected-surface-points")
    if p90 > profile_residual_p90_max_m:
        failures.append(f"surface-residual-p90>{profile_residual_p90_max_m:.3f}m")
    if max_unsupported_run > maximum_unsupported_run_m:
        failures.append(f"max-unsupported-run>{maximum_unsupported_run_m:.3f}m")
    if outlier_ratio > outlier_fail_ratio:
        failures.append(f"outlier-ratio>{outlier_fail_ratio:.3f}")
    if coverage_ratio < profile_support_ratio_min:
        review_reasons.append(f"coverage-area-ratio<{profile_support_ratio_min:.3f}")
    if vertical_ratio < vertical_ratio_min:
        review_reasons.append(f"vertical-coverage-ratio<{vertical_ratio_min:.3f}")
    if not is_glass and two_face_ratio < 0.05:
        review_reasons.append("two-face-support-ratio<0.050")
    start_support = endpoint_ratio(slice(0, endpoint_columns))
    end_support = endpoint_ratio(slice(-endpoint_columns, None))
    if min(start_support, end_support) < 0.50:
        review_reasons.append("endpoint-support<0.500")
    status = "FAIL" if failures else ("REVIEW" if review_reasons else "PASS")
    return {
        "id": wall_id,
        "status": status,
        "supportProfile": support_profile,
        "lengthM": round(length, 4),
        "thicknessM": round(float(thickness), 5),
        "pointCount": int(len(along)),
        "residualSampleCount": int(len(residual_sample)),
        "surfaceResidualP50M": round(p50, 6),
        "surfaceResidualP90M": round(p90, 6),
        "surfaceResidualP99M": round(p99, 6),
        "residualP50M": round(p50, 6),
        "residualP90M": round(p90, 6),
        "signedSurfaceResidualMeanM": round(signed_mean, 6),
        "coverageAreaRatio": round(coverage_ratio, 6),
        "verticalCoverageRatio": round(vertical_ratio, 6),
        "supportRatio": round(coverage_ratio, 6),
        "supportedCellCount": supported_count,
        "expectedCellCount": expected_count,
        "supportedBinCount": supported_count,
        "expectedBinCount": expected_count,
        "unsupportedLengthM": round(unsupported_length, 4),
        "maxUnsupportedRunM": round(max_unsupported_run, 4),
        "unsupportedIntervals": unsupported_intervals,
        "endpointSupport": {
            "start": round(start_support, 6),
            "end": round(end_support, 6),
        },
        "twoFaceSupportRatio": round(two_face_ratio, 6),
        "outlierRatio": round(outlier_ratio, 6),
        "excludedOpeningMasks": opening_masks,
        "excludedOpeningIntervals": [[round(a, 4), round(b, 4)] for a, b in _opening_intervals(scene, wall)],
        "queryStats": query.stats,
        "hardGateFailures": failures,
        "reviewReasons": review_reasons,
    }


def _accepted_walls(scene: dict[str, Any]) -> list[dict[str, Any]]:
    nodes, evidence = scene.get("nodes", {}), scene.get("evidence", {})
    if not isinstance(nodes, dict) or not isinstance(evidence, dict):
        return []
    result = []
    for node_id, node in nodes.items():
        receipt = evidence.get(node_id, {})
        if (
            isinstance(node, dict)
            and node.get("type") == "wall"
            and node.get("wallKind", "solid") != "elevated-band"
            and isinstance(receipt, dict)
            and receipt.get("status") in {"accepted-measured", "accepted-inferred"}
            and _finite_point(node.get("start"))
            and _finite_point(node.get("end"))
        ):
            result.append(node)
    return result


def _accepted_columns(scene: dict[str, Any]) -> list[dict[str, Any]]:
    nodes, evidence = scene.get("nodes", {}), scene.get("evidence", {})
    if not isinstance(nodes, dict) or not isinstance(evidence, dict):
        return []
    result = []
    for node_id, node in nodes.items():
        receipt = evidence.get(node_id, {})
        if (
            isinstance(node, dict)
            and node.get("type") == "column"
            and _finite_point(node.get("center"))
            and _finite_point(node.get("size"))
            and all(float(value) > 0 for value in node["size"])
            and isinstance(receipt, dict)
            and receipt.get("status") in {"accepted-measured", "accepted-inferred"}
        ):
            result.append(node)
    return result


def evaluate_point_to_model(
    scene: dict[str, Any],
    index: CaptureIndex,
    *,
    grid_size_m: float = 0.12,
    minimum_band_points: int = 2,
    explanation_tolerance_m: float = 0.12,
    max_unexplained_run_m: float = 1.0,
) -> dict[str, Any]:
    """Find full-height structural returns not explained by accepted walls."""
    if grid_size_m <= 0 or minimum_band_points < 1 or explanation_tolerance_m <= 0 or max_unexplained_run_m <= 0:
        raise ValueError("point-to-model thresholds are invalid")
    walls = _accepted_walls(scene)
    columns_nodes = _accepted_columns(scene)
    if not walls:
        return {"status": "NOT_RUN", "hardGateFailures": ["no-accepted-walls"]}
    endpoints = np.asarray([point for wall in walls for point in (wall["start"], wall["end"])], dtype=np.float64)
    padding = 0.35
    x_min, y_min = endpoints.min(axis=0) - padding
    x_max, y_max = endpoints.max(axis=0) + padding
    index_bounds = index.manifest.get("bounds", {}) if isinstance(getattr(index, "manifest", None), dict) else {}
    if isinstance(index_bounds, dict) and all(
        isinstance(index_bounds.get(key), (int, float)) and math.isfinite(index_bounds[key])
        for key in ("minX", "minY", "maxX", "maxY")
    ):
        x_min, y_min = float(index_bounds["minX"]), float(index_bounds["minY"])
        x_max, y_max = float(index_bounds["maxX"]), float(index_bounds["maxY"])
    columns = max(1, int(math.ceil((x_max - x_min) / grid_size_m)))
    rows = max(1, int(math.ceil((y_max - y_min) / grid_size_m)))
    if columns * rows > 8_000_000:
        return {"status": "NOT_RUN", "hardGateFailures": ["point-to-model-grid-too-large"]}
    base_values = [_level_elevation(scene, wall) + float(wall.get("baseHeight", 0.0) or 0.0) for wall in walls]
    base_z = min(base_values)
    top_z = max(base + float(wall.get("height", 0.0)) for base, wall in zip(base_values, walls))
    structural_height = top_z - base_z
    band_edges = np.asarray([
        base_z + min(0.20, structural_height * 0.08),
        base_z + min(0.80, structural_height * 0.32),
        base_z + min(1.80, structural_height * 0.70),
        top_z - min(0.05, structural_height * 0.02),
    ])
    if not np.all(np.diff(band_edges) > 0):
        return {"status": "NOT_RUN", "hardGateFailures": ["point-to-model-height-bands-invalid"]}
    counts = np.zeros((3, rows, columns), dtype=np.uint32)
    query_stats = {"tilesRead": 0, "pointsRead": 0, "pointsReturned": 0}
    for points in index.iter_bbox(float(x_min), float(y_min), float(x_max), float(y_max), z_min=float(band_edges[0]), z_max=float(band_edges[-1])):
        for key in query_stats:
            query_stats[key] += int(points.stats.get(key, 0))
        ix = np.clip(((points.x - x_min) / grid_size_m).astype(np.int32), 0, columns - 1)
        iy = np.clip(((points.y - y_min) / grid_size_m).astype(np.int32), 0, rows - 1)
        band = np.searchsorted(band_edges[1:], points.z, side="right")
        valid = (band >= 0) & (band < 3)
        np.add.at(counts, (band[valid], iy[valid], ix[valid]), 1)
    structural = np.all(counts >= minimum_band_points, axis=0)
    structural_y, structural_x = np.nonzero(structural)
    if not len(structural_x):
        return {
            "status": "NOT_RUN", "structuralCellCount": 0, "queryStats": query_stats,
            "hardGateFailures": ["no-full-height-structural-cells"],
        }
    centres = np.column_stack((
        x_min + (structural_x + 0.5) * grid_size_m,
        y_min + (structural_y + 0.5) * grid_size_m,
    ))
    explained = np.zeros(len(centres), dtype=bool)
    for wall in walls:
        start = np.asarray(wall["start"], dtype=np.float64)
        end = np.asarray(wall["end"], dtype=np.float64)
        vector = end - start
        length = float(np.linalg.norm(vector))
        if length < 0.05:
            continue
        direction = vector / length
        normal = np.asarray([-direction[1], direction[0]])
        relative = centres - start
        along = relative @ direction
        lateral = np.abs(relative @ normal)
        half_thickness = float(wall.get("thickness", 0.12)) * 0.5
        explained |= (along >= -explanation_tolerance_m) & (along <= length + explanation_tolerance_m) & (
            lateral <= half_thickness + explanation_tolerance_m
        )
    for column in columns_nodes:
        center = np.asarray(column["center"], dtype=np.float64)
        size = np.asarray(column["size"], dtype=np.float64)
        yaw = float(column.get("yaw", 0.0) or 0.0)
        cosine, sine = math.cos(-yaw), math.sin(-yaw)
        relative = centres - center
        local_x = relative[:, 0] * cosine - relative[:, 1] * sine
        local_y = relative[:, 0] * sine + relative[:, 1] * cosine
        explained |= (np.abs(local_x) <= size[0] * 0.5 + explanation_tolerance_m) & (
            np.abs(local_y) <= size[1] * 0.5 + explanation_tolerance_m
        )
    unexplained_cells = {(int(y), int(x)) for y, x in zip(structural_y[~explained], structural_x[~explained])}
    components: list[dict[str, Any]] = []
    remaining = set(unexplained_cells)
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        component = [seed]
        while stack:
            y, x = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbour = (y + dy, x + dx)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        stack.append(neighbour)
                        component.append(neighbour)
        ys = [item[0] for item in component]
        xs = [item[1] for item in component]
        span = max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) * grid_size_m
        components.append({"cellCount": len(component), "areaM2": len(component) * grid_size_m ** 2, "spanM": span})
    max_run = max((item["spanM"] for item in components), default=0.0)
    unexplained_area = len(unexplained_cells) * grid_size_m ** 2
    unexplained_length = sum(item["spanM"] for item in components)
    failures = [f"max-unexplained-run>{max_unexplained_run_m:.3f}m"] if max_run > max_unexplained_run_m else []
    return {
        "status": "FAIL" if failures else ("REVIEW" if unexplained_cells else "PASS"),
        "gridSizeM": grid_size_m,
        "structuralCellCount": int(len(structural_x)),
        "acceptedWallCount": len(walls),
        "acceptedColumnCount": len(columns_nodes),
        "explainedStructuralCellCount": int(np.count_nonzero(explained)),
        "unexplainedStructuralCellCount": len(unexplained_cells),
        "unexplainedStructuralAreaM2": round(unexplained_area, 4),
        "unexplainedStructuralLengthM": round(unexplained_length, 4),
        "maxUnexplainedRunM": round(max_run, 4),
        "unexplainedComponentCount": len(components),
        "queryStats": query_stats,
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
    point_to_model_grid_size_m: float = 0.12,
    max_unexplained_run_m: float = 1.0,
    scene_sha256: str | None = None,
) -> dict[str, Any]:
    if (
        residual_p90_max_m <= 0
        or not 0 < support_ratio_min <= 1
        or support_tolerance_m <= 0
        or bin_size_m <= 0
        or point_to_model_grid_size_m <= 0
        or max_unexplained_run_m <= 0
    ):
        raise ValueError("geometry metric thresholds are invalid")
    nodes = scene.get("nodes", {})
    evidence = scene.get("evidence", {})
    if scene.get("sceneLayer", "authority") != "authority":
        raise ValueError("POINTCLOUD_METRICS_AUTHORITY_REQUIRED")
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
            if metric.get("status") == "REVIEW":
                blocking_failures.append(f"{wall_id}:wall-metric-review-required")

    point_to_model = evaluate_point_to_model(
        scene,
        index,
        grid_size_m=point_to_model_grid_size_m,
        max_unexplained_run_m=max_unexplained_run_m,
    )
    if measured_count > 0 and point_to_model.get("status") != "PASS":
        failures = point_to_model.get("hardGateFailures", [])
        if failures:
            blocking_failures.extend(f"point-to-model:{failure}" for failure in failures)
        else:
            blocking_failures.append(f"point-to-model:status-{str(point_to_model.get('status')).lower()}")

    if measured_count == 0:
        status = "NOT_RUN"
        blocking_failures.append("no-accepted-measured-walls")
    else:
        status = "FAIL" if blocking_failures else "PASS"
    index_manifest = index.root / "capture-index.json"
    scene_digest = scene_sha256 or _canonical_scene_hash(scene)
    index_manifest_digest = hashlib.sha256(index_manifest.read_bytes()).hexdigest()
    thresholds = {
        "residualP90MaxM": residual_p90_max_m,
        "supportRatioMin": support_ratio_min,
        "supportToleranceM": support_tolerance_m,
        "binSizeM": bin_size_m,
        "pointToModelGridSizeM": point_to_model_grid_size_m,
        "maxUnexplainedRunM": max_unexplained_run_m,
        "solidProfile": {
            "name": "solid-two-face-v1",
            "minimumPointsPerCell": 2,
            "verticalCoverageRatioMin": 0.65,
            "maximumUnsupportedRunM": 0.60,
            "twoFaceSupportRatioReviewMin": 0.05,
            "endpointSupportReviewMin": 0.50,
        },
        "glassProfile": {
            "name": "glass-weak-return-v1",
            "minimumPointsPerCell": 1,
            "coverageAreaRatioMin": min(support_ratio_min, 0.45),
            "verticalCoverageRatioMin": 0.35,
            "maximumUnsupportedRunM": 1.0,
            "endpointSupportReviewMin": 0.50,
        },
        "pointToModelMinimumBandPoints": 2,
        "pointToModelExplanationToleranceM": 0.12,
    }
    source_identity = index.manifest.get("sourceIdentity", {})
    capture_binding = index.manifest.get("captureBinding", {})
    return {
        "schemaVersion": "2.0",
        "artifactType": "pointcloud-scene-metrics-v2",
        "kind": "pointcloud-scene-metrics",
        "status": status,
        "sceneSha256": scene_digest,
        "sceneRevision": scene.get("revision"),
        "index": str(index.root),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "indexManifestSha256": index_manifest_digest,
        "inputHashes": {
            "sceneSha256": scene_digest,
            "indexManifestSha256": index_manifest_digest,
        },
        "configDigest": _canonical_hash(thresholds),
        "producer": {
            "name": "pointcloud_scene_metrics.py",
            "version": "2.0",
            "codeSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "gitSha": os.environ.get("GITHUB_SHA", "WORKTREE"),
        },
        "lineage": {
            "sourceContentSha256": source_identity.get("contentSha256") if isinstance(source_identity, dict) else None,
            "sourceSetDigest": capture_binding.get("sourceSetDigest") if isinstance(capture_binding, dict) else None,
            "captureFingerprint": capture_binding.get("captureFingerprint") if isinstance(capture_binding, dict) else None,
            "indexFingerprint": index.manifest["indexFingerprint"],
        },
        "thresholds": thresholds,
        "acceptedMeasuredWallCount": measured_count,
        "evaluatedWallCount": len(wall_metrics),
        "wallMetrics": wall_metrics,
        "pointToModelAudit": point_to_model,
        "hardGateFailures": blocking_failures,
        "scope": "model-to-point wall support and point-to-model full-height structural omission audit; visual semantics remain separate",
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
    parser.add_argument("--point-to-model-grid-size", type=float, default=0.12)
    parser.add_argument("--max-unexplained-run", type=float, default=1.0)
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
        point_to_model_grid_size_m=args.point_to_model_grid_size,
        max_unexplained_run_m=args.max_unexplained_run,
        scene_sha256=hashlib.sha256(raw_scene).hexdigest(),
    )
    report["scene"] = str(args.scene.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
