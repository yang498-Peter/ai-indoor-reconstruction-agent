#!/usr/bin/env python3
"""Render a browser-free delivery-model axonometric image with evidence hashes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box2d(center: list[float], width: float, depth: float, yaw: float) -> np.ndarray:
    local = np.asarray([[-width / 2, -depth / 2], [width / 2, -depth / 2],
                        [width / 2, depth / 2], [-width / 2, depth / 2]])
    rotation = np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
    return local @ rotation.T + np.asarray(center)


def add_prism(axis, footprint: np.ndarray, bottom: float, top: float, color: str,
              alpha: float = 1.0, edge: str = "#243238", linewidth: float = 0.25) -> None:
    lower = [(x, z, bottom) for x, z in footprint]
    upper = [(x, z, top) for x, z in footprint]
    faces = [upper, lower]
    faces.extend([[lower[i], lower[(i + 1) % len(lower)], upper[(i + 1) % len(lower)], upper[i]]
                  for i in range(len(lower))])
    axis.add_collection3d(Poly3DCollection(faces, facecolors=color, edgecolors=edge,
                                            linewidths=linewidth, alpha=alpha))


def material_color(item: dict, fallback: str) -> str:
    material = item.get("material", {})
    color = material.get("color") if isinstance(material, dict) else None
    return color if isinstance(color, str) and color.startswith("#") else fallback


def add_chairs(axis, item: dict) -> int:
    category = item.get("category")
    layout = item.get("layout", {})
    width, _, depth = map(float, item["size"])
    yaw = float(item.get("yaw", 0))
    center = np.asarray([item["center"][0], item["center"][2]], dtype=float)
    positions = []
    if category == "workstation":
        count = int(layout.get("seatsPerSide", 4))
        clearance = min(0.58, max(0.42, float(layout.get("chairClearanceM", 0.56))))
        positions = [(-width / 2 + (index + .5) * width / count, side * (depth / 2 + clearance))
                     for index in range(count) for side in (-1, 1)]
    elif category == "wall-workbench":
        count = int(layout.get("seatCount", max(1, round(width / 1.4))))
        side = int(layout.get("seatSide", 1)) or 1
        clearance = min(0.58, max(0.42, float(layout.get("chairClearanceM", 0.56))))
        offsets = layout.get("chairOffsetsM", [])
        positions = [(-width / 2 + (index + .5) * width / count + (offsets[index] if index < len(offsets) else 0),
                      side * (depth / 2 + clearance)) for index in range(count)]
    elif category in {"round-table", "oval-table"}:
        count = int(layout.get("seatCount", 6 if category == "oval-table" else 4))
        clearance = min(.42, max(.14, float(layout.get("chairClearanceM", .38))))
        positions = [(math.sin(index / count * math.tau) * (width / 2 + clearance),
                      math.cos(index / count * math.tau) * (depth / 2 + clearance)) for index in range(count)]
    elif category == "meeting-table":
        count = int(layout.get("seatCount", 6))
        slots = math.ceil(count / 2)
        clearance = min(.42, max(.14, float(layout.get("chairClearanceM", .38))))
        positions = [(-width / 2 + (index // 2 + .5) * width / slots,
                      (-1 if index % 2 == 0 else 1) * (depth / 2 + clearance)) for index in range(count)]
    rotation = np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
    for local in positions:
        chair_center = np.asarray(local) @ rotation.T + center
        add_prism(axis, box2d(chair_center.tolist(), .56, .56, yaw), .02, .47, "#aab3b2", .98)
        back_offset = np.asarray([0, .23]) @ rotation.T
        back_center = chair_center + back_offset
        add_prism(axis, box2d(back_center.tolist(), .50, .08, yaw), .42, .91, "#7f8c8d", .98)
    return len(positions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    scene = json.loads(args.scene.read_text(encoding="utf-8"))
    pipeline_by_id = {item.get("id"): item.get("status") for item in scene.get("pipeline", [])}
    delivery_ready = all(pipeline_by_id.get(stage) == "PASS" for stage in ("objects", "structures", "author"))
    figure = plt.figure(figsize=(16, 10), dpi=180, facecolor="#091014")
    axis = figure.add_subplot(111, projection="3d", facecolor="#091014")
    structure_count = 0
    for item in scene.get("structures", []):
        category = item.get("category")
        if category == "ceiling-zone":
            continue
        if category == "floor-zone":
            footprint = np.asarray([[point[0], point[2]] for point in item.get("points", [])], dtype=float)
            if len(footprint) >= 3:
                add_prism(axis, footprint, 0, .045, material_color(item, "#59605e"), .88, "none", 0)
            continue
        geometry = item.get("geometryType")
        footprint = None
        if geometry == "segment" and item.get("start") and item.get("end"):
            start, end = item["start"], item["end"]
            length = math.hypot(end[0] - start[0], end[2] - start[2])
            footprint = box2d([(start[0] + end[0]) / 2, (start[2] + end[2]) / 2], length,
                              max(.035, float(item.get("thickness", .08))), -math.atan2(end[2] - start[2], end[0] - start[0]))
        elif geometry == "rectangle" and item.get("center") and item.get("size"):
            footprint = box2d([item["center"][0], item["center"][2]], item["size"][0], item["size"][1], float(item.get("yaw", 0)))
        if footprint is None:
            continue
        base = float(item.get("baseHeight", 0))
        if base >= 1.5:
            continue
        color = material_color(item, {"glass": "#6fd9e3", "window": "#7cb6d8", "door": "#a77b54"}.get(category, "#d6d1c5"))
        alpha = .28 if category in {"glass", "window"} else .98
        actual_top = base + float(item.get("height", 3.0))
        cutaway_top = 1.18 if category in {"glass", "window"} else .92
        add_prism(axis, footprint, base, min(actual_top, cutaway_top), color, alpha)
        structure_count += 1
    object_count = 0
    chair_count = 0
    for item in scene.get("objects", []):
        if item.get("deliveryValidation", {}).get("status") != "PASS":
            continue
        center = [item["center"][0], item["center"][2]]
        width, height, depth = map(float, item["size"])
        yaw = float(item.get("yaw", 0))
        color = {"booth-desk": "#c48d57", "meeting-table": "#cda363",
                 "round-table": "#d5d1c7", "oval-table": "#d5d1c7"}.get(item.get("category"), "#ece9e0")
        add_prism(axis, box2d(center, width, depth, yaw), max(.06, height - .06), height, color, .99)
        if item.get("category") == "booth-desk":
            direction = np.asarray([math.cos(yaw), math.sin(yaw)])
            cross = np.asarray([-direction[1], direction[0]])
            for side in (-1, 1):
                bench_center = np.asarray(center) + cross * side * (depth / 2 + .38)
                add_prism(axis, box2d(bench_center.tolist(), width * .88, .42, yaw), .02, .42, "#79543f", .99)
                back_center = np.asarray(center) + cross * side * (depth / 2 + .54)
                add_prism(axis, box2d(back_center.tolist(), width * .88, .10, yaw), .42, .94, "#79543f", .99)
        leg_height = max(.12, height - .07)
        for x_sign in (-1, 1):
            for z_sign in (-1, 1):
                local = np.asarray([x_sign * max(0, width / 2 - .22), z_sign * max(0, depth / 2 - .18)])
                leg_center = local @ np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]]).T + np.asarray(center)
                add_prism(axis, box2d(leg_center.tolist(), .07, .07, yaw), .055, leg_height, "#879291", .98)
        chair_count += add_chairs(axis, item)
        object_count += 1
    all_points = []
    for item in scene.get("structures", []):
        all_points.extend([[p[0], p[2]] for p in item.get("points", [])])
        for key in ("start", "end", "center"):
            p = item.get(key)
            if p:
                all_points.append([p[0], p[2]])
    points = np.asarray(all_points, dtype=float)
    axis.set_xlim(float(points[:, 0].min()) - .25, float(points[:, 0].max()) + .25)
    axis.set_ylim(float(points[:, 1].min()) - .25, float(points[:, 1].max()) + .25)
    axis.set_zlim(0, float(scene.get("levels", [{}])[0].get("height", 3.05)) + .2)
    axis.set_box_aspect((np.ptp(points[:, 0]), np.ptp(points[:, 1]), 10))
    axis.view_init(elev=55, azim=-62)
    axis.set_axis_off()
    review_cn = "交付级剖切轴测审查" if delivery_ready else "证据图 · 待交付复核"
    review_en = "Delivery Cutaway Review" if delivery_ready else "Evidence Review · Not for Delivery"
    axis.set_title(f"AI 室内模型重建 · {review_cn}\nAI Interior Reconstruction · {review_en}",
                   color="#eef4f0", loc="left", pad=5, fontsize=13, fontweight="bold")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)
    manifest = {
        "schemaVersion": "1.0", "status": "DELIVERY_REVIEW" if delivery_ready else "EVIDENCE_ONLY", "mode": "delivery-cutaway-axonometric-no-ceiling",
        "scenePath": str(args.scene.resolve()), "sceneSha256": sha256_file(args.scene),
        "rendererPath": str(Path(__file__).resolve()), "rendererSha256": sha256_file(Path(__file__)),
        "outputPath": str(args.output.resolve()), "outputSha256": sha256_file(args.output),
        "renderedAt": datetime.now(timezone.utc).isoformat(), "structures": structure_count,
        "objects": object_count, "proceduralChairs": chair_count,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
