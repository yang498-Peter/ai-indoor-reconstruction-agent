#!/usr/bin/env python3
"""Render browser-free metric point-pick overlays and normalized structure JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pixel(point: list[float], metadata: dict) -> tuple[int, int]:
    resolution = float(metadata["resolutionMPerPixel"])
    return (
        round((float(point[0]) - float(metadata["minX"])) / resolution),
        round((float(metadata["maxY"]) - float(point[1])) / resolution),
    )


def validate(document: dict, image_path: Path, metadata: dict) -> None:
    expected = document.get("imageSha256")
    actual = sha256(image_path)
    if expected != actual:
        raise ValueError(f"image hash mismatch: expected {expected}, got {actual}")
    bounds = (float(metadata["minX"]), float(metadata["maxX"]), float(metadata["minY"]), float(metadata["maxY"]))
    ids: set[str] = set()
    for element in document.get("elements", []):
        element_id = str(element.get("id", "")).strip()
        if not element_id or element_id in ids:
            raise ValueError(f"missing or duplicate id: {element_id!r}")
        ids.add(element_id)
        start, end = element.get("start"), element.get("end")
        if not all(isinstance(point, list) and len(point) == 2 and all(np.isfinite(value) for value in point) for point in (start, end)):
            raise ValueError(f"{element_id}: invalid start/end")
        extra_points = [element.get("facadeEnd"), element.get("facadeStart")]
        for x, y in [start, end, *(point for point in extra_points if point is not None)]:
            if not (bounds[0] <= x <= bounds[1] and bounds[2] <= y <= bounds[3]):
                raise ValueError(f"{element_id}: point outside evidence image")
        if np.linalg.norm(np.asarray(end) - np.asarray(start)) < 0.05:
            raise ValueError(f"{element_id}: segment shorter than 0.05 m")
        if element.get("status") not in {"accepted-measured", "accepted-inferred", "review"}:
            raise ValueError(f"{element_id}: invalid status")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--picks", required=True, type=Path)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--structures", required=True, type=Path)
    args = parser.parse_args()

    metadata = load_json(args.metadata)
    document = load_json(args.picks)
    validate(document, args.image, metadata)
    image = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode {args.image}")

    colors = {"wall": (86, 216, 255), "glass": (235, 220, 92), "door": (102, 210, 255), "boundary": (122, 255, 187)}
    normalized = []
    facade_points = []
    for element in document["elements"]:
        start = pixel(element["start"], metadata)
        end = pixel(element["end"], metadata)
        color = colors.get(element.get("category"), (104, 220, 255))
        cv2.line(image, start, end, color, 3, cv2.LINE_AA)
        cv2.circle(image, start, 5, color, -1, cv2.LINE_AA)
        cv2.circle(image, end, 5, color, -1, cv2.LINE_AA)
        label = str(element["id"])
        cv2.putText(image, label, ((start[0] + end[0]) // 2 + 5, (start[1] + end[1]) // 2 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)
        facade_end_source = element.get("facadeEnd")
        if facade_end_source:
            facade_end = pixel(facade_end_source, metadata)
            cv2.line(image, end, facade_end, (255, 210, 70), 3, cv2.LINE_AA)
            cv2.circle(image, facade_end, 5, (255, 210, 70), -1, cv2.LINE_AA)
            facade_points.append((facade_end_source, facade_end))
        normalized.append({
            "id": element["id"],
            "category": element["category"],
            "geometry": {"type": "segment", "sourceXY": [element["start"], element["end"]]},
            "status": element["status"],
            "uncertaintyM": element.get("uncertaintyM"),
            "evidence": [str(args.image.resolve()), *element.get("evidence", [])],
            "note": element.get("note", ""),
            "facadeEnd": facade_end_source,
            "extensionEvidence": element.get("extensionEvidence"),
        })

    if len(facade_points) >= 2:
        for (_, first), (_, second) in zip(facade_points, facade_points[1:]):
            cv2.line(image, first, second, (205, 115, 255), 2, cv2.LINE_AA)
        cv2.putText(image, "cyan=separate junction  magenta=selected outer envelope",
                    (16, image.shape[0] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
                    (230, 230, 230), 1, cv2.LINE_AA)

    args.overlay.parent.mkdir(parents=True, exist_ok=True)
    encoded_ok, encoded = cv2.imencode(args.overlay.suffix or ".png", image)
    if not encoded_ok:
        raise ValueError(f"cannot encode {args.overlay}")
    encoded.tofile(args.overlay)
    args.structures.write_text(json.dumps({
        "schemaVersion": "1.0",
        "sourceImage": str(args.image.resolve()),
        "sourceImageSha256": sha256(args.image),
        "metadata": str(args.metadata.resolve()),
        "elements": normalized,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "elements": len(normalized), "overlay": str(args.overlay), "structures": str(args.structures)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
