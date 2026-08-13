#!/usr/bin/env python3
"""Fail-closed quality score for a semantic indoor scene and visual receipt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_point(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) in {2, 3}
        and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value)
    )


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def signed_polygon_area(points: list[list[float]]) -> float:
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )


def convex_intersection(subject: list[list[float]], clip: list[list[float]]) -> list[list[float]]:
    """Clip a convex subject polygon to a convex parent without external geometry libraries."""
    output = [list(map(float, point)) for point in subject]
    orientation = 1.0 if signed_polygon_area(clip) >= 0 else -1.0

    def cross(a: list[float], b: list[float]) -> float:
        return a[0] * b[1] - a[1] * b[0]

    def inside(point: list[float], start: list[float], end: list[float]) -> bool:
        return orientation * cross(
            [end[0] - start[0], end[1] - start[1]],
            [point[0] - start[0], point[1] - start[1]],
        ) >= -1e-9

    def intersection(first: list[float], second: list[float], start: list[float], end: list[float]) -> list[float]:
        direction = [second[0] - first[0], second[1] - first[1]]
        edge = [end[0] - start[0], end[1] - start[1]]
        denominator = cross(direction, edge)
        if abs(denominator) < 1e-12:
            return second
        offset = [start[0] - first[0], start[1] - first[1]]
        factor = cross(offset, edge) / denominator
        return [first[0] + factor * direction[0], first[1] + factor * direction[1]]

    for index, start in enumerate(clip):
        end = clip[(index + 1) % len(clip)]
        input_points, output = output, []
        if not input_points:
            break
        previous = input_points[-1]
        for current in input_points:
            current_inside = inside(current, start, end)
            previous_inside = inside(previous, start, end)
            if current_inside:
                if not previous_inside:
                    output.append(intersection(previous, current, start, end))
                output.append(current)
            elif previous_inside:
                output.append(intersection(previous, current, start, end))
            previous = current
    return output


def recompute_derived_geometry(item: dict[str, Any], parent: dict[str, Any]) -> tuple[float, float] | None:
    footprint = item.get("footprint")
    parent_points = parent.get("points") or parent.get("polygon")
    if (
        not isinstance(footprint, list) or len(footprint) != 4
        or not all(finite_point(point) and len(point) == 2 for point in footprint)
        or not isinstance(parent_points, list) or len(parent_points) < 3
        or not all(finite_point(point) for point in parent_points)
    ):
        return None
    parent_plan = [[float(point[0]), float(point[2] if len(point) == 3 else point[1])] for point in parent_points]
    child_plan = [[float(point[0]), float(point[1])] for point in footprint]
    child_area = abs(signed_polygon_area(child_plan))
    if child_area <= 1e-9:
        return None
    clipped = convex_intersection(child_plan, parent_plan)
    containment = abs(signed_polygon_area(clipped)) / child_area if len(clipped) >= 3 else 0.0
    parent_axis = [parent_plan[1][0] - parent_plan[0][0], parent_plan[1][1] - parent_plan[0][1]]
    child_axis = [child_plan[1][0] - child_plan[0][0], child_plan[1][1] - child_plan[0][1]]
    parent_length = math.hypot(*parent_axis)
    child_length = math.hypot(*child_axis)
    if parent_length <= 1e-9 or child_length <= 1e-9:
        return None
    cosine = abs((parent_axis[0] * child_axis[0] + parent_axis[1] * child_axis[1]) / (parent_length * child_length))
    axis_delta = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    return containment, axis_delta


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def parse_review_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def canonical_polygon(points: list[list[float]]) -> tuple[tuple[float, ...], ...]:
    rounded = [tuple(round(float(value), 4) for value in point) for point in points]
    rotations = []
    for ordered in (rounded, list(reversed(rounded))):
        rotations.extend(tuple(ordered[index:] + ordered[:index]) for index in range(len(ordered)))
    return min(rotations)


def canonical_geometry(item: dict[str, Any]) -> tuple[Any, ...] | None:
    geometry_type = item.get("geometryType")
    vertical = (
        round(float(item.get("baseHeight", 0.0)), 4) if finite_number(item.get("baseHeight", 0.0)) else None,
        round(float(item.get("height", 0.0)), 4) if finite_number(item.get("height", 0.0)) else None,
        round(float(item.get("thickness", 0.0)), 4) if finite_number(item.get("thickness", 0.0)) else None,
    )
    if geometry_type == "segment":
        start, end = item.get("start"), item.get("end")
        if not finite_point(start) or not finite_point(end) or start == end:
            return None
        endpoints = sorted(tuple(round(float(value), 4) for value in point) for point in (start, end))
        return (geometry_type, tuple(endpoints), vertical)
    if geometry_type == "rectangle":
        center, size = item.get("center"), item.get("size")
        yaw = item.get("yaw", 0.0)
        if not finite_point(center) or not finite_point(size) or not finite_number(yaw):
            return None
        if any(float(value) <= 0 for value in size):
            return None
        return (
            geometry_type,
            tuple(round(float(value), 4) for value in center),
            tuple(round(float(value), 4) for value in size),
            round(float(yaw) % math.pi, 4),
            vertical,
        )
    if geometry_type == "polygon":
        polygon = item.get("polygon") or item.get("points")
        if not isinstance(polygon, list) or len(polygon) < 3 or not all(finite_point(point) for point in polygon):
            return None
        return (geometry_type, canonical_polygon(polygon), vertical)
    return None


def resolve_evidence_path(path_value: Any, scene_path: Path, scene: dict[str, Any]) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    normalized = Path(path_value.replace("\\", "/"))
    if normalized.is_absolute():
        return normalized.resolve()
    source_root_value = scene.get("source", {}).get("dataDirectory")
    source_root = Path(source_root_value) if isinstance(source_root_value, str) and source_root_value else None
    if path_value.replace("\\", "/").startswith(("panorama/", "undistort/")) and source_root:
        return (source_root / normalized).resolve()
    prototype_root = scene_path.parent.parent if scene_path.parent.name.lower() == "generated" else scene_path.parent
    return (prototype_root / normalized).resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--visual-review", required=True, type=Path)
    parser.add_argument("--wall-joint-review", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    raw_scene = args.scene.read_bytes()
    scene = json.loads(raw_scene.decode("utf-8"))
    review = load_json(args.visual_review)
    scene_hash = hashlib.sha256(raw_scene).hexdigest()
    errors: list[str] = []
    checks: dict[str, Any] = {}

    candidates = scene.get("structureCandidates", [])
    checks["candidateCount"] = len(candidates) if isinstance(candidates, list) else None
    if not isinstance(candidates, list) or candidates:
        errors.append("unresolved structure candidates remain")

    wall_joint_review_path = args.wall_joint_review or args.scene.with_name("wall-joint-review.json")
    wall_joint_review = load_json(wall_joint_review_path) if wall_joint_review_path.is_file() else None
    wall_joint_review_ok = (
        isinstance(wall_joint_review, dict)
        and wall_joint_review.get("status") == "PASS"
        and wall_joint_review.get("sceneSha256") == scene_hash
        and wall_joint_review.get("acceptedSolidWallCount", 0) > 0
        and wall_joint_review.get("jointCount", 0) > 0
        and not wall_joint_review.get("issues")
    )
    checks["wallJointReviewPass"] = wall_joint_review_ok
    checks["wallJointReview"] = str(wall_joint_review_path.resolve())
    if not wall_joint_review_ok:
        errors.append("solid-wall endpoint/T-junction audit is missing or not PASS")

    structures = scene.get("structures", [])
    ids: list[str] = []
    bad_geometry: list[str] = []
    bad_publish: list[str] = []
    bad_plan_evidence: list[str] = []
    inferred_evidence_errors: list[str] = []
    geometry_owners: dict[tuple[Any, ...], list[str]] = {}
    for index, item in enumerate(structures if isinstance(structures, list) else []):
        item_id = str(item.get("id", f"index-{index}"))
        ids.append(item_id)
        geometry_key = canonical_geometry(item)
        if geometry_key is None:
            bad_geometry.append(item_id)
        else:
            geometry_owners.setdefault(geometry_key, []).append(item_id)
        validation = item.get("validation", {})
        decision = item.get("decision", {})
        if validation.get("publish") is not True or decision.get("status") not in {"accepted", "accepted-inferred"}:
            bad_publish.append(item_id)
        plan_mode = validation.get("planEvidenceMode")
        if decision.get("status") == "accepted-inferred" and plan_mode != "inferred":
            bad_plan_evidence.append(item_id)
        if plan_mode == "inferred":
            evidence = item.get("evidence", {})
            evidence_paths = evidence.get("inferenceEvidencePaths", []) if isinstance(evidence, dict) else []
            resolved_sources = {
                str(resolved).lower()
                for path_value in evidence_paths if isinstance(evidence_paths, list)
                for resolved in [resolve_evidence_path(path_value, args.scene, scene)]
                if resolved is not None and resolved.is_file()
            }
            inference_reason = evidence.get("inferenceReason") if isinstance(evidence, dict) else None
            if (
                validation.get("effectiveGates", {}).get("planAlignment") != "INFERRED"
                or decision.get("status") != "accepted-inferred"
                or not isinstance(inference_reason, str)
                or not inference_reason.strip()
                or len(resolved_sources) < 2
            ):
                bad_plan_evidence.append(item_id)
                inferred_evidence_errors.append(item_id)
        else:
            offset = validation.get("planOffsetDisagreementM")
            maximum = validation.get("maxPlanOffsetM", 0.08)
            if not isinstance(offset, (int, float)) or not math.isfinite(offset) or offset > maximum:
                bad_plan_evidence.append(item_id)
    if not isinstance(structures, list) or not structures:
        errors.append("scene has no accepted structures")
    if len(ids) != len(set(ids)):
        errors.append("structure ids are not unique")
    if bad_geometry:
        errors.append(f"invalid structure geometry: {', '.join(bad_geometry[:8])}")
    if bad_publish:
        errors.append(f"unpublished or undecided structures: {', '.join(bad_publish[:8])}")
    bad_plan_evidence = list(dict.fromkeys(bad_plan_evidence))
    inferred_evidence_errors = list(dict.fromkeys(inferred_evidence_errors))
    if bad_plan_evidence:
        errors.append(f"invalid measured/inferred plan evidence: {', '.join(bad_plan_evidence[:8])}")
    duplicate_geometry = [owners for owners in geometry_owners.values() if len(owners) > 1]
    if duplicate_geometry:
        errors.append("exact duplicate structure geometry: " + "; ".join(", ".join(group) for group in duplicate_geometry[:5]))
    checks["structureCount"] = len(structures) if isinstance(structures, list) else 0
    checks["invalidGeometry"] = bad_geometry
    checks["invalidPublishDecision"] = bad_publish
    checks["invalidPlanEvidence"] = bad_plan_evidence
    checks["invalidInferredEvidence"] = inferred_evidence_errors
    checks["exactDuplicateGeometry"] = duplicate_geometry

    pipeline = scene.get("pipeline", [])
    pipeline_status = {
        item.get("id"): item.get("status")
        for item in pipeline if isinstance(item, dict)
    } if isinstance(pipeline, list) else {}
    pipeline_ok = pipeline_status.get("structures") == "PASS" and pipeline_status.get("author") == "PASS"
    checks["pipelineStructuresAuthorPass"] = pipeline_ok
    if not pipeline_ok:
        errors.append("scene pipeline structures/author are not both PASS")

    quality_loops = scene.get("qualityLoops", [])
    failed_blocking_loops = [
        str(loop.get("name", "unnamed"))
        for loop in quality_loops if isinstance(loop, dict)
        and loop.get("blocking", True) is not False
        and loop.get("status") != "PASS"
    ] if isinstance(quality_loops, list) else ["qualityLoops-missing"]
    required_loop_names = {
        "manual-structure-fail-closed-acceptance",
        "structure-area-and-global-omission-review",
        "room-boundary-topology-closure",
    }
    present_loop_names = {
        loop.get("name") for loop in quality_loops if isinstance(loop, dict)
    } if isinstance(quality_loops, list) else set()
    missing_required_loops = sorted(required_loop_names - present_loop_names)
    checks["failedBlockingQualityLoops"] = failed_blocking_loops
    checks["missingRequiredQualityLoops"] = missing_required_loops
    if failed_blocking_loops:
        errors.append(f"blocking quality loops are not PASS: {', '.join(failed_blocking_loops[:8])}")
    if missing_required_loops:
        errors.append(f"required quality loops are missing: {', '.join(missing_required_loops)}")

    overlap_review = scene.get("overlapReview", {})
    overlap_issue_keys = (
        "exactDuplicates", "duplicateGeometry", "duplicateGeometryPairs",
        "illegalOverlaps", "overlapPairs",
    )
    overlap_ok = (
        isinstance(overlap_review, dict)
        and overlap_review.get("status") == "PASS"
        and all(not overlap_review.get(key) for key in overlap_issue_keys)
    )
    checks["overlapReviewPass"] = overlap_ok
    if not overlap_ok:
        errors.append("scene overlapReview is missing or not PASS")

    object_clearance = scene.get("objectClearanceReview", {})
    object_clearance_ok = (
        isinstance(object_clearance, dict)
        and object_clearance.get("status") == "PASS"
        and object_clearance.get("componentCount", 0) > 0
        and not object_clearance.get("issues")
    )
    checks["objectClearancePass"] = object_clearance_ok
    if not object_clearance_ok:
        errors.append("programmatic furniture or chairs intersect accepted room structures")

    furniture_objects = scene.get("objects", [])
    furniture_pose_errors = []
    for item in furniture_objects if isinstance(furniture_objects, list) else []:
        if item.get("category") == "round-table":
            continue
        validation = item.get("furnitureValidation")
        if not isinstance(validation, dict) or validation.get("status") != "PASS":
            furniture_pose_errors.append(item.get("id", "<unknown>"))
            continue
        delivery_validation = item.get("deliveryValidation")
        if not isinstance(delivery_validation, dict) or delivery_validation.get("status") != "PASS":
            furniture_pose_errors.append(item.get("id", "<unknown>"))
            continue
        if validation.get("independentSource") == "accepted-manual-search-box":
            furniture_pose_errors.append(item.get("id", "<unknown>"))
    checks["independentFurniturePosePass"] = not furniture_pose_errors and bool(furniture_objects)
    checks["failedFurniturePoseIds"] = furniture_pose_errors
    if furniture_pose_errors:
        errors.append("independent furniture pose review is missing or failed: " + ", ".join(furniture_pose_errors[:8]))

    facade_review = scene.get("facadeFaceReview", {})
    facade_reviews = facade_review.get("reviews", []) if isinstance(facade_review, dict) else []
    facade_ok = (
        facade_review.get("status") == "PASS"
        and bool(facade_reviews)
        and all(
            item.get("status") == "PASS"
            and item.get("crossWallCount", 0) >= 2
            and isinstance(item.get("selectedPlane"), dict)
            and item["selectedPlane"].get("offsetSpreadM", 999) <= 0.12
            and item["selectedPlane"].get("maxCollinearityResidualM", 999) <= 0.08
            and not item["selectedPlane"].get("strongerCommonPlaneBeyondSelected", True)
            for item in facade_reviews
        )
    )
    checks["facadeFaceReviewPass"] = facade_ok
    if not facade_ok:
        errors.append("exterior facade semantic face selection is missing, stale, or contradicted")

    derived_review = scene.get("derivedGeometryReview", {})
    derived_items = scene.get("derivedGeometry", [])
    structure_by_id = {item.get("id"): item for item in structures if isinstance(item, dict)}
    derived_recomputed_errors: list[str] = []
    for item in derived_items if isinstance(derived_items, list) else []:
        parent = structure_by_id.get(item.get("parentId"))
        result = recompute_derived_geometry(item, parent) if isinstance(parent, dict) else None
        if result is None or result[0] < 0.99 or result[1] > 1.0:
            derived_recomputed_errors.append(str(item.get("id", "unknown")))
    derived_ok = (
        isinstance(derived_review, dict)
        and derived_review.get("status") == "PASS"
        and isinstance(derived_items, list)
        and bool(derived_items)
        and not derived_review.get("issues")
        and derived_review.get("primitiveCount") == len(derived_items)
        and not derived_recomputed_errors
        and all(
            isinstance(item, dict)
            and item.get("geometryType") == "box"
            and finite_point(item.get("center"))
            and finite_point(item.get("size"))
            and finite_number(item.get("yaw"))
            and item.get("validation", {}).get("axisDeltaDeg", 999) <= 1.0
            and item.get("validation", {}).get("insideParentWithToleranceM", 999) <= 0.001
            for item in derived_items
        )
    )
    checks["derivedGeometryPass"] = derived_ok
    checks["derivedGeometryRecomputedErrors"] = derived_recomputed_errors
    if not derived_ok:
        errors.append("derived geometry is absent, stale, misaligned, or outside its parent footprint")

    topology = scene.get("topologyReview", {})
    declared_spaces = scene.get("declaredTopologySpaces", [])
    topology_spaces = topology.get("spaces", []) if isinstance(topology, dict) else []
    topology_ok = (
        isinstance(topology, dict)
        and topology.get("status") == "PASS"
        and bool(topology_spaces)
        and all(space.get("status") == "PASS" for space in topology_spaces)
    )
    checks["topologyPass"] = topology_ok
    if not topology_ok:
        errors.append("measured room topology is not fully PASS")

    declared_topology = scene.get("declaredTopologyReview", {})
    declared_review_spaces = declared_topology.get("spaces", []) if isinstance(declared_topology, dict) else []
    declared_source_ids = {space.get("id") for space in declared_spaces if isinstance(space, dict)} \
        if isinstance(declared_spaces, list) else set()
    declared_review_ids = {space.get("id") for space in declared_review_spaces if isinstance(space, dict)}
    declared_topology_ok = (
        isinstance(declared_topology, dict)
        and declared_topology.get("status") == "PASS"
        and bool(declared_review_spaces)
        and all(space.get("status") == "PASS" for space in declared_review_spaces)
        and declared_source_ids == declared_review_ids
        and not declared_topology.get("openingErrors")
        and not declared_topology.get("continuationErrors")
        and bool(declared_spaces)
    )
    checks["declaredTopologyPass"] = declared_topology_ok
    if not declared_topology_ok:
        errors.append("declared topology/evidence review is not fully PASS")

    hash_ok = valid_sha256(review.get("sceneSha256")) and review.get("sceneSha256") == scene_hash
    checks["sceneHashMatchesReview"] = hash_ok
    if not hash_ok:
        errors.append("visual review is stale or bound to another scene")

    reviewer = review.get("reviewer")
    reviewed_at = parse_review_time(review.get("reviewedAt"))
    reviewer_ok = isinstance(reviewer, str) and len(reviewer.strip()) >= 3
    reviewed_at_ok = reviewed_at is not None and reviewed_at <= datetime.now(timezone.utc)
    checks["reviewerValid"] = reviewer_ok
    checks["reviewedAtValid"] = reviewed_at_ok
    if not reviewer_ok:
        errors.append("visual review has no valid reviewer identifier")
    if not reviewed_at_ok:
        errors.append("visual review reviewedAt is missing, invalid, timezone-naive, or in the future")

    p0 = review.get("p0", [])
    p1 = review.get("p1", [])
    if not isinstance(p0, list) or p0:
        errors.append("P0 visual findings remain")
    if not isinstance(p1, list) or p1:
        errors.append("P1 visual findings remain")

    areas = review.get("areas", [])
    required_area_ids = review.get("requiredAreaIds", [])
    present_area_ids = [area.get("id") for area in areas if isinstance(area, dict)] if isinstance(areas, list) else []
    area_review = scene.get("areaReview", {})
    scene_area_ids = area_review.get("regionIds", []) if isinstance(area_review, dict) else []
    exact_area_review = (
        isinstance(required_area_ids, list)
        and bool(required_area_ids)
        and len(set(required_area_ids)) == len(required_area_ids)
        and len(present_area_ids) == len(required_area_ids)
        and len(set(present_area_ids)) == len(present_area_ids)
        and len(scene_area_ids) == len(required_area_ids)
        and len(set(scene_area_ids)) == len(scene_area_ids)
        and set(required_area_ids) == set(present_area_ids)
        and set(required_area_ids) == set(scene_area_ids)
        and area_review.get("status") == "PASS"
    )
    checks["exactDeclaredAreaReviewPass"] = exact_area_review
    if not exact_area_review:
        errors.append("reviewed areas do not exactly match requiredAreaIds")
    evidence_errors: list[str] = []
    for area in areas if isinstance(areas, list) else []:
        evidence_items = area.get("evidence", []) if isinstance(area, dict) else []
        if not evidence_items:
            evidence_errors.append(str(area.get("id", "unknown")))
            continue
        for evidence in evidence_items:
            if not isinstance(evidence, dict) or not evidence.get("path") or not valid_sha256(evidence.get("sha256")):
                evidence_errors.append(str(area.get("id", "unknown")))
                break
            evidence_path = (args.visual_review.parent / evidence["path"]).resolve()
            if not evidence_path.is_file() or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence["sha256"]:
                evidence_errors.append(str(area.get("id", "unknown")))
                break
    if evidence_errors:
        errors.append(f"missing or stale area evidence: {', '.join(sorted(set(evidence_errors)))}")
    checks["areaEvidenceErrors"] = sorted(set(evidence_errors))
    area_scores = [area.get("score") for area in areas if isinstance(area, dict)] if isinstance(areas, list) else []
    valid_area_scores = bool(area_scores) and all(
        isinstance(score, int) and 0 <= score <= 100 for score in area_scores
    )
    if not valid_area_scores:
        errors.append("visual review has no valid area scores")
        visual_score = 0.0
        minimum_area = 0
    else:
        visual_score = round(sum(area_scores) / len(area_scores) * 0.45, 2)
        minimum_area = min(area_scores)
        if minimum_area < 85:
            errors.append("at least one reviewed area scores below 85")

    structural_score = 0
    structural_score += 15 if checks["candidateCount"] == 0 else 0
    structural_score += 10 if not bad_geometry and not duplicate_geometry and len(ids) == len(set(ids)) and bool(ids) else 0
    structural_score += 10 if topology_ok and declared_topology_ok and overlap_ok and wall_joint_review_ok else 0
    structural_score += 10 if not bad_publish and pipeline_ok and not failed_blocking_loops and not missing_required_loops else 0
    structural_score += 10 if not bad_plan_evidence and exact_area_review and reviewer_ok and reviewed_at_ok else 0
    total = round(structural_score + visual_score, 2)
    if total < 90:
        errors.append("total score is below 90")

    report = {
        "schemaVersion": 1,
        "status": "PASS" if not errors else "FAIL",
        "scene": str(args.scene.resolve()),
        "sceneSha256": scene_hash,
        "review": str(args.visual_review.resolve()),
        "score": {
            "structural": structural_score,
            "visual": visual_score,
            "total": total,
            "minimumArea": minimum_area,
        },
        "checks": checks,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
