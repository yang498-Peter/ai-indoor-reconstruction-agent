#!/usr/bin/env python3
"""Deterministic wall and global-axis proposal engine for indoor captures.

The engine consumes a CaptureIndex, never the source capture.  It detects wall
face observations in a structural height band, refines every line against raw
indexed points, pairs parallel faces into centreline/thickness hypotheses, and
derives dominant building axes.  Every output remains ``candidate`` and keeps
both raw and snapped/suggested geometry; this script never edits Scene V2 or
silently moves accepted geometry.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Iterable

import cv2
import numpy as np

from capture_index import CaptureIndex


def _angle_delta(a: float, b: float) -> float:
    delta = abs((a - b) % math.pi)
    return min(delta, math.pi - delta)


def _point(value: np.ndarray) -> list[float]:
    return [round(float(value[0]), 6), round(float(value[1]), 6)]


def _offset_line(line: dict[str, list[float]], offset_m: float) -> dict[str, list[float]]:
    start = np.asarray(line["start"], dtype=np.float64)
    end = np.asarray(line["end"], dtype=np.float64)
    direction = end - start
    direction /= np.linalg.norm(direction)
    normal = np.asarray([-direction[1], direction[0]], dtype=np.float64)
    return {"start": _point(start + normal * offset_m), "end": _point(end + normal * offset_m)}


def _wall_hypothesis(candidate: dict[str, object]) -> dict[str, object]:
    """Expose candidate geometry without laundering one observed face into a centreline."""
    hypothesis_id = f"hypothesis:{candidate['id']}"
    raw = candidate["rawCenterline"]
    assert isinstance(raw, dict)
    thickness = float(candidate["thicknessM"])
    wall_mode = str(candidate["wallMode"])
    if wall_mode == "single-face":
        alternatives = [
            {
                "id": f"{hypothesis_id}:negative",
                "side": "negative",
                "centerline": _offset_line(raw, -thickness * 0.5),
            },
            {
                "id": f"{hypothesis_id}:positive",
                "side": "positive",
                "centerline": _offset_line(raw, thickness * 0.5),
            },
        ]
        authority_eligibility = "inferred-only-until-corroborated"
    else:
        alternatives = [{"id": f"{hypothesis_id}:center", "side": "paired", "centerline": raw}]
        authority_eligibility = "measurement-eligible-after-independent-review"
    confidence = float(candidate.get("confidence", 0.0))
    confidence_band = "high" if confidence >= 0.85 else "medium" if confidence >= 0.55 else "low"
    return {
        "hypothesisId": hypothesis_id,
        "sourceCandidateId": candidate["id"],
        "status": "candidate",
        "wallMode": wall_mode,
        "sourceObservationIds": list(candidate.get("sourceFaceIds", [])),
        "observedFaceLine": raw if wall_mode == "single-face" else None,
        "centerlineAlternatives": alternatives,
        "thicknessM": thickness,
        "thicknessDistribution": {
            "medianM": thickness,
            "minM": thickness,
            "maxM": thickness,
            "source": "paired-faces" if wall_mode == "paired-faces" else "versioned-profile-prior",
        },
        "lengthM": candidate["lengthM"],
        "supportPointCount": candidate["supportPointCount"],
        "fitResidualP50M": candidate["fitResidualP50M"],
        "fitResidualP90M": candidate["fitResidualP90M"],
        "confidenceBand": confidence_band,
        "rawFeatures": {
            "legacyConfidence": candidate.get("confidence"),
            "cavityPointRatio": candidate.get("cavityPointRatio"),
            "faceColorDeltaNorm": candidate.get("faceColorDeltaNorm"),
        },
        "authorityEligibility": authority_eligibility,
        "authorityRule": "hypothesis remains a candidate until an Agent/reviewer transaction",
    }


@dataclass(frozen=True)
class LineObservation:
    start: np.ndarray
    end: np.ndarray
    support_count: int
    residual_p50_m: float = 0.0
    residual_p90_m: float = 0.0
    observation_id: str = ""

    @classmethod
    def from_endpoints(
        cls,
        start: Iterable[float],
        end: Iterable[float],
        *,
        support_count: int,
        residual_p50_m: float = 0.0,
        residual_p90_m: float = 0.0,
        observation_id: str = "",
    ) -> "LineObservation":
        first = np.asarray(list(start), dtype=np.float64)
        second = np.asarray(list(end), dtype=np.float64)
        if first.shape != (2,) or second.shape != (2,) or not np.isfinite(first).all() or not np.isfinite(second).all():
            raise ValueError("line endpoints must be finite source-plan points")
        if float(np.linalg.norm(second - first)) < 0.05:
            raise ValueError("line observation is too short")
        direction = second - first
        angle = math.atan2(float(direction[1]), float(direction[0])) % math.pi
        unit = np.asarray([math.cos(angle), math.sin(angle)], dtype=np.float64)
        if float(np.dot(second - first, unit)) < 0:
            first, second = second, first
        return cls(first, second, int(support_count), float(residual_p50_m), float(residual_p90_m), observation_id)

    @property
    def vector(self) -> np.ndarray:
        return self.end - self.start

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.vector))

    @property
    def angle(self) -> float:
        return math.atan2(float(self.vector[1]), float(self.vector[0])) % math.pi

    @property
    def unit(self) -> np.ndarray:
        return np.asarray([math.cos(self.angle), math.sin(self.angle)], dtype=np.float64)

    @property
    def normal(self) -> np.ndarray:
        unit = self.unit
        return np.asarray([-unit[1], unit[0]], dtype=np.float64)

    @property
    def offset(self) -> float:
        return float(np.dot((self.start + self.end) * 0.5, self.normal))

    def interval(self, unit: np.ndarray | None = None) -> tuple[float, float]:
        direction = self.unit if unit is None else unit
        values = sorted((float(np.dot(self.start, direction)), float(np.dot(self.end, direction))))
        return values[0], values[1]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.observation_id,
            "start": _point(self.start),
            "end": _point(self.end),
            "lengthM": round(self.length, 4),
            "angleDeg": round(math.degrees(self.angle), 4),
            "supportPointCount": self.support_count,
            "residualP50M": round(self.residual_p50_m, 5),
            "residualP90M": round(self.residual_p90_m, 5),
        }


# Loop-drift duplicates of one wall face sit 8-30 cm apart and would otherwise
# pair into a phantom wall whose "thickness" equals the drift magnitude.  A real
# wall keeps a solid, point-free cavity between its faces, so the cavity/face
# density ratio separates the two cases; colour continuity across the pair is a
# secondary drift hint (a wall's two faces may share paint, hence colour alone
# never decides).
_DRIFT_FACE_CORRIDOR_M = 0.02
_DRIFT_CAVITY_INSET_M = 0.02
_DRIFT_MIN_COLOR_SAMPLE = 10


def _pair_drift_evidence(
    index: CaptureIndex,
    *,
    unit: np.ndarray,
    normal: np.ndarray,
    offsets: tuple[float, float],
    along: tuple[float, float],
    z_min: float | None,
    z_max: float | None,
) -> tuple[float | None, float | None]:
    """Return (cavity/face point-density ratio, face RGB median delta in [0,1]).

    Either value is ``None`` when it cannot be measured (degenerate band,
    empty corridors, or a colourless index).
    """
    lo, hi = min(offsets), max(offsets)
    cavity_lo = lo + _DRIFT_CAVITY_INSET_M
    cavity_hi = hi - _DRIFT_CAVITY_INSET_M
    along_start, along_end = along
    span = along_end - along_start
    if span <= 0.05 or cavity_hi - cavity_lo < 0.01:
        return None, None
    pad = _DRIFT_FACE_CORRIDOR_M + 0.01
    xs = [unit[0] * a + normal[0] * o for a in (along_start, along_end) for o in (lo - pad, hi + pad)]
    ys = [unit[1] * a + normal[1] * o for a in (along_start, along_end) for o in (lo - pad, hi + pad)]
    points = index.query_bbox(min(xs), min(ys), max(xs), max(ys), z_min=z_min, z_max=z_max)
    if points.point_count == 0:
        return None, None
    along_values = points.x * float(unit[0]) + points.y * float(unit[1])
    offset_values = points.x * float(normal[0]) + points.y * float(normal[1])
    keep = (along_values >= along_start) & (along_values <= along_end)
    offset_values = offset_values[keep]
    rgb = points.rgb[keep]
    first_mask = np.abs(offset_values - offsets[0]) <= _DRIFT_FACE_CORRIDOR_M
    second_mask = np.abs(offset_values - offsets[1]) <= _DRIFT_FACE_CORRIDOR_M
    cavity_mask = (offset_values > cavity_lo) & (offset_values < cavity_hi) & ~first_mask & ~second_mask
    face_count = int(first_mask.sum()) + int(second_mask.sum())
    if face_count == 0:
        return None, None
    face_density = face_count / (2.0 * span * 2.0 * _DRIFT_FACE_CORRIDOR_M)
    cavity_density = int(cavity_mask.sum()) / (span * (cavity_hi - cavity_lo))
    cavity_ratio = float(cavity_density / face_density)
    color_delta: float | None = None
    if (
        bool(index.manifest.get("hasColor"))
        and int(first_mask.sum()) >= _DRIFT_MIN_COLOR_SAMPLE
        and int(second_mask.sum()) >= _DRIFT_MIN_COLOR_SAMPLE
    ):
        first_color = np.median(rgb[first_mask].astype(np.float64), axis=0)
        second_color = np.median(rgb[second_mask].astype(np.float64), axis=0)
        # sqrt(3)*255 ~= 441.7; 443 keeps the historical normaliser stable.
        color_delta = float(np.linalg.norm(first_color - second_color)) / 443.0
    return cavity_ratio, color_delta


def _merge_drift_faces(first: LineObservation, second: LineObservation) -> LineObservation:
    """Fuse two drift copies of one face into a single support-weighted line.

    The merged residuals understate the drift smear on purpose: the candidate
    built from this observation carries ``driftMergedFrom`` and a review
    warning, so acceptance still goes through a human/Agent transaction.
    """
    total = first.support_count + second.support_count
    sin_sum = first.support_count * math.sin(2.0 * first.angle) + second.support_count * math.sin(2.0 * second.angle)
    cos_sum = first.support_count * math.cos(2.0 * first.angle) + second.support_count * math.cos(2.0 * second.angle)
    angle = (0.5 * math.atan2(sin_sum, cos_sum)) % math.pi
    unit = np.asarray([math.cos(angle), math.sin(angle)], dtype=np.float64)
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    first_offset = float(np.dot((first.start + first.end) * 0.5, normal))
    second_offset = float(np.dot((second.start + second.end) * 0.5, normal))
    offset = (first_offset * first.support_count + second_offset * second.support_count) / total
    first_interval = first.interval(unit)
    second_interval = second.interval(unit)
    along_start = min(first_interval[0], second_interval[0])
    along_end = max(first_interval[1], second_interval[1])
    dominant = first if first.support_count >= second.support_count else second
    return LineObservation.from_endpoints(
        unit * along_start + normal * offset,
        unit * along_end + normal * offset,
        support_count=total,
        residual_p50_m=max(first.residual_p50_m, second.residual_p50_m),
        residual_p90_m=max(first.residual_p90_m, second.residual_p90_m),
        observation_id=dominant.observation_id,
    )


def pair_wall_faces(
    faces: list[LineObservation],
    *,
    index: CaptureIndex | None = None,
    z_min: float | None = None,
    z_max: float | None = None,
    min_thickness_m: float = 0.07,
    max_thickness_m: float = 0.45,
    angle_tolerance_deg: float = 4.0,
    min_overlap_m: float = 0.7,
    cavity_ratio_threshold: float = 0.25,
    color_delta_threshold: float = 0.12,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Pair parallel faces into wall hypotheses and split off drift duplicates.

    Returns ``(walls, drift_merges)``.  Without an ``index`` no cavity/colour
    evidence exists, so every candidate is treated as a normal pair.
    """
    candidates: list[tuple[float, int, int, dict[str, object], bool, float | None, float | None]] = []
    angle_tolerance = math.radians(angle_tolerance_deg)
    for first_index, first in enumerate(faces):
        for second_index in range(first_index + 1, len(faces)):
            second = faces[second_index]
            if _angle_delta(first.angle, second.angle) > angle_tolerance:
                continue
            unit = first.unit
            if float(np.dot(unit, second.unit)) < 0:
                unit = -unit
            normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
            first_offset = float(np.dot((first.start + first.end) * 0.5, normal))
            second_offset = float(np.dot((second.start + second.end) * 0.5, normal))
            thickness = abs(second_offset - first_offset)
            if thickness < min_thickness_m or thickness > max_thickness_m:
                continue
            first_interval = first.interval(unit)
            second_interval = second.interval(unit)
            along_start = max(first_interval[0], second_interval[0])
            along_end = min(first_interval[1], second_interval[1])
            overlap = along_end - along_start
            if overlap < min_overlap_m:
                continue
            cavity_ratio: float | None = None
            color_delta: float | None = None
            if index is not None:
                cavity_ratio, color_delta = _pair_drift_evidence(
                    index,
                    unit=unit,
                    normal=normal,
                    offsets=(first_offset, second_offset),
                    along=(along_start, along_end),
                    z_min=z_min,
                    z_max=z_max,
                )
            drift_threshold = cavity_ratio_threshold
            if color_delta is not None and color_delta < color_delta_threshold:
                # Colour continuity raises drift suspicion but never decides
                # alone: it only lowers the cavity-density threshold.
                drift_threshold *= 0.7
            is_drift = cavity_ratio is not None and cavity_ratio > drift_threshold
            # High cavity occupancy penalises confidence so phantom walls stop
            # outranking (and greedily displacing) real walls.
            penalty = 1.0 if cavity_ratio is None else max(0.2, 1.0 - 2.0 * cavity_ratio)
            centre_offset = (first_offset + second_offset) * 0.5
            start = unit * along_start + normal * centre_offset
            end = unit * along_end + normal * centre_offset
            support = first.support_count + second.support_count
            residual_p50 = max(first.residual_p50_m, second.residual_p50_m)
            residual_p90 = max(first.residual_p90_m, second.residual_p90_m)
            confidence = min(
                0.99,
                0.55
                + 0.18 * min(1.0, overlap / 4.0)
                + 0.14 * min(1.0, support / 1000.0)
                + 0.12 * max(0.0, 1.0 - residual_p90 / 0.06),
            ) * penalty
            face_ids = [first.observation_id or f"face-{first_index + 1}", second.observation_id or f"face-{second_index + 1}"]
            value = {
                "wallMode": "paired-faces",
                "sourceFaceIds": face_ids,
                "rawCenterline": {"start": _point(start), "end": _point(end)},
                "thicknessM": round(thickness, 5),
                "lengthM": round(overlap, 4),
                "supportPointCount": support,
                "fitResidualP50M": round(residual_p50, 5),
                "fitResidualP90M": round(residual_p90, 5),
                "cavityPointRatio": None if cavity_ratio is None else round(cavity_ratio, 4),
                "faceColorDeltaNorm": None if color_delta is None else round(color_delta, 4),
                "confidence": round(confidence, 4),
            }
            score = overlap * max(1.0, math.log2(support + 1)) / max(0.01, residual_p90 + 0.01) * penalty
            candidates.append((score, first_index, second_index, value, is_drift, cavity_ratio, color_delta))

    # A face cannot justify two wall thickness hypotheses.  Prefer the pair
    # with the strongest overlap/support/residual score; drift pairs consume
    # their faces too, but merge into one observation instead of a wall.
    used: set[int] = set()
    accepted: list[dict[str, object]] = []
    merged: list[dict[str, object]] = []
    for _score, first_index, second_index, value, is_drift, cavity_ratio, color_delta in sorted(
        candidates, key=lambda item: item[0], reverse=True
    ):
        if first_index in used or second_index in used:
            continue
        used.update((first_index, second_index))
        if is_drift:
            merged.append(
                {
                    "observation": _merge_drift_faces(faces[first_index], faces[second_index]),
                    "driftMergedFrom": list(value["sourceFaceIds"]),
                    "cavityPointRatio": None if cavity_ratio is None else round(cavity_ratio, 4),
                    "faceColorDeltaNorm": None if color_delta is None else round(color_delta, 4),
                }
            )
        else:
            accepted.append(value)
    return accepted, merged


def _refine_observation(
    start: np.ndarray,
    end: np.ndarray,
    xy: np.ndarray,
    *,
    corridor_m: float,
    observation_id: str,
) -> LineObservation | None:
    vector = end - start
    length = float(np.linalg.norm(vector))
    if length < 0.05:
        return None
    unit = vector / length
    normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
    relative = xy - start
    along = relative @ unit
    lateral = relative @ normal
    keep = (along >= -0.2) & (along <= length + 0.2) & (np.abs(lateral) <= corridor_m)
    selected = xy[keep]
    if len(selected) < 20:
        return None
    centre = selected.mean(axis=0)
    covariance = np.cov((selected - centre).T)
    values, vectors = np.linalg.eigh(covariance)
    fitted_unit = vectors[:, int(np.argmax(values))]
    if float(np.dot(fitted_unit, unit)) < 0:
        fitted_unit = -fitted_unit
    fitted_normal = np.asarray([-fitted_unit[1], fitted_unit[0]], dtype=np.float64)
    fitted_along = (selected - centre) @ fitted_unit
    fitted_lateral = np.abs((selected - centre) @ fitted_normal)
    lo, hi = np.percentile(fitted_along, [1.0, 99.0])
    if hi - lo < 0.05:
        return None
    return LineObservation.from_endpoints(
        centre + fitted_unit * lo,
        centre + fitted_unit * hi,
        support_count=len(selected),
        residual_p50_m=float(np.percentile(fitted_lateral, 50)),
        residual_p90_m=float(np.percentile(fitted_lateral, 90)),
        observation_id=observation_id,
    )


def _deduplicate_observations(observations: list[LineObservation]) -> list[LineObservation]:
    kept: list[LineObservation] = []
    ranked = sorted(observations, key=lambda item: (item.length * math.log2(item.support_count + 1)), reverse=True)
    for observation in ranked:
        duplicate = False
        for existing in kept:
            if _angle_delta(observation.angle, existing.angle) > math.radians(2.0):
                continue
            unit = existing.unit
            normal = existing.normal
            offset_delta = abs(float(np.dot((observation.start + observation.end) * 0.5, normal)) - existing.offset)
            if offset_delta > 0.055:
                continue
            first = observation.interval(unit)
            second = existing.interval(unit)
            overlap = max(0.0, min(first[1], second[1]) - max(first[0], second[0]))
            if overlap >= 0.65 * min(observation.length, existing.length):
                duplicate = True
                break
        if not duplicate:
            kept.append(observation)
    ordered = sorted(kept, key=lambda item: (round(item.angle, 3), item.offset, -item.length))
    return [
        LineObservation.from_endpoints(
            item.start,
            item.end,
            support_count=item.support_count,
            residual_p50_m=item.residual_p50_m,
            residual_p90_m=item.residual_p90_m,
            observation_id=f"face-{index + 1:03d}",
        )
        for index, item in enumerate(ordered)
    ]


def _axis_families(lines: list[LineObservation]) -> list[dict[str, object]]:
    if not lines:
        return []
    remaining = set(range(len(lines)))
    families: list[dict[str, object]] = []
    while remaining and len(families) < 4:
        best_index = max(
            remaining,
            key=lambda candidate: sum(
                lines[index].length
                for index in remaining
                if _angle_delta(lines[candidate].angle, lines[index].angle) <= math.radians(5.0)
            ),
        )
        members = [index for index in remaining if _angle_delta(lines[best_index].angle, lines[index].angle) <= math.radians(5.0)]
        sin_sum = sum(lines[index].length * math.sin(2.0 * lines[index].angle) for index in members)
        cos_sum = sum(lines[index].length * math.cos(2.0 * lines[index].angle) for index in members)
        angle = (0.5 * math.atan2(sin_sum, cos_sum)) % math.pi
        support_length = sum(lines[index].length for index in members)
        families.append(
            {
                "id": f"axis-{len(families) + 1}",
                "angleDeg": round(math.degrees(angle), 4),
                "supportLengthM": round(support_length, 4),
                "observationIds": [lines[index].observation_id for index in members],
            }
        )
        remaining.difference_update(members)
    return sorted(families, key=lambda item: float(item["supportLengthM"]), reverse=True)


def _suggested_line(raw: dict[str, list[float]], families: list[dict[str, object]]) -> tuple[dict[str, list[float]], str | None, float]:
    start = np.asarray(raw["start"], dtype=np.float64)
    end = np.asarray(raw["end"], dtype=np.float64)
    vector = end - start
    length = float(np.linalg.norm(vector))
    angle = math.atan2(float(vector[1]), float(vector[0])) % math.pi
    if not families:
        return raw, None, 0.0
    family = min(families, key=lambda item: _angle_delta(angle, math.radians(float(item["angleDeg"]))))
    family_angle = math.radians(float(family["angleDeg"]))
    delta_deg = math.degrees(_angle_delta(angle, family_angle))
    if delta_deg > 5.0:
        return raw, None, round(delta_deg, 4)
    unit = np.asarray([math.cos(family_angle), math.sin(family_angle)], dtype=np.float64)
    if float(np.dot(vector, unit)) < 0:
        unit = -unit
    midpoint = (start + end) * 0.5
    suggested = {"start": _point(midpoint - unit * length * 0.5), "end": _point(midpoint + unit * length * 0.5)}
    return suggested, str(family["id"]), round(delta_deg, 4)


def build_proposals(
    index: CaptureIndex,
    *,
    floor_z: float,
    band_min_m: float = 0.5,
    band_max_m: float = 2.4,
    raster_cell_m: float = 0.05,
    min_length_m: float = 0.8,
    max_gap_m: float = 0.18,
    min_cell_points: int = 2,
    max_points: int = 2_000_000,
) -> dict[str, object]:
    if band_min_m < 0 or band_max_m <= band_min_m:
        raise ValueError("structural height band is invalid")
    if raster_cell_m <= 0 or min_length_m <= 0 or max_gap_m < 0 or max_points < 1000:
        raise ValueError("proposal raster/sampling parameters are invalid")
    indexed_count = int(index.manifest["indexedPointCount"])
    stride = max(1, int(math.ceil(indexed_count / max_points)))
    point_query = index.query_all(z_min=floor_z + band_min_m, z_max=floor_z + band_max_m, every=stride)
    parameters = {
        "floorZ": floor_z,
        "bandMinM": band_min_m,
        "bandMaxM": band_max_m,
        "rasterCellM": raster_cell_m,
        "minLengthM": min_length_m,
        "maxGapM": max_gap_m,
        "minCellPoints": min_cell_points,
    }
    manifest_path = index.root / "capture-index.json"
    if point_query.point_count < 40:
        # Sparse captures degrade to an explicit empty proposal set: the
        # pipeline keeps moving and the Agent authors geometry manually.
        reason = (
            f"structural height band has too few indexed points "
            f"({point_query.point_count} < 40); no automated proposals were generated"
        )
        return {
            "schemaVersion": 2,
            "kind": "structural-proposals",
            "status": "DEGRADED",
            "degradationReason": reason,
            "index": str(index.root),
            "indexFingerprint": index.manifest["indexFingerprint"],
            "indexManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "parameters": {**parameters, "degradedRasterCellM": None},
            "sampling": {
                "stride": stride,
                "pointCount": point_query.point_count,
                "queryStats": point_query.stats,
            },
            "raster": None,
            "axisFamilies": [],
            "faceObservations": [],
            "wallHypotheses": [],
            "wallCandidates": [],
            "observationHypothesisContract": {
                "observationsAreMeasuredFaces": True,
                "hypothesesRemainCandidates": True,
                "singleFaceRequiresTwoAlternatives": True,
            },
            "warnings": [reason],
        }
    xy = np.column_stack((point_query.x, point_query.y))
    min_x = float(np.min(point_query.x))
    max_x = float(np.max(point_query.x))
    min_y = float(np.min(point_query.y))
    max_y = float(np.max(point_query.y))
    # Oversized rasters coarsen deterministically instead of failing; the
    # effective cell is recorded so the loss of resolution stays auditable.
    effective_cell_m = raster_cell_m
    degraded_cell_m: float | None = None
    width = max(1, int(math.ceil((max_x - min_x) / effective_cell_m)) + 1)
    height = max(1, int(math.ceil((max_y - min_y) / effective_cell_m)) + 1)
    while width * height > 50_000_000:
        effective_cell_m *= 2.0
        degraded_cell_m = effective_cell_m
        width = max(1, int(math.ceil((max_x - min_x) / effective_cell_m)) + 1)
        height = max(1, int(math.ceil((max_y - min_y) / effective_cell_m)) + 1)
    col = np.clip(((point_query.x - min_x) / effective_cell_m).astype(np.int32), 0, width - 1)
    row = np.clip(((max_y - point_query.y) / effective_cell_m).astype(np.int32), 0, height - 1)
    counts = np.zeros((height, width), dtype=np.uint32)
    np.add.at(counts, (row, col), 1)
    occupied = (counts >= max(1, int(min_cell_points))).astype(np.uint8) * 255
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8))
    threshold = max(10, int(round(min_length_m / effective_cell_m * 0.45)))
    lines = cv2.HoughLinesP(
        occupied,
        rho=1.0,
        theta=np.pi / 720.0,
        threshold=threshold,
        minLineLength=max(5, int(round(min_length_m / effective_cell_m))),
        maxLineGap=max(1, int(round(max_gap_m / effective_cell_m))),
    )
    # HoughLinesP returns (N,1,4) or (N,4) depending on the OpenCV build.
    raw_lines = [] if lines is None else np.asarray(lines).reshape(-1, 4)
    observations: list[LineObservation] = []
    for index_number, (x0, y0, x1, y1) in enumerate(raw_lines):
        start = np.asarray([min_x + x0 * effective_cell_m, max_y - y0 * effective_cell_m], dtype=np.float64)
        end = np.asarray([min_x + x1 * effective_cell_m, max_y - y1 * effective_cell_m], dtype=np.float64)
        refined = _refine_observation(
            start,
            end,
            xy,
            corridor_m=max(0.055, effective_cell_m * 1.5),
            observation_id=f"raw-face-{index_number + 1:03d}",
        )
        if refined is not None and refined.length >= min_length_m:
            observations.append(refined)
    observations = _deduplicate_observations(observations)
    paired, drift_merged = pair_wall_faces(
        observations,
        index=index,
        z_min=floor_z + band_min_m,
        z_max=floor_z + band_max_m,
        min_overlap_m=min_length_m * 0.7,
    )
    paired_face_ids = {face_id for item in paired for face_id in item["sourceFaceIds"]}
    paired_face_ids.update(face_id for item in drift_merged for face_id in item["driftMergedFrom"])
    candidates = list(paired)
    for merge in drift_merged:
        observation = merge["observation"]
        confidence = min(
            0.78,
            0.30
            + 0.20 * min(1.0, observation.length / 4.0)
            + 0.16 * min(1.0, observation.support_count / 700.0)
            + 0.10 * max(0.0, 1.0 - observation.residual_p90_m / 0.06),
        )
        candidates.append(
            {
                "wallMode": "single-face",
                "sourceFaceIds": list(merge["driftMergedFrom"]),
                "driftMergedFrom": list(merge["driftMergedFrom"]),
                "cavityPointRatio": merge["cavityPointRatio"],
                "faceColorDeltaNorm": merge["faceColorDeltaNorm"],
                "rawCenterline": {"start": _point(observation.start), "end": _point(observation.end)},
                "thicknessM": 0.12,
                "lengthM": round(observation.length, 4),
                "supportPointCount": observation.support_count,
                "fitResidualP50M": round(observation.residual_p50_m, 5),
                "fitResidualP90M": round(observation.residual_p90_m, 5),
                "confidence": round(confidence, 4),
                "warning": (
                    "parallel faces were merged as loop-drift copies of one wall face; "
                    "centreline and thickness require review"
                ),
            }
        )
    for observation in observations:
        if observation.observation_id in paired_face_ids:
            continue
        confidence = min(
            0.78,
            0.30
            + 0.20 * min(1.0, observation.length / 4.0)
            + 0.16 * min(1.0, observation.support_count / 700.0)
            + 0.10 * max(0.0, 1.0 - observation.residual_p90_m / 0.06),
        )
        candidates.append(
            {
                "wallMode": "single-face",
                "sourceFaceIds": [observation.observation_id],
                "rawCenterline": {"start": _point(observation.start), "end": _point(observation.end)},
                "thicknessM": 0.12,
                "lengthM": round(observation.length, 4),
                "supportPointCount": observation.support_count,
                "fitResidualP50M": round(observation.residual_p50_m, 5),
                "fitResidualP90M": round(observation.residual_p90_m, 5),
                "confidence": round(confidence, 4),
                "warning": "only one wall face was observed; centreline and thickness require review",
            }
        )

    families = _axis_families(observations)
    candidates.sort(
        key=lambda item: (
            -float(item["confidence"]),
            float(item["rawCenterline"]["start"][0]),
            float(item["rawCenterline"]["start"][1]),
        )
    )
    for candidate_index, candidate in enumerate(candidates):
        suggested, family_id, delta = _suggested_line(candidate["rawCenterline"], families)
        candidate.update(
            {
                "id": f"wall-proposal-{candidate_index + 1:03d}",
                "status": "candidate",
                "suggestedCenterline": suggested,
                "axisFamilyId": family_id,
                "axisSnapDeltaDeg": delta,
                "authorityRule": "raw and suggested geometry are evidence only; acceptance requires an Agent/reviewer transaction",
            }
        )

    warnings: list[str] = []
    degradation_reason: str | None = None
    if degraded_cell_m is not None:
        degradation_reason = (
            f"proposal raster exceeded 50M cells; rasterCellM was coarsened from "
            f"{raster_cell_m} to {degraded_cell_m}"
        )
        warnings.append(degradation_reason)
    if drift_merged:
        warnings.append(
            f"{len(drift_merged)} parallel face pair(s) were merged as loop-drift copies of a single wall face"
        )
    if not paired:
        warnings.append("no parallel wall-face pairs were found; all thicknesses remain single-face defaults")
    if len(families) < 2:
        warnings.append("fewer than two dominant axis families were found")
    face_observations: list[dict[str, object]] = []
    for observation in observations:
        row = observation.as_dict()
        min_xy = np.minimum(observation.start, observation.end) - 0.10
        max_xy = np.maximum(observation.start, observation.end) + 0.10
        row.update(
            {
                "alongSupportIntervalsM": [[0.0, round(observation.length, 5)]],
                "verticalSupportIntervalsM": [[band_min_m, band_max_m]],
                "sourceTileIds": index._tile_keys(  # noqa: SLF001 - lineage needs exact index tiles
                    float(min_xy[0]), float(min_xy[1]), float(max_xy[0]), float(max_xy[1])
                ),
                "densityPointsPerM": round(observation.support_count / max(observation.length, 1e-6), 4),
                "incidenceStats": {"status": "NOT_AVAILABLE"},
            }
        )
        face_observations.append(row)
    wall_hypotheses = [_wall_hypothesis(candidate) for candidate in candidates]
    return {
        "schemaVersion": 2,
        "kind": "structural-proposals",
        "status": "DEGRADED" if degraded_cell_m is not None else "CANDIDATES_ONLY",
        "degradationReason": degradation_reason,
        "index": str(index.root),
        "indexFingerprint": index.manifest["indexFingerprint"],
        "indexManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "parameters": {**parameters, "degradedRasterCellM": degraded_cell_m},
        "sampling": {
            "stride": stride,
            "pointCount": point_query.point_count,
            "queryStats": point_query.stats,
        },
        "raster": {"originX": min_x, "originY": max_y, "widthPx": width, "heightPx": height, "cellM": effective_cell_m},
        "axisFamilies": families,
        "faceObservations": face_observations,
        "wallHypotheses": wall_hypotheses,
        "observationHypothesisContract": {
            "observationsAreMeasuredFaces": True,
            "hypothesesRemainCandidates": True,
            "singleFaceRequiresTwoAlternatives": True,
        },
        # Compatibility view for the existing clean-room profile and older
        # reviewers.  New global topology code consumes wallHypotheses.
        "wallCandidates": candidates,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--floor-z", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--band-min", type=float, default=0.5)
    parser.add_argument("--band-max", type=float, default=2.4)
    parser.add_argument("--cell", type=float, default=0.05)
    parser.add_argument("--min-length", type=float, default=0.8)
    parser.add_argument("--max-gap", type=float, default=0.18)
    parser.add_argument("--min-cell-points", type=int, default=2)
    parser.add_argument("--max-points", type=int, default=2_000_000)
    parser.add_argument("--validate-source", action="store_true")
    args = parser.parse_args()
    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    report = build_proposals(
        index,
        floor_z=args.floor_z,
        band_min_m=args.band_min,
        band_max_m=args.band_max,
        raster_cell_m=args.cell,
        min_length_m=args.min_length,
        max_gap_m=args.max_gap,
        min_cell_points=args.min_cell_points,
        max_points=args.max_points,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output.resolve()), "wallCandidateCount": len(report["wallCandidates"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
