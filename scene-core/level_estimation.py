#!/usr/bin/env python3
"""Estimate indoor floor and ceiling peaks directly from one LAS/LAZ."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import laspy
import numpy as np


CHUNK_POINTS = 1_000_000


def _peaks(values: np.ndarray, first: int, last: int) -> list[int]:
    return [
        index
        for index in range(max(1, first), min(len(values) - 1, last))
        if values[index] >= values[index - 1] and values[index] >= values[index + 1]
    ]


def estimate_levels(
    source: Path,
    *,
    bin_size_m: float = 0.01,
    max_points: int = 2_000_000,
    min_height_m: float = 2.0,
    max_height_m: float = 4.5,
) -> dict[str, object]:
    source = source.resolve()
    if not source.is_file() or source.suffix.casefold() not in {".las", ".laz"}:
        raise ValueError("level estimation requires one existing LAS or LAZ")
    if bin_size_m <= 0 or max_points < 10_000 or min_height_m <= 0 or max_height_m <= min_height_m:
        raise ValueError("level-estimation parameters are invalid")

    with laspy.open(source) as reader:
        point_count = int(reader.header.point_count)
        min_z = float(reader.header.mins[2])
        max_z = float(reader.header.maxs[2])
        if not math.isfinite(min_z) or not math.isfinite(max_z) or max_z - min_z < min_height_m:
            raise ValueError("LAS Z bounds cannot contain a valid indoor level")
        stride = max(1, int(math.ceil(point_count / max_points)))
        counts = np.zeros(max(2, int(math.ceil((max_z - min_z) / bin_size_m)) + 1), dtype=np.int64)
        sampled = 0
        for chunk in reader.chunk_iterator(CHUNK_POINTS):
            z = np.asarray(chunk.z, dtype=np.float64)[::stride]
            indices = np.clip(((z - min_z) / bin_size_m).astype(np.int64), 0, len(counts) - 1)
            np.add.at(counts, indices, 1)
            sampled += len(indices)

    kernel = np.asarray([1, 2, 3, 2, 1], dtype=np.float64)
    smooth = np.convolve(counts.astype(np.float64), kernel / kernel.sum(), mode="same")
    span = max_z - min_z
    floor_peaks = _peaks(smooth, int(0.01 * span / bin_size_m), int(min(0.48 * span, max_height_m * 0.5) / bin_size_m))
    if not floor_peaks:
        raise ValueError("no stable lower horizontal peak was found")
    floor_index = max(floor_peaks, key=lambda index: smooth[index])
    floor_z = min_z + (floor_index + 0.5) * bin_size_m
    ceiling_peaks = _peaks(
        smooth,
        int((floor_z + min_height_m - min_z) / bin_size_m),
        int((min(floor_z + max_height_m, max_z) - min_z) / bin_size_m),
    )
    if not ceiling_peaks:
        raise ValueError("no stable ceiling-height peak was found")

    def ceiling_score(index: int) -> float:
        height = min_z + (index + 0.5) * bin_size_m - floor_z
        return float(smooth[index]) * (1.15 if 2.4 <= height <= 3.4 else 1.0)

    ceiling_index = max(ceiling_peaks, key=ceiling_score)
    ceiling_z = min_z + (ceiling_index + 0.5) * bin_size_m
    if not min_height_m <= ceiling_z - floor_z <= max_height_m:
        raise ValueError("selected floor/ceiling separation is outside the configured indoor range")

    def alternatives(indices: list[int], selected: int) -> list[dict[str, object]]:
        return [
            {
                "z": round(min_z + (index + 0.5) * bin_size_m, 5),
                "smoothedCount": round(float(smooth[index]), 3),
                "selected": index == selected,
            }
            for index in sorted(indices, key=lambda item: smooth[item], reverse=True)[:8]
        ]

    return {
        "schemaVersion": 1,
        "kind": "raw-las-level-estimation",
        "status": "REVIEW_REQUIRED",
        "source": str(source),
        "inputPointCount": point_count,
        "sampledPointCount": sampled,
        "samplingStride": stride,
        "zBounds": [min_z, max_z],
        "binSizeM": bin_size_m,
        "floorZ": round(floor_z, 5),
        "ceilingZ": round(ceiling_z, 5),
        "levelHeightM": round(ceiling_z - floor_z, 5),
        "uncertaintyM": round(bin_size_m * 2.0, 5),
        "floorAlternatives": alternatives(floor_peaks, floor_index),
        "ceilingAlternatives": alternatives(ceiling_peaks, ceiling_index),
        "authorityRule": "raw histogram proposal; an Agent must inspect unannotated evidence before acceptance",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bin-size", type=float, default=0.01)
    parser.add_argument("--max-points", type=int, default=2_000_000)
    args = parser.parse_args()
    result = estimate_levels(args.las, bin_size_m=args.bin_size, max_points=args.max_points)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
