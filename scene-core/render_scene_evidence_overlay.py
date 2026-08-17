#!/usr/bin/env python3
"""Render Semantic Scene V2 plan geometry over indexed point-cloud evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _status(scene: dict, node_id: str) -> str:
    return str((scene.get("evidence", {}).get(node_id) or {}).get("status", "candidate"))


def _pixel(point: list[float], grid: dict) -> tuple[int, int]:
    return (
        int(round((float(point[0]) - float(grid["originX"])) / float(grid["cellSizeM"]))),
        int(round((float(grid["originY"]) - float(point[1])) / float(grid["cellSizeM"]))),
    )


def _wall_intervals(scene: dict, wall: dict) -> list[tuple[float, float]]:
    start, end = np.asarray(wall["start"], dtype=np.float64), np.asarray(wall["end"], dtype=np.float64)
    length = float(np.linalg.norm(end - start))
    openings = []
    for child_id in wall.get("children", []):
        child = scene["nodes"].get(child_id)
        if child and child.get("type") in {"opening", "door", "window"}:
            center = float(child.get("hostOffsetM", 0.0))
            half = float(child.get("width", 0.0)) / 2.0
            openings.append((max(0.0, center - half), min(length, center + half)))
    openings.sort()
    intervals, cursor = [], 0.0
    for left, right in openings:
        if left > cursor:
            intervals.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < length:
        intervals.append((cursor, length))
    return intervals


def _angle_delta(a: np.ndarray, b: np.ndarray) -> float:
    angle_a = math.atan2(float(a[1]), float(a[0])) % math.pi
    angle_b = math.atan2(float(b[1]), float(b[0])) % math.pi
    delta = abs(angle_a - angle_b)
    return min(delta, math.pi - delta)


def _distance_to_segment(point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    delta = end - start
    length2 = float(np.dot(delta, delta))
    if length2 <= 1e-12:
        return float(np.linalg.norm(point - start))
    along = float(np.clip(np.dot(point - start, delta) / length2, 0.0, 1.0))
    return float(np.linalg.norm(point - (start + along * delta)))


def _scene_wall_segments(scene: dict) -> tuple[list[tuple[np.ndarray, np.ndarray]], set[str]]:
    segments: list[tuple[np.ndarray, np.ndarray]] = []
    source_ids: set[str] = set()
    for node in scene["nodes"].values():
        if node.get("type") != "wall" or _status(scene, node["id"]) == "rejected":
            continue
        start = np.asarray(node["start"], dtype=np.float64)
        end = np.asarray(node["end"], dtype=np.float64)
        delta = end - start
        length = float(np.linalg.norm(delta))
        if length <= 1e-6:
            continue
        direction = delta / length
        for left, right in _wall_intervals(scene, node):
            segments.append((start + direction * left, start + direction * right))
        source_ids.update(str(value) for value in node.get("meta", {}).get("sourceProposalIds", []))
    return segments, source_ids


def _proposal_explanation(candidate: dict, segments: list[tuple[np.ndarray, np.ndarray]],
                          source_ids: set[str]) -> dict:
    line = candidate["suggestedCenterline"]
    start = np.asarray(line["start"], dtype=np.float64)
    end = np.asarray(line["end"], dtype=np.float64)
    direction = end - start
    if float(np.linalg.norm(direction)) <= 1e-6:
        return {"coverageRatio": 0.0, "sourceBound": False, "explained": False}
    aligned = [segment for segment in segments
               if _angle_delta(direction, segment[1] - segment[0]) <= math.radians(8.0)]
    samples = np.linspace(start, end, 31)
    supported = sum(
        1 for point in samples
        if any(_distance_to_segment(point, segment[0], segment[1]) <= 0.24 for segment in aligned)
    )
    ratio = supported / len(samples)
    source_bound = str(candidate.get("id")) in source_ids
    return {
        "coverageRatio": round(ratio, 4),
        "sourceBound": source_bound,
        "explained": source_bound or ratio >= 0.60,
    }


def render(
    scene_path: Path,
    manifest_path: Path,
    image_path: Path,
    output_path: Path,
    proposals_path: Path | None = None,
    proposal_limit: int = 60,
    unexplained_only: bool = False,
) -> dict:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    grid = manifest["grid"]
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read evidence image: {image_path}")
    overlay = image.copy()
    extents: list[tuple[int, int]] = []

    for node in scene["nodes"].values():
        if node.get("type") != "wall" or _status(scene, node["id"]) == "rejected":
            continue
        start, end = np.asarray(node["start"], dtype=np.float64), np.asarray(node["end"], dtype=np.float64)
        delta = end - start
        length = float(np.linalg.norm(delta))
        if length <= 1e-6:
            continue
        direction = delta / length
        status = _status(scene, node["id"])
        color = (210, 185, 90) if node.get("wallKind") == "glass" else (218, 234, 245)
        if status == "candidate":
            color = (70, 160, 255)
        width_px = max(2, int(round(float(node.get("thickness", 0.12)) / float(grid["cellSizeM"]))))
        for left, right in _wall_intervals(scene, node):
            p0 = _pixel((start + direction * left).tolist(), grid)
            p1 = _pixel((start + direction * right).tolist(), grid)
            cv2.line(overlay, p0, p1, color, width_px + 4, cv2.LINE_AA)
            cv2.line(overlay, p0, p1, (18, 24, 28), max(1, width_px), cv2.LINE_AA)
            extents.extend((p0, p1))

    for node in scene["nodes"].values():
        if node.get("type") != "item" or _status(scene, node["id"]) == "rejected":
            continue
        center = np.asarray(node["center"], dtype=np.float64)
        long_size, _, short_size = [float(value) for value in node["size"]]
        yaw = float(node.get("yaw", 0.0))
        forward = np.asarray([math.cos(yaw), math.sin(yaw)])
        side = np.asarray([-forward[1], forward[0]])
        corners = [
            center + sx * forward * long_size / 2.0 + sy * side * short_size / 2.0
            for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))
        ]
        polygon = np.asarray([_pixel(corner.tolist(), grid) for corner in corners], dtype=np.int32)
        cv2.polylines(overlay, [polygon], True, (110, 205, 220), 2, cv2.LINE_AA)
        extents.extend(tuple(point) for point in polygon.tolist())

    rendered_proposals = 0
    omission_audit: list[dict] = []
    if proposals_path is not None:
        proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
        wall_segments, source_ids = _scene_wall_segments(scene)
        ranked = []
        for candidate in proposals.get("wallCandidates", []):
            length = float(candidate.get("lengthM", 0.0))
            confidence = float(candidate.get("confidence", 0.0))
            residual = float(candidate.get("fitResidualP90M", 1.0))
            support = int(candidate.get("supportPointCount", 0))
            paired = candidate.get("wallMode") == "paired-faces"
            eligible = residual <= 0.08 and support >= 1000 and (
                (paired and confidence >= 0.74 and length >= 1.0)
                or (not paired and confidence >= 0.64 and length >= 2.0)
            )
            if eligible:
                explanation = _proposal_explanation(candidate, wall_segments, source_ids)
                audit = {
                    "id": str(candidate["id"]),
                    "lengthM": round(length, 4),
                    "confidence": round(confidence, 4),
                    "fitResidualP90M": round(residual, 5),
                    "supportPointCount": support,
                    **explanation,
                }
                omission_audit.append(audit)
                if not unexplained_only or not explanation["explained"]:
                    ranked.append((confidence + min(length, 6.0) * 0.025 + min(support, 20000) / 200000.0,
                                   candidate, explanation))
        for _, candidate, explanation in sorted(ranked, key=lambda row: row[0], reverse=True)[:proposal_limit]:
            line = candidate["suggestedCenterline"]
            p0, p1 = _pixel(line["start"], grid), _pixel(line["end"], grid)
            color = (35, 65, 245) if not explanation["explained"] else (205, 85, 235)
            cv2.line(overlay, p0, p1, color, 2, cv2.LINE_AA)
            midpoint = ((p0[0] + p1[0]) // 2, (p0[1] + p1[1]) // 2)
            cv2.putText(overlay, str(candidate["id"]).rsplit("-", 1)[-1], midpoint,
                        cv2.FONT_HERSHEY_SIMPLEX, 0.34, (240, 185, 255), 1, cv2.LINE_AA)
            extents.extend((p0, p1))
            rendered_proposals += 1

    blended = cv2.addWeighted(image, 0.78, overlay, 0.88, 0.0)
    if extents:
        xs, ys = zip(*extents)
        margin = 70
        left, right = max(0, min(xs) - margin), min(blended.shape[1], max(xs) + margin)
        top, bottom = max(0, min(ys) - margin), min(blended.shape[0], max(ys) + margin)
        blended = blended[top:bottom, left:right]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), blended):
        raise ValueError(f"cannot write overlay: {output_path}")
    result = {
        "scene": str(scene_path.resolve()),
        "sceneSha256": _sha256(scene_path),
        "evidence": str(image_path.resolve()),
        "evidenceSha256": _sha256(image_path),
        "output": str(output_path.resolve()),
        "outputSha256": _sha256(output_path),
        "renderedWalls": sum(1 for node in scene["nodes"].values() if node.get("type") == "wall" and _status(scene, node["id"]) != "rejected"),
        "renderedItems": sum(1 for node in scene["nodes"].values() if node.get("type") == "item" and _status(scene, node["id"]) != "rejected"),
        "renderedProposals": rendered_proposals,
        "eligibleProposalCount": len(omission_audit),
        "unexplainedProposalCount": sum(1 for item in omission_audit if not item["explained"]),
        "unexplainedProposals": sorted(
            (item for item in omission_audit if not item["explained"]),
            key=lambda item: (-item["confidence"], -item["supportPointCount"]),
        ),
    }
    output_path.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--proposal-limit", type=int, default=60)
    parser.add_argument("--unexplained-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(render(
        args.scene, args.manifest, args.image, args.output, args.proposals, args.proposal_limit,
        args.unexplained_only,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
