#!/usr/bin/env python3
"""Rank point-cloud wall proposals that are not explained by the current scene.

The audit is deliberately geometry-first.  A plan-line proposal is not treated as
a wall unless indexed points persist through low, middle and high elevation bands.
This filters desks, booth backs and ceiling-only returns before Agent review.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from capture_index import CaptureIndex
from render_scene_evidence_overlay import _proposal_explanation, _scene_wall_segments


def _eligible(candidate: dict) -> bool:
    length = float(candidate.get("lengthM", 0.0))
    confidence = float(candidate.get("confidence", 0.0))
    residual = float(candidate.get("fitResidualP90M", 1.0))
    support = int(candidate.get("supportPointCount", 0))
    paired = candidate.get("wallMode") == "paired-faces"
    return residual <= 0.08 and support >= 1000 and (
        (paired and confidence >= 0.74 and length >= 1.0)
        or (not paired and confidence >= 0.64 and length >= 2.0)
    )


def _longest_run(mask: np.ndarray, bin_size: float) -> float:
    best = current = 0
    for value in mask:
        current = current + 1 if value else 0
        best = max(best, current)
    return best * bin_size


def _runs(mask: np.ndarray, bin_size: float) -> list[list[float]]:
    result: list[list[float]] = []
    start: int | None = None
    for index, value in enumerate(np.append(mask, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            result.append([round(start * bin_size, 3), round(index * bin_size, 3)])
            start = None
    return result


def _classify_height_support(low: np.ndarray, middle: np.ndarray, high: np.ndarray,
                             bin_size: float) -> dict:
    occupied = (low + middle + high) >= 3
    full = (low >= 2) & (middle >= 2) & (high >= 2)
    mid_high = (middle >= 2) & (high >= 2)
    denominator = max(1, int(np.count_nonzero(occupied)))
    full_ratio = float(np.count_nonzero(full)) / denominator
    mid_high_ratio = float(np.count_nonzero(mid_high)) / denominator
    longest = _longest_run(full, bin_size)
    if full_ratio >= 0.55 and longest >= 0.90:
        disposition = "LOCAL_ELEVATION_REVIEW"
    elif full_ratio >= 0.25 and longest >= 0.60:
        disposition = "PARTIAL_SEGMENT_REVIEW"
    else:
        disposition = "WITHHOLD_NON_FULL_HEIGHT"
    return {
        "occupiedAlongBinCount": int(np.count_nonzero(occupied)),
        "fullHeightCoverageRatio": round(full_ratio, 4),
        "middleHighCoverageRatio": round(mid_high_ratio, 4),
        "longestFullHeightRunM": round(longest, 3),
        "fullHeightRunsM": _runs(full, bin_size),
        "disposition": disposition,
    }


def _height_support(index: CaptureIndex, candidate: dict, floor: dict,
                    corridor_width: float = 0.32, bin_size: float = 0.12) -> dict:
    line = candidate["suggestedCenterline"]
    start = np.asarray(line["start"], dtype=np.float64)
    end = np.asarray(line["end"], dtype=np.float64)
    delta = end - start
    length = float(np.linalg.norm(delta))
    direction = delta / length
    normal = np.asarray([-direction[1], direction[0]])
    half = corridor_width / 2.0
    corners = [start + normal * half, start - normal * half, end + normal * half, end - normal * half]
    bounds = np.asarray(corners)
    bins = max(1, int(math.ceil(length / bin_size)))
    low = np.zeros(bins, dtype=np.int32)
    middle = np.zeros(bins, dtype=np.int32)
    high = np.zeros(bins, dtype=np.int32)
    returned = read = tiles = 0
    for points in index.iter_bbox(
        float(bounds[:, 0].min()), float(bounds[:, 1].min()),
        float(bounds[:, 0].max()), float(bounds[:, 1].max()),
    ):
        tiles += int(points.stats.get("tilesRead", 0))
        read += int(points.stats.get("pointsRead", 0))
        relative = np.column_stack((points.x - start[0], points.y - start[1]))
        along = relative @ direction
        lateral = np.abs(relative @ normal)
        height = points.z - (
            float(floor["a"]) * points.x + float(floor["b"]) * points.y + float(floor["c"])
        )
        keep = (along >= 0.0) & (along <= length) & (lateral <= half) & (height >= 0.18) & (height <= 2.80)
        if not keep.any():
            continue
        returned += int(np.count_nonzero(keep))
        columns = np.minimum((along[keep] / bin_size).astype(np.int32), bins - 1)
        selected_height = height[keep]
        np.add.at(low, columns[(selected_height >= 0.20) & (selected_height < 0.80)], 1)
        np.add.at(middle, columns[(selected_height >= 0.80) & (selected_height < 1.80)], 1)
        np.add.at(high, columns[(selected_height >= 1.80) & (selected_height <= 2.80)], 1)
    result = _classify_height_support(low, middle, high, bin_size)
    result["queryStats"] = {"tilesRead": tiles, "pointsRead": read, "pointsReturned": returned}
    return result


def audit(index_path: Path, proposals_path: Path, scene_path: Path, survey_path: Path,
          author_picks_path: Path | None, output_path: Path) -> dict:
    index = CaptureIndex.open(index_path)
    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    survey = json.loads(survey_path.read_text(encoding="utf-8"))
    dispositions: dict[str, str] = {}
    if author_picks_path:
        picks = json.loads(author_picks_path.read_text(encoding="utf-8"))
        dispositions = {str(item["id"]): str(item.get("disposition", "WITHHOLD"))
                        for item in picks.get("withheld", [])}
    segments, source_ids = _scene_wall_segments(scene)
    results = []
    for candidate in proposals.get("wallCandidates", []):
        if not _eligible(candidate):
            continue
        explanation = _proposal_explanation(candidate, segments, source_ids)
        candidate_id = str(candidate["id"])
        if explanation["explained"]:
            continue
        record = {
            "id": candidate_id,
            "lengthM": round(float(candidate["lengthM"]), 4),
            "confidence": round(float(candidate["confidence"]), 4),
            "fitResidualP90M": round(float(candidate["fitResidualP90M"]), 5),
            "supportPointCount": int(candidate["supportPointCount"]),
            "sceneCoverageRatio": explanation["coverageRatio"],
            "priorDisposition": dispositions.get(candidate_id),
            **_height_support(index, candidate, survey["floor"]),
        }
        if record["priorDisposition"]:
            record["disposition"] = record["priorDisposition"]
        results.append(record)
    results.sort(key=lambda item: (
        item["disposition"] not in {"LOCAL_ELEVATION_REVIEW", "PARTIAL_SEGMENT_REVIEW"},
        -item["fullHeightCoverageRatio"], -item["longestFullHeightRunM"], -item["confidence"],
    ))
    payload = {
        "schemaVersion": 1,
        "kind": "structural-omission-audit",
        "indexFingerprint": index.manifest["indexFingerprint"],
        "candidateCount": len(results),
        "localElevationReviewCount": sum(item["disposition"] == "LOCAL_ELEVATION_REVIEW" for item in results),
        "partialSegmentReviewCount": sum(item["disposition"] == "PARTIAL_SEGMENT_REVIEW" for item in results),
        "candidates": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--survey", required=True, type=Path)
    parser.add_argument("--author-picks", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.index, args.proposals, args.scene, args.survey, args.author_picks, args.output)
    print(json.dumps({key: result[key] for key in (
        "kind", "indexFingerprint", "candidateCount", "localElevationReviewCount", "partialSegmentReviewCount"
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
