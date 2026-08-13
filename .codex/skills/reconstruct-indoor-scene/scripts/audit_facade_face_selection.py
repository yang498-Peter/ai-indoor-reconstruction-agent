#!/usr/bin/env python3
"""Compare a measured wall end with an explicitly selected semantic facade plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import laspy
import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--las", required=True, type=Path)
    parser.add_argument("--picks", required=True, type=Path)
    parser.add_argument("--floor-z", required=True, type=float)
    parser.add_argument("--side", choices=("start", "end"), default="end")
    parser.add_argument("--max-extension", type=float, default=0.80)
    parser.add_argument("--bin-size", type=float, default=0.025)
    parser.add_argument("--half-band", type=float, default=0.10)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plot", type=Path)
    args = parser.parse_args()

    package = json.loads(args.picks.read_text(encoding="utf-8"))
    rows = []
    for item in package.get("elements", []):
        start = np.asarray(item["start"], dtype=np.float64)
        end = np.asarray(item["end"], dtype=np.float64)
        declared_facade = item.get("facadeEnd") if args.side == "end" else item.get("facadeStart")
        anchor = end if args.side == "end" else start
        inward = start if args.side == "end" else end
        direction = anchor - inward
        direction /= np.linalg.norm(direction)
        rows.append({
            "id": item["id"], "anchor": anchor, "direction": direction,
            "facade": np.asarray(declared_facade, dtype=np.float64) if declared_facade else None,
            "extensionEvidence": item.get("extensionEvidence"),
            "counts": np.zeros(int(np.ceil((args.max_extension + 0.25) / args.bin_size)), dtype=np.int64),
        })

    with laspy.open(args.las) as reader:
        for chunk in reader.chunk_iterator(800_000):
            xy = np.column_stack((np.asarray(chunk.x), np.asarray(chunk.y))).astype(np.float64)
            z = np.asarray(chunk.z, dtype=np.float64)
            height_mask = (z >= args.floor_z + 0.45) & (z <= args.floor_z + 2.85)
            xy = xy[height_mask]
            for row in rows:
                relative = xy - row["anchor"]
                along = relative @ row["direction"]
                perpendicular = np.abs(relative[:, 0] * row["direction"][1] - relative[:, 1] * row["direction"][0])
                mask = (along >= -0.25) & (along <= args.max_extension) & (perpendicular <= args.half_band)
                if not np.any(mask):
                    continue
                indices = np.floor((along[mask] + 0.25) / args.bin_size).astype(np.int32)
                indices = np.clip(indices, 0, len(row["counts"]) - 1)
                row["counts"] += np.bincount(indices, minlength=len(row["counts"]))[:len(row["counts"])]

    results = []
    detected = []
    selected_offsets = []
    selected_points = []
    selected_support_flags = []
    beyond_offsets = []
    for row in rows:
        counts = row["counts"]
        smooth = np.convolve(counts.astype(float), np.ones(5), mode="same")
        baseline = smooth[:max(3, int(0.25 / args.bin_size))]
        threshold = max(25.0, float(np.percentile(baseline, 65)) * 0.22)
        metres = np.arange(len(smooth)) * args.bin_size - 0.25
        beyond = np.flatnonzero((metres >= 0.08) & (smooth >= threshold))
        extension = 0.0 if beyond.size == 0 else float(metres[beyond[-1]])
        detected.append(extension)
        selected_offset = None
        selected_supported = None
        competing_offset = None
        if row["facade"] is not None:
            selected_offset = float(np.linalg.norm(row["facade"] - row["anchor"]))
            selected_offsets.append(selected_offset)
            selected_points.append(row["facade"])
            selected_band = (metres >= max(0.0, selected_offset - 0.10)) & (metres <= selected_offset + 0.10)
            selected_supported = bool(np.max(smooth[selected_band], initial=0) >= threshold)
            selected_support_flags.append(selected_supported or row["extensionEvidence"] == "inferred-glazing-junction")
            selected_peak = float(np.max(smooth[selected_band], initial=0))
            competing = np.flatnonzero(
                (metres >= selected_offset + 0.20)
                & (smooth >= max(threshold, selected_peak * 0.75))
            )
            if competing.size:
                competing_offset = float(metres[competing[np.argmax(smooth[competing])]])
                beyond_offsets.append(competing_offset)
        results.append({
            "id": row["id"], "pickedAnchor": row["anchor"].round(4).tolist(),
            "detectedStructuralExtensionM": round(extension, 4),
            "selectedFacadeOffsetM": None if selected_offset is None else round(selected_offset, 4),
            "selectedFacadeSupported": selected_supported,
            "extensionEvidence": row["extensionEvidence"],
            "strongestCompetingOffsetM": None if competing_offset is None else round(competing_offset, 4),
            "threshold": round(threshold, 2),
            "binCounts": counts.tolist(),
        })
    meaningful = [value for value in detected if value >= 0.08]
    median_extension = float(np.median(meaningful)) if meaningful else 0.0
    supporting_ratio = len(meaningful) / len(rows) if rows else 0.0
    selected_plane = None
    selected_plane_ok = False
    if len(selected_points) == len(rows) and len(selected_points) >= 2:
        points = np.asarray(selected_points)
        centered = points - points.mean(axis=0)
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        normal = np.asarray([-vh[0, 1], vh[0, 0]])
        residuals = np.abs(centered @ normal)
        offset_spread = max(selected_offsets) - min(selected_offsets)
        competing_common = (
            len(beyond_offsets) / len(rows) >= 0.5
            and max(beyond_offsets) - min(beyond_offsets) <= 0.12
        )
        selected_plane_ok = (
            offset_spread <= 0.12
            and float(residuals.max(initial=0)) <= 0.08
            and all(selected_support_flags)
            and not competing_common
        )
        selected_plane = {
            "offsetRangeM": [round(min(selected_offsets), 4), round(max(selected_offsets), 4)],
            "offsetSpreadM": round(offset_spread, 4),
            "maxCollinearityResidualM": round(float(residuals.max(initial=0)), 4),
            "supportedOrExplicitlyInferredCount": sum(selected_support_flags),
            "strongerCommonPlaneBeyondSelected": competing_common,
        }
    if selected_plane is not None:
        status = "PASS" if selected_plane_ok else "REVIEW"
    else:
        status = "REVIEW" if supporting_ratio >= 0.5 and median_extension >= 0.12 else "PASS"
    report = {
        "schemaVersion": "1.0", "status": status,
        "rule": "an envelope face cannot publish while a majority of intersecting structural walls continue materially beyond it",
        "side": args.side, "crossWallCount": len(rows),
        "supportingWallRatio": round(supporting_ratio, 4),
        "medianStructuralExtensionM": round(median_extension, 4),
        "selectedPlane": selected_plane,
        "requiredAction": "enumerate inner/centre/outer candidates or correct unsupported junction grades" if status == "REVIEW" else "selected outer facade is a coherent repeated plane and no stronger common plane remains beyond it",
        "items": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.plot:
        height = 70 + 44 * len(rows)
        image = np.full((height, 920, 3), 18, dtype=np.uint8)
        for index, row in enumerate(results):
            y = 55 + index * 44
            counts = np.asarray(row["binCounts"], dtype=float)
            if counts.max(initial=0) > 0:
                counts /= counts.max()
            cv2.putText(image, row["id"], (12, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1, cv2.LINE_AA)
            x0 = 260
            cv2.line(image, (x0 + int(0.25 / args.bin_size) * 12, y - 18), (x0 + int(0.25 / args.bin_size) * 12, y + 12), (70, 90, 255), 1)
            for bin_index, value in enumerate(counts):
                x = x0 + bin_index * 12
                cv2.line(image, (x, y + 12), (x, y + 12 - int(value * 26)), (80, 210, 255), 7)
        cv2.putText(image, f"status={status} median extension={median_extension:.3f}m", (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (80, 220, 255) if status == "PASS" else (80, 100, 255), 2, cv2.LINE_AA)
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        encoded, buffer = cv2.imencode(args.plot.suffix or ".png", image)
        if not encoded:
            raise SystemExit("failed to encode plot")
        buffer.tofile(str(args.plot))
    print(json.dumps({"status": status, "medianExtensionM": round(median_extension, 4), "supportingWallRatio": round(supporting_ratio, 4)}))
    return 2 if status == "REVIEW" else 0


if __name__ == "__main__":
    raise SystemExit(main())
