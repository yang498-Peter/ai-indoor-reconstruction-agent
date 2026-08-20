#!/usr/bin/env python3
"""Candidate continuity, regional axes, and global room-topology proposals.

This module consumes structural proposal evidence and produces a separate,
hash-bound optimization artifact.  It never edits Semantic Scene V2 and never
promotes a candidate to authority.  Single-face observations retain both
possible centreline offsets until an independent evidence/review transaction
resolves the side.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

import numpy as np
from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.ops import polygonize, unary_union


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = ROOT / "profiles" / "default-office-v1.json"
PRODUCER = "candidate-topology-v1"
AUTHORITY_RULE = "proposal-only-agent-review-required"


class CandidateTopologyError(ValueError):
    """Stable, caller-visible input/config error."""


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _point(value: Iterable[float], *, code: str = "TOPOLOGY_POINT_INVALID") -> np.ndarray:
    result = np.asarray(list(value), dtype=np.float64)
    if result.shape != (2,) or not np.isfinite(result).all():
        raise CandidateTopologyError(f"{code}: expected one finite source-plan XY point")
    return result


def _line(value: object, *, code: str = "TOPOLOGY_LINE_INVALID") -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(value, dict):
        raise CandidateTopologyError(f"{code}: line must be an object")
    start = _point(value.get("start", []), code=code)
    end = _point(value.get("end", []), code=code)
    if float(np.linalg.norm(end - start)) < 0.05:
        raise CandidateTopologyError(f"{code}: line is shorter than 0.05 m")
    return start, end


def _line_dict(start: np.ndarray, end: np.ndarray) -> dict[str, list[float]]:
    return {
        "start": [round(float(start[0]), 6), round(float(start[1]), 6)],
        "end": [round(float(end[0]), 6), round(float(end[1]), 6)],
    }


def _angle(start: np.ndarray, end: np.ndarray) -> float:
    return math.atan2(float(end[1] - start[1]), float(end[0] - start[0])) % math.pi


def _angle_delta(first: float, second: float) -> float:
    delta = abs((first - second) % math.pi)
    return min(delta, math.pi - delta)


def _confidence_band(row: dict[str, Any]) -> str:
    residual = float(row.get("fitResidualP90M", row.get("residualP90M", 1.0)))
    support = int(row.get("supportPointCount", 0))
    length = float(row.get("lengthM", 0.0))
    paired = row.get("wallMode") == "paired-faces"
    if paired and residual <= 0.04 and support >= 500 and length >= 1.5:
        return "high"
    if residual <= 0.08 and support >= 120 and length >= 0.8:
        return "medium"
    return "low"


def _validate_profile(profile: object) -> dict[str, Any]:
    if not isinstance(profile, dict) or profile.get("schemaVersion") != 1:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: expected profile schemaVersion 1")
    if not isinstance(profile.get("id"), str) or not profile["id"]:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: profile id is required")
    thresholds = profile.get("thresholds")
    weights = profile.get("weights")
    required_thresholds = {
        "continuityAngleDeg",
        "continuityOffsetM",
        "continuityGapM",
        "zoneRadiusM",
        "axisToleranceDeg",
        "duplicateOffsetM",
        "duplicateOverlapRatio",
        "topologySnapM",
        "minRoomAreaM2",
        "beamWidth",
    }
    required_weights = {
        "dataSupport",
        "dataLength",
        "residual",
        "complexity",
        "axisAlignment",
        "junction",
        "closure",
        "duplicate",
        "unexplained",
    }
    if not isinstance(thresholds, dict) or not required_thresholds.issubset(thresholds):
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: profile thresholds are incomplete")
    if not isinstance(weights, dict) or not required_weights.issubset(weights):
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: profile weights are incomplete")
    for key in required_thresholds - {"beamWidth"}:
        value = thresholds[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise CandidateTopologyError(f"TOPOLOGY_PROFILE_INVALID: threshold {key} must be finite and non-negative")
    if float(thresholds["zoneRadiusM"]) <= 0:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: zoneRadiusM must be positive")
    if float(thresholds["duplicateOverlapRatio"]) > 1:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: duplicateOverlapRatio must not exceed 1")
    beam_width = thresholds["beamWidth"]
    if isinstance(beam_width, bool) or not isinstance(beam_width, int) or beam_width < 2 or beam_width > 4096:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: beamWidth must be an integer in [2,4096]")
    for key in required_weights:
        value = weights[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise CandidateTopologyError(f"TOPOLOGY_PROFILE_INVALID: weight {key} must be finite and non-negative")
    if profile.get("manhattanHardConstraint") is not False:
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: Manhattan may be a prior, never a hard constraint")
    if not isinstance(profile.get("explicitOptInRequired"), bool):
        raise CandidateTopologyError("TOPOLOGY_PROFILE_INVALID: explicitOptInRequired must be boolean")
    return json.loads(json.dumps(profile))


def load_profile(path: Path | None) -> dict[str, Any]:
    profile_path = DEFAULT_PROFILE_PATH if path is None else Path(path).resolve()
    if not profile_path.is_file():
        raise CandidateTopologyError(f"TOPOLOGY_PROFILE_NOT_FOUND: {profile_path}")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateTopologyError(f"TOPOLOGY_PROFILE_INVALID: {error}") from error
    result = _validate_profile(profile)
    result["artifactSha256"] = _sha256(profile_path)
    return result


@dataclass(frozen=True)
class Alternative:
    hypothesis_id: str
    alternative_id: str
    source_candidate_id: str
    start: np.ndarray
    end: np.ndarray
    thickness: float
    support: int
    residual_p90: float
    confidence_band: str
    wall_mode: str
    zone_id: str = ""
    axis_family_id: str | None = None
    axis_delta_deg: float | None = None

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.end - self.start))


def _normalize_observations(document: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    raw = document.get("faceObservations", [])
    if not isinstance(raw, list):
        raise CandidateTopologyError("TOPOLOGY_PROPOSALS_INVALID: faceObservations must be an array")
    observations: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    parameters = document.get("parameters") if isinstance(document.get("parameters"), dict) else {}
    band_min = float(parameters.get("bandMinM", 0.5))
    band_max = float(parameters.get("bandMaxM", 2.4))
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise CandidateTopologyError("TOPOLOGY_PROPOSALS_INVALID: face observation must be an object")
        observation_id = str(row.get("id") or f"face-{index + 1:03d}")
        if observation_id in by_id:
            raise CandidateTopologyError(f"TOPOLOGY_DUPLICATE_ID: {observation_id}")
        start, end = _line(row, code="TOPOLOGY_OBSERVATION_INVALID")
        length = float(np.linalg.norm(end - start))
        normalized = {
            **row,
            "id": observation_id,
            "start": _line_dict(start, end)["start"],
            "end": _line_dict(start, end)["end"],
            "lengthM": round(length, 5),
            "angleDeg": round(math.degrees(_angle(start, end)), 4),
            "alongSupportIntervalsM": row.get("alongSupportIntervalsM", [[0.0, round(length, 5)]]),
            "verticalSupportIntervalsM": row.get("verticalSupportIntervalsM", [[band_min, band_max]]),
            "sourceTileIds": list(row.get("sourceTileIds", [])),
            "densityPointsPerM": round(float(row.get("supportPointCount", 0)) / max(length, 1e-6), 4),
            "incidenceStats": row.get("incidenceStats", {"status": "NOT_AVAILABLE"}),
        }
        observations.append(normalized)
        by_id[observation_id] = normalized
    observations.sort(key=lambda item: str(item["id"]))
    return observations, by_id


def _continuity_segments(observations: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = profile["thresholds"]
    angle_tolerance = math.radians(float(thresholds["continuityAngleDeg"]))
    offset_tolerance = float(thresholds["continuityOffsetM"])
    gap_tolerance = float(thresholds["continuityGapM"])
    remaining = list(observations)
    groups: list[list[dict[str, Any]]] = []
    while remaining:
        seed = remaining.pop(0)
        seed_start = _point(seed["start"])
        seed_end = _point(seed["end"])
        seed_angle = _angle(seed_start, seed_end)
        unit = np.asarray([math.cos(seed_angle), math.sin(seed_angle)], dtype=np.float64)
        normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
        seed_offset = float(np.dot((seed_start + seed_end) * 0.5, normal))
        group = [seed]
        deferred: list[dict[str, Any]] = []
        for row in remaining:
            start = _point(row["start"])
            end = _point(row["end"])
            if _angle_delta(seed_angle, _angle(start, end)) > angle_tolerance:
                deferred.append(row)
                continue
            offset = float(np.dot((start + end) * 0.5, normal))
            if abs(offset - seed_offset) > offset_tolerance:
                deferred.append(row)
                continue
            group.append(row)
        remaining = deferred
        groups.append(group)

    output: list[dict[str, Any]] = []
    segment_index = 0
    for group in groups:
        first = group[0]
        first_start = _point(first["start"])
        first_end = _point(first["end"])
        group_angle = _angle(first_start, first_end)
        unit = np.asarray([math.cos(group_angle), math.sin(group_angle)], dtype=np.float64)
        normal = np.asarray([-unit[1], unit[0]], dtype=np.float64)
        rows: list[tuple[float, float, float, dict[str, Any]]] = []
        for item in group:
            start = _point(item["start"])
            end = _point(item["end"])
            interval = sorted((float(np.dot(start, unit)), float(np.dot(end, unit))))
            offset = float(np.dot((start + end) * 0.5, normal))
            rows.append((interval[0], interval[1], offset, item))
        rows.sort(key=lambda item: (item[0], item[1], str(item[3]["id"])))
        batches: list[list[tuple[float, float, float, dict[str, Any]]]] = []
        for row in rows:
            if batches and row[0] - max(item[1] for item in batches[-1]) <= gap_tolerance:
                batches[-1].append(row)
            else:
                batches.append([row])
        for batch in batches:
            segment_index += 1
            lo = min(item[0] for item in batch)
            hi = max(item[1] for item in batch)
            weights = np.asarray([max(1, int(item[3].get("supportPointCount", 1))) for item in batch])
            offset = float(np.average([item[2] for item in batch], weights=weights))
            actual = [[round(item[0] - lo, 5), round(item[1] - lo, 5)] for item in batch]
            gaps = [max(0.0, batch[index + 1][0] - batch[index][1]) for index in range(len(batch) - 1)]
            output.append(
                {
                    "id": f"continuity-{segment_index:03d}",
                    "status": "observation-only",
                    "sourceObservationIds": [str(item[3]["id"]) for item in batch],
                    "line": _line_dict(unit * lo + normal * offset, unit * hi + normal * offset),
                    "lengthM": round(hi - lo, 5),
                    "supportIntervalsM": actual,
                    "bridgedGapsM": [round(value, 5) for value in gaps if value > 1e-8],
                    "maxBridgedGapM": round(max(gaps), 5) if gaps else 0.0,
                    "supportPointCount": int(sum(int(item[3].get("supportPointCount", 0)) for item in batch)),
                    "authorityRule": "continuity describes measured returns; it does not fill a gap with wall authority",
                }
            )
    return output


def _offset_line(line: dict[str, Any], offset: float) -> dict[str, list[float]]:
    start, end = _line(line)
    angle = _angle(start, end)
    normal = np.asarray([-math.sin(angle), math.cos(angle)], dtype=np.float64)
    return _line_dict(start + normal * offset, end + normal * offset)


def _normalize_hypotheses(document: dict[str, Any]) -> list[dict[str, Any]]:
    source = document.get("wallHypotheses")
    if source is None:
        source = document.get("wallCandidates", [])
    if not isinstance(source, list):
        raise CandidateTopologyError("TOPOLOGY_PROPOSALS_INVALID: wall hypotheses must be an array")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, row in enumerate(source):
        if not isinstance(row, dict):
            raise CandidateTopologyError("TOPOLOGY_PROPOSALS_INVALID: wall hypothesis must be an object")
        source_id = str(row.get("sourceCandidateId") or row.get("id") or f"wall-proposal-{index + 1:03d}")
        if source_id in ids:
            raise CandidateTopologyError(f"TOPOLOGY_DUPLICATE_ID: {source_id}")
        ids.add(source_id)
        if row.get("status", "candidate") != "candidate":
            continue
        hypothesis_id = str(row.get("hypothesisId") or f"hypothesis:{source_id}")
        wall_mode = str(row.get("wallMode", "single-face"))
        thickness = float(row.get("thicknessM", row.get("thicknessDistribution", {}).get("medianM", 0.12)))
        if not math.isfinite(thickness) or thickness <= 0 or thickness > 1.0:
            raise CandidateTopologyError(f"TOPOLOGY_HYPOTHESIS_INVALID: invalid thickness for {source_id}")
        alternatives = row.get("alternatives") or row.get("centerlineAlternatives")
        if alternatives is None:
            raw = row.get("rawCenterline") or row.get("observedFaceLine")
            _line(raw, code="TOPOLOGY_HYPOTHESIS_INVALID")
            if wall_mode == "single-face":
                alternatives = [
                    {"id": f"{hypothesis_id}:negative", "side": "negative", "centerline": _offset_line(raw, -thickness * 0.5)},
                    {"id": f"{hypothesis_id}:positive", "side": "positive", "centerline": _offset_line(raw, thickness * 0.5)},
                ]
            else:
                alternatives = [{"id": f"{hypothesis_id}:center", "side": "paired", "centerline": raw}]
        normalized_alternatives: list[dict[str, Any]] = []
        for alternative_index, alternative in enumerate(alternatives):
            if not isinstance(alternative, dict):
                raise CandidateTopologyError(f"TOPOLOGY_HYPOTHESIS_INVALID: alternative for {source_id} is invalid")
            line = alternative.get("centerline", alternative)
            start, end = _line(line, code="TOPOLOGY_HYPOTHESIS_INVALID")
            normalized_alternatives.append(
                {
                    "id": str(alternative.get("id") or f"{hypothesis_id}:alternative-{alternative_index + 1}"),
                    "side": str(alternative.get("side", "unknown")),
                    "centerline": _line_dict(start, end),
                }
            )
        if wall_mode == "single-face" and len(normalized_alternatives) != 2:
            raise CandidateTopologyError(
                f"TOPOLOGY_SINGLE_FACE_ALTERNATIVES_REQUIRED: {source_id} must keep both centreline sides"
            )
        result.append(
            {
                "id": hypothesis_id,
                "sourceCandidateId": source_id,
                "status": "candidate",
                "wallMode": wall_mode,
                "sourceObservationIds": list(row.get("sourceObservationIds", row.get("sourceFaceIds", []))),
                "alternatives": normalized_alternatives,
                "thicknessDistribution": {
                    "medianM": round(thickness, 5),
                    "minM": round(float(row.get("thicknessMinM", thickness)), 5),
                    "maxM": round(float(row.get("thicknessMaxM", thickness)), 5),
                    "source": "paired-faces" if wall_mode == "paired-faces" else "versioned-profile-prior",
                },
                "supportPointCount": int(row.get("supportPointCount", 0)),
                "residualStats": {
                    "p50M": float(row.get("fitResidualP50M", 0.0)),
                    "p90M": float(row.get("fitResidualP90M", 1.0)),
                },
                "confidenceBand": str(row.get("confidenceBand") or _confidence_band(row)),
                "rawFeatures": {
                    "lengthM": float(row.get("lengthM", 0.0)),
                    "supportPointCount": int(row.get("supportPointCount", 0)),
                    "fitResidualP50M": float(row.get("fitResidualP50M", 0.0)),
                    "fitResidualP90M": float(row.get("fitResidualP90M", 1.0)),
                    "legacyConfidence": row.get("confidence"),
                },
                "authorityEligibility": (
                    "measurement-eligible-after-independent-review"
                    if wall_mode == "paired-faces"
                    else "inferred-only-until-corroborated"
                ),
            }
        )
    return sorted(result, key=lambda item: str(item["sourceCandidateId"]))


def _preferred_alternative(hypothesis: dict[str, Any]) -> dict[str, Any]:
    return sorted(hypothesis["alternatives"], key=lambda item: str(item["id"]))[0]


def _zones(hypotheses: list[dict[str, Any]], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not hypotheses:
        return [], {}
    radius = float(profile["thresholds"]["zoneRadiusM"])
    rows: list[tuple[str, np.ndarray, float, float]] = []
    for hypothesis in hypotheses:
        alternative = _preferred_alternative(hypothesis)
        start, end = _line(alternative["centerline"])
        rows.append((str(hypothesis["id"]), (start + end) * 0.5, _angle(start, end), float(np.linalg.norm(end - start))))
    remaining = set(range(len(rows)))
    components: list[list[int]] = []
    while remaining:
        seed = min(remaining, key=lambda index: rows[index][0])
        remaining.remove(seed)
        queue = [seed]
        component = [seed]
        while queue:
            current = queue.pop(0)
            neighbours = [
                other for other in sorted(remaining)
                if float(np.linalg.norm(rows[current][1] - rows[other][1])) <= radius
            ]
            for other in neighbours:
                remaining.remove(other)
                queue.append(other)
                component.append(other)
        components.append(component)
    components.sort(key=lambda component: min(rows[index][0] for index in component))
    mapping: dict[str, str] = {}
    zones: list[dict[str, Any]] = []
    tolerance = math.radians(float(profile["thresholds"]["axisToleranceDeg"]))
    for zone_index, component in enumerate(components, 1):
        zone_id = f"zone-{zone_index:03d}"
        members = [rows[index] for index in component]
        for hypothesis_id, _midpoint, _angle_value, _length in members:
            mapping[hypothesis_id] = zone_id
        unassigned = set(range(len(members)))
        families: list[dict[str, Any]] = []
        while unassigned:
            seed = max(
                unassigned,
                key=lambda index: sum(
                    members[other][3] for other in unassigned
                    if _angle_delta(members[index][2], members[other][2]) <= tolerance
                ),
            )
            family_members = [
                index for index in unassigned
                if _angle_delta(members[seed][2], members[index][2]) <= tolerance
            ]
            sin_sum = sum(members[index][3] * math.sin(2.0 * members[index][2]) for index in family_members)
            cos_sum = sum(members[index][3] * math.cos(2.0 * members[index][2]) for index in family_members)
            angle_value = (0.5 * math.atan2(sin_sum, cos_sum)) % math.pi
            families.append(
                {
                    "id": f"{zone_id}:axis-{len(families) + 1}",
                    "angleDeg": round(math.degrees(angle_value), 4),
                    "supportLengthM": round(sum(members[index][3] for index in family_members), 5),
                    "hypothesisIds": sorted(members[index][0] for index in family_members),
                }
            )
            unassigned.difference_update(family_members)
        families.sort(key=lambda item: (-float(item["supportLengthM"]), str(item["id"])))
        zones.append(
            {
                "id": zone_id,
                "hypothesisIds": sorted(member[0] for member in members),
                "bounds": {
                    "minX": round(min(float(member[1][0]) for member in members), 6),
                    "minY": round(min(float(member[1][1]) for member in members), 6),
                    "maxX": round(max(float(member[1][0]) for member in members), 6),
                    "maxY": round(max(float(member[1][1]) for member in members), 6),
                },
                "axisFamilies": families,
                "manhattanHardConstraint": False,
            }
        )
    return zones, mapping


def _alternatives(
    hypotheses: list[dict[str, Any]], zones: list[dict[str, Any]], mapping: dict[str, str]
) -> dict[str, list[Alternative]]:
    families = {
        str(family["id"]): math.radians(float(family["angleDeg"]))
        for zone in zones for family in zone["axisFamilies"]
    }
    family_zones = {str(family["id"]): str(zone["id"]) for zone in zones for family in zone["axisFamilies"]}
    result: dict[str, list[Alternative]] = {}
    for hypothesis in hypotheses:
        zone_id = mapping[str(hypothesis["id"])]
        zone_families = [(family_id, angle) for family_id, angle in families.items() if family_zones[family_id] == zone_id]
        choices: list[Alternative] = []
        for row in hypothesis["alternatives"]:
            start, end = _line(row["centerline"])
            angle_value = _angle(start, end)
            family_id: str | None = None
            family_delta: float | None = None
            if zone_families:
                family_id, family_angle = min(zone_families, key=lambda item: _angle_delta(angle_value, item[1]))
                family_delta = math.degrees(_angle_delta(angle_value, family_angle))
            choices.append(
                Alternative(
                    hypothesis_id=str(hypothesis["id"]),
                    alternative_id=str(row["id"]),
                    source_candidate_id=str(hypothesis["sourceCandidateId"]),
                    start=start,
                    end=end,
                    thickness=float(hypothesis["thicknessDistribution"]["medianM"]),
                    support=int(hypothesis["supportPointCount"]),
                    residual_p90=float(hypothesis["residualStats"]["p90M"]),
                    confidence_band=str(hypothesis["confidenceBand"]),
                    wall_mode=str(hypothesis["wallMode"]),
                    zone_id=zone_id,
                    axis_family_id=family_id,
                    axis_delta_deg=family_delta,
                )
            )
        result[str(hypothesis["id"])] = choices
    return result


def _unary_score(alternative: Alternative, profile: dict[str, Any]) -> float:
    weights = profile["weights"]
    residual_scale = max(0.0, min(2.0, alternative.residual_p90 / 0.08))
    axis_delta = alternative.axis_delta_deg if alternative.axis_delta_deg is not None else 90.0
    axis_score = max(0.0, 1.0 - axis_delta / max(1.0, float(profile["thresholds"]["axisToleranceDeg"])))
    return (
        float(weights["dataSupport"]) * math.log1p(max(0, alternative.support)) / 5.0
        + float(weights["dataLength"]) * alternative.length
        - float(weights["residual"]) * residual_scale
        - float(weights["complexity"])
        + float(weights["axisAlignment"]) * axis_score
    )


def _snap_lines(alternatives: list[Alternative], tolerance: float) -> list[LineString]:
    if not alternatives:
        return []
    endpoints = [point.copy() for item in alternatives for point in (item.start, item.end)]
    parent = list(range(len(endpoints)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[max(root_first, root_second)] = min(root_first, root_second)

    for first in range(len(endpoints)):
        for second in range(first + 1, len(endpoints)):
            if float(np.linalg.norm(endpoints[first] - endpoints[second])) <= tolerance:
                union(first, second)
    clusters: dict[int, list[np.ndarray]] = {}
    for index, point in enumerate(endpoints):
        clusters.setdefault(find(index), []).append(point)
    centres = {root: np.mean(points, axis=0) for root, points in clusters.items()}
    snapped = [centres[find(index)].copy() for index in range(len(endpoints))]

    # Snap a loose endpoint to a nearby interior point of another segment so
    # T-junctions close without forcing the other wall endpoint to move.
    for endpoint_index, point in enumerate(snapped):
        own_line = endpoint_index // 2
        best: tuple[float, np.ndarray] | None = None
        for line_index, _item in enumerate(alternatives):
            if line_index == own_line:
                continue
            segment_start = snapped[line_index * 2]
            segment_end = snapped[line_index * 2 + 1]
            vector = segment_end - segment_start
            length_squared = float(np.dot(vector, vector))
            parameter = float(np.dot(point - segment_start, vector) / max(length_squared, 1e-12))
            if parameter <= 1e-6 or parameter >= 1.0 - 1e-6:
                continue
            projected = segment_start + vector * parameter
            distance = float(np.linalg.norm(point - projected))
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, projected)
        if best is not None:
            snapped[endpoint_index] = best[1]

    # Explicitly split host segments at snapped T endpoints.  Depending on
    # floating-point magnitude, GEOS may not node an endpoint that is only a
    # few ulps from a segment; sharing the exact coordinate avoids that drift.
    lines: list[LineString] = []
    for index in range(len(alternatives)):
        start, end = snapped[index * 2], snapped[index * 2 + 1]
        vector = end - start
        length_squared = float(np.dot(vector, vector))
        if math.sqrt(length_squared) < 0.05:
            continue
        cuts: list[tuple[float, np.ndarray]] = [(0.0, start), (1.0, end)]
        for endpoint_index, endpoint in enumerate(snapped):
            if endpoint_index // 2 == index:
                continue
            parameter = float(np.dot(endpoint - start, vector) / max(length_squared, 1e-12))
            if parameter <= 1e-6 or parameter >= 1.0 - 1e-6:
                continue
            projected = start + vector * parameter
            if float(np.linalg.norm(endpoint - projected)) <= tolerance:
                cuts.append((parameter, endpoint))
        cuts.sort(key=lambda item: item[0])
        deduplicated: list[tuple[float, np.ndarray]] = []
        for parameter, point in cuts:
            if deduplicated and abs(parameter - deduplicated[-1][0]) <= 1e-8:
                continue
            deduplicated.append((parameter, point))
        for first, second in zip(deduplicated, deduplicated[1:]):
            if float(np.linalg.norm(second[1] - first[1])) >= 0.05:
                lines.append(LineString([first[1].tolist(), second[1].tolist()]))
    return lines


def _topology(alternatives: list[Alternative], profile: dict[str, Any]) -> dict[str, Any]:
    lines = _snap_lines(alternatives, float(profile["thresholds"]["topologySnapM"]))
    if not lines:
        return {"rooms": [], "roomCount": 0, "adjacencies": [], "adjacencyCount": 0, "lineComponentCount": 0}
    merged = unary_union(MultiLineString(lines))
    min_area = float(profile["thresholds"]["minRoomAreaM2"])
    polygons = [polygon for polygon in polygonize(merged) if polygon.is_valid and float(polygon.area) >= min_area]
    polygons.sort(key=lambda polygon: (round(float(polygon.centroid.x), 6), round(float(polygon.centroid.y), 6)))
    rooms = [
        {
            "id": f"room-candidate-{index + 1:03d}",
            "polygon": [[round(float(x), 6), round(float(y), 6)] for x, y in list(polygon.exterior.coords)[:-1]],
            "areaM2": round(float(polygon.area), 5),
            "status": "candidate",
        }
        for index, polygon in enumerate(polygons)
    ]
    adjacencies: list[dict[str, Any]] = []
    for first in range(len(polygons)):
        for second in range(first + 1, len(polygons)):
            shared = polygons[first].boundary.intersection(polygons[second].boundary)
            if float(shared.length) > 0.05:
                adjacencies.append(
                    {
                        "roomIds": [rooms[first]["id"], rooms[second]["id"]],
                        "sharedBoundaryLengthM": round(float(shared.length), 5),
                        "status": "candidate",
                    }
                )
    line_parts = [merged] if isinstance(merged, LineString) else [part for part in merged.geoms if isinstance(part, LineString)]
    parents = list(range(len(line_parts)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    endpoint_owner: dict[tuple[float, float], int] = {}
    for index, part in enumerate(line_parts):
        for coordinate in (part.coords[0], part.coords[-1]):
            key = (round(float(coordinate[0]), 8), round(float(coordinate[1]), 8))
            owner = endpoint_owner.get(key)
            if owner is None:
                endpoint_owner[key] = index
                continue
            first, second = find(index), find(owner)
            if first != second:
                parents[first] = second
    component_count = len({find(index) for index in range(len(line_parts))})
    return {
        "rooms": rooms,
        "roomCount": len(rooms),
        "adjacencies": adjacencies,
        "adjacencyCount": len(adjacencies),
        "lineComponentCount": component_count,
    }


def _selection(alternatives_by_hypothesis: dict[str, list[Alternative]], profile: dict[str, Any]) -> tuple[list[Alternative], dict[str, float]]:
    beam_width = int(profile["thresholds"]["beamWidth"])
    groups = sorted(alternatives_by_hypothesis)
    all_alternatives = [item for group in groups for item in alternatives_by_hypothesis[group]]
    alternative_index = {item.alternative_id: index for index, item in enumerate(all_alternatives)}
    pair_adjustment = np.zeros((len(all_alternatives), len(all_alternatives)), dtype=np.float64)
    duplicate_conflict = np.zeros((len(all_alternatives), len(all_alternatives)), dtype=np.bool_)
    snap = float(profile["thresholds"]["topologySnapM"])
    duplicate_offset = float(profile["thresholds"]["duplicateOffsetM"])
    duplicate_overlap = float(profile["thresholds"]["duplicateOverlapRatio"])
    angle_tolerance = math.radians(float(profile["thresholds"]["axisToleranceDeg"]))
    weights = profile["weights"]

    def endpoint_segment_distance_squared(px: float, py: float, line: tuple[float, ...]) -> float:
        dx, dy = line[2] - line[0], line[3] - line[1]
        parameter = ((px - line[0]) * dx + (py - line[1]) * dy) / max(dx * dx + dy * dy, 1e-12)
        parameter = min(1.0, max(0.0, parameter))
        closest_x, closest_y = line[0] + parameter * dx, line[1] + parameter * dy
        return (px - closest_x) ** 2 + (py - closest_y) ** 2

    geometry: list[tuple[float, ...]] = []
    for item in all_alternatives:
        sx, sy = float(item.start[0]), float(item.start[1])
        ex, ey = float(item.end[0]), float(item.end[1])
        length = math.hypot(ex - sx, ey - sy)
        ux, uy = (ex - sx) / length, (ey - sy) / length
        geometry.append(
            (
                sx, sy, ex, ey, length, math.atan2(uy, ux) % math.pi,
                ux, uy, -uy, ux, (sx + ex) * 0.5, (sy + ey) * 0.5,
                min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey),
            )
        )
    for first_index, first in enumerate(all_alternatives):
        for second_index in range(first_index + 1, len(all_alternatives)):
            second = all_alternatives[second_index]
            if first.zone_id != second.zone_id:
                continue
            first_geometry = geometry[first_index]
            second_geometry = geometry[second_index]
            bbox_gap_x = max(
                0.0,
                max(first_geometry[12], second_geometry[12])
                - min(first_geometry[14], second_geometry[14]),
            )
            bbox_gap_y = max(
                0.0,
                max(first_geometry[13], second_geometry[13])
                - min(first_geometry[15], second_geometry[15]),
            )
            if math.hypot(bbox_gap_x, bbox_gap_y) > max(snap, duplicate_offset):
                continue
            duplicate = 0.0
            if _angle_delta(first_geometry[5], second_geometry[5]) <= angle_tolerance:
                ux, uy, nx, ny = first_geometry[6:10]
                first_interval = sorted((first_geometry[0] * ux + first_geometry[1] * uy,
                                         first_geometry[2] * ux + first_geometry[3] * uy))
                second_interval = sorted((second_geometry[0] * ux + second_geometry[1] * uy,
                                          second_geometry[2] * ux + second_geometry[3] * uy))
                overlap = max(0.0, min(first_interval[1], second_interval[1]) - max(first_interval[0], second_interval[0]))
                offset = abs((first_geometry[10] - second_geometry[10]) * nx +
                             (first_geometry[11] - second_geometry[11]) * ny)
                ratio = overlap / max(min(first_geometry[4], second_geometry[4]), 1e-6)
                if offset <= duplicate_offset and ratio >= duplicate_overlap:
                    duplicate = ratio
                    duplicate_conflict[first_index, second_index] = True
                    duplicate_conflict[second_index, first_index] = True
            adjustment = -float(weights["duplicate"]) * duplicate
            distances_squared = (
                endpoint_segment_distance_squared(first_geometry[0], first_geometry[1], second_geometry),
                endpoint_segment_distance_squared(first_geometry[2], first_geometry[3], second_geometry),
                endpoint_segment_distance_squared(second_geometry[0], second_geometry[1], first_geometry),
                endpoint_segment_distance_squared(second_geometry[2], second_geometry[3], first_geometry),
            )
            if min(distances_squared) <= snap * snap:
                adjustment += float(weights["junction"])
            pair_adjustment[first_index, second_index] = adjustment
            pair_adjustment[second_index, first_index] = adjustment
    unary = np.asarray([_unary_score(item, profile) for item in all_alternatives], dtype=np.float64)
    group_indices = {
        hypothesis_id: [alternative_index[item.alternative_id] for item in alternatives_by_hypothesis[hypothesis_id]]
        for hypothesis_id in groups
    }
    states: list[tuple[float, tuple[int, ...]]] = [(0.0, tuple())]
    for hypothesis_id in groups:
        expanded: list[tuple[float, tuple[int, ...]]] = []
        for score, selected in states:
            expanded.append((score, selected))
            for candidate_index in group_indices[hypothesis_id]:
                interactions = sum(float(pair_adjustment[existing, candidate_index]) for existing in selected)
                expanded.append((score + float(unary[candidate_index]) + interactions, selected + (candidate_index,)))
        expanded.sort(
            key=lambda state: (
                -state[0],
                tuple(all_alternatives[index].source_candidate_id for index in state[1]),
                tuple(all_alternatives[index].alternative_id for index in state[1]),
            )
        )
        states = expanded[:beam_width]

    best_score = -math.inf
    best_selected: tuple[int, ...] = tuple()
    best_terms: dict[str, float] = {}
    all_hypotheses = set(groups)
    # Polygonisation is the expensive exact term.  Evaluate every bounded beam
    # finalist so the closure term can actually change the global result.
    finalists = states
    for incremental, selected in finalists:
        selected_alternatives = [all_alternatives[index] for index in selected]
        topology = _topology(selected_alternatives, profile)
        closure = float(profile["weights"]["closure"]) * float(topology["roomCount"])
        selected_hypotheses = {item.hypothesis_id for item in selected_alternatives}
        unexplained = 0.0
        for hypothesis_id in all_hypotheses - selected_hypotheses:
            choices = alternatives_by_hypothesis[hypothesis_id]
            choice_indices = [alternative_index[choice.alternative_id] for choice in choices]
            strongest = max((float(unary[index]) for index in choice_indices), default=0.0)
            explained_by_duplicate = any(
                bool(duplicate_conflict[choice_index, accepted_index])
                for choice_index in choice_indices for accepted_index in selected
            )
            if strongest > 0 and not explained_by_duplicate:
                unexplained += strongest
        omission_penalty = float(profile["weights"]["unexplained"]) * unexplained
        total = incremental + closure - omission_penalty
        if total > best_score + 1e-12:
            best_score = total
            best_selected = selected
            best_terms = {
                "incrementalDataAndConstraint": round(incremental, 6),
                "roomClosureBonus": round(closure, 6),
                "unexplainedCandidatePenalty": round(omission_penalty, 6),
                "total": round(total, 6),
            }
    return [all_alternatives[index] for index in best_selected], best_terms


def _validate_boundaries(value: object | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CandidateTopologyError("TOPOLOGY_BOUNDARY_INVALID: boundary semantics must be an object")
    allowed = {"scanCoverageBoundary", "floorSupportPolygon", "inferredRoomBoundary"}
    unknown = set(value) - allowed
    if unknown:
        raise CandidateTopologyError(f"TOPOLOGY_BOUNDARY_INVALID: unknown keys {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, polygon in value.items():
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise CandidateTopologyError(f"TOPOLOGY_BOUNDARY_INVALID: {key} must have at least three vertices")
        points = [_point(point, code="TOPOLOGY_BOUNDARY_INVALID") for point in polygon]
        shape = Polygon([point.tolist() for point in points])
        if not shape.is_valid or float(shape.area) <= 0:
            raise CandidateTopologyError(f"TOPOLOGY_BOUNDARY_INVALID: {key} polygon is invalid")
        result[key] = [[float(point[0]), float(point[1])] for point in points]
    return result


def optimize_topology(
    proposals_document: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    boundary_semantics: dict[str, Any] | None = None,
    proposals_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(proposals_document, dict):
        raise CandidateTopologyError("TOPOLOGY_PROPOSALS_INVALID: proposals must be an object")
    active_profile = _validate_profile(profile) if profile is not None else load_profile(None)
    observations, observations_by_id = _normalize_observations(proposals_document)
    continuity = _continuity_segments(observations, active_profile)
    hypotheses = _normalize_hypotheses(proposals_document)
    zones, zone_mapping = _zones(hypotheses, active_profile)
    alternatives_by_hypothesis = _alternatives(hypotheses, zones, zone_mapping)
    selected, objective = _selection(alternatives_by_hypothesis, active_profile)
    topology = _topology(selected, active_profile)
    selected_hypotheses = {item.hypothesis_id for item in selected}
    selected_sources = {item.source_candidate_id for item in selected}
    all_sources = {str(item["sourceCandidateId"]) for item in hypotheses}
    blocking_codes: list[str] = []
    if not hypotheses:
        blocking_codes.append("TOPOLOGY_NO_ELIGIBLE_HYPOTHESES")
    if selected and topology["roomCount"] == 0:
        blocking_codes.append("TOPOLOGY_NO_CLOSED_ROOM_CANDIDATE")
    status = "CANDIDATE_SELECTION_PROPOSED" if selected and not blocking_codes else "REVIEW_REQUIRED"
    code_path = Path(__file__).resolve()
    input_digest = proposals_sha256 or _canonical_hash(proposals_document)
    return {
        "schemaVersion": "1.0",
        "kind": "candidate-topology-optimization",
        "status": status,
        "blockingCodes": blocking_codes,
        "authorityRule": AUTHORITY_RULE,
        "profile": {
            "id": active_profile["id"],
            "artifactSha256": active_profile.get("artifactSha256"),
            "explicitOptInRequired": bool(active_profile.get("explicitOptInRequired", False)),
            "manhattanHardConstraint": False,
        },
        "inputHashes": {
            "proposalsSha256": input_digest,
            "indexManifestSha256": proposals_document.get("indexManifestSha256"),
            "indexFingerprint": proposals_document.get("indexFingerprint"),
        },
        "configDigest": _canonical_hash({key: value for key, value in active_profile.items() if key != "artifactSha256"}),
        "producer": {
            "name": PRODUCER,
            "codeSha256": _sha256(code_path),
        },
        "observationHypothesisContract": {
            "observationsAreMeasuredFaces": True,
            "hypothesesRemainCandidates": True,
            "singleFaceRequiresTwoAlternatives": True,
        },
        "observations": observations,
        "observationsById": {
            observation_id: {"index": index}
            for index, observation_id in enumerate(sorted(observations_by_id))
        },
        "continuitySegments": continuity,
        "wallHypotheses": hypotheses,
        "zones": zones,
        "selection": {
            "status": "PROPOSED_NOT_AUTHORITY",
            "selectedHypothesisIds": sorted(selected_hypotheses),
            "selectedAlternativeIds": sorted(item.alternative_id for item in selected),
            "selectedSourceCandidateIds": sorted(selected_sources),
            "rejectedSourceCandidateIds": sorted(all_sources - selected_sources),
            "objective": objective,
        },
        "topology": topology,
        "boundarySemantics": _validate_boundaries(boundary_semantics),
        "boundaryPromotionRule": "scan/floor/inferred polygons never become walls without wall evidence",
        "lineage": {
            "artifactType": "candidate-topology-optimization",
            "captureIndexFingerprint": proposals_document.get("indexFingerprint"),
            "rootContentSha256s": list((proposals_document.get("lineage") or {}).get("rootContentSha256s", [])),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--boundaries", type=Path)
    args = parser.parse_args()
    try:
        proposals_path = args.proposals.resolve()
        proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
        active_profile = load_profile(args.profile)
        boundaries = json.loads(args.boundaries.read_text(encoding="utf-8")) if args.boundaries else None
        report = optimize_topology(
            proposals,
            profile=active_profile,
            boundary_semantics=boundaries,
            proposals_sha256=_sha256(proposals_path),
        )
        report["input"] = {
            "proposals": str(proposals_path),
            "profile": str((args.profile or DEFAULT_PROFILE_PATH).resolve()),
            "boundaries": str(args.boundaries.resolve()) if args.boundaries else None,
        }
        _atomic_json(args.output.resolve(), report)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CandidateTopologyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "status": report["status"],
                "selected": len(report["selection"]["selectedHypothesisIds"]),
                "rooms": report["topology"]["roomCount"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
