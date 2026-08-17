#!/usr/bin/env python3
"""Render a browser-free semantic plan for independent visual review."""

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
from matplotlib.lines import Line2D
from matplotlib.patches import Ellipse, Polygon
import numpy as np
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.ops import unary_union

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box(center: list[float], width: float, depth: float, yaw: float) -> np.ndarray:
    local = np.asarray([
        [-width / 2, -depth / 2], [width / 2, -depth / 2],
        [width / 2, depth / 2], [-width / 2, depth / 2],
    ])
    rotation = np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
    return local @ rotation.T + np.asarray(center)


def add_polygon(axis, points, face, edge, alpha=1.0, width=0.8, zorder=1):
    axis.add_patch(Polygon(points, closed=True, facecolor=face, edgecolor=edge,
                           linewidth=width, alpha=alpha, zorder=zorder))


def material_color(item: dict, fallback: str) -> str:
    material = item.get("material", {})
    color = material.get("color") if isinstance(material, dict) else None
    return color if isinstance(color, str) and color.startswith("#") else fallback


def accepted_v2(scene: dict, node_id: str) -> bool:
    return str(scene.get("evidence", {}).get(node_id, {}).get("status", "")).startswith("accepted-")


def compile_scene_v2_for_review(scene: dict) -> dict:
    """Compile the small browser-free review surface without importing JS."""
    structures: list[dict] = []
    objects: list[dict] = []
    nodes = scene.get("nodes", {})
    levels = [node for node in nodes.values() if node.get("type") == "level"]
    for node in nodes.values():
        node_type = node.get("type")
        if node_type == "wall" and accepted_v2(scene, node["id"]):
            start, end = node["start"], node["end"]
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            openings = [nodes[child] for child in node.get("children", [])
                        if child in nodes and nodes[child].get("type") in {"opening", "door", "window"}
                        and accepted_v2(scene, child)]
            intervals = sorted([(max(0.0, float(row.get("hostOffsetM", 0)) - float(row.get("width", 0)) / 2),
                                 min(length, float(row.get("hostOffsetM", 0)) + float(row.get("width", 0)) / 2), row)
                                for row in openings], key=lambda row: row[0])
            category = "glass" if node.get("wallKind") == "glass" else "wall"
            def emit(part_id, a, b, base, height):
                if b - a <= 1e-6 or height <= 1e-6:
                    return
                p0 = [start[0] + (end[0] - start[0]) * a / length, 0, -(start[1] + (end[1] - start[1]) * a / length)]
                p1 = [start[0] + (end[0] - start[0]) * b / length, 0, -(start[1] + (end[1] - start[1]) * b / length)]
                structures.append({"id": part_id, "category": category, "geometryType": "segment",
                                   "start": p0, "end": p1, "baseHeight": base, "height": height,
                                   "thickness": node.get("thickness", .08), "material": node.get("material", {}),
                                   "wallKind": node.get("wallKind", "solid")})
            cursor = 0.0
            for index, (a, b, opening) in enumerate(intervals):
                emit(f"{node['id']}-solid-{index}", cursor, a, node.get("baseHeight", 0), node.get("height", 3.0))
                sill = float(opening.get("sillHeight", 0))
                emit(f"{opening['id']}-sill", a, b, node.get("baseHeight", 0), sill)
                head_base = float(node.get("baseHeight", 0)) + sill + float(opening.get("height", 0))
                emit(f"{opening['id']}-head", a, b, head_base,
                     float(node.get("baseHeight", 0)) + float(node.get("height", 3.0)) - head_base)
                cursor = max(cursor, b)
            emit(f"{node['id']}-solid-tail", cursor, length, node.get("baseHeight", 0), node.get("height", 3.0))
        elif node_type == "slab" and accepted_v2(scene, node["id"]):
            structures.append({
                "id": node["id"], "category": "floor-zone", "geometryType": "polygon",
                "points": [[point[0], node.get("elevation", 0), -point[1]] for point in node.get("polygon", [])],
                "material": node.get("material", {}), "baseHeight": node.get("elevation", 0),
                "height": node.get("thickness", .05),
            })
        elif node_type == "item" and accepted_v2(scene, node["id"]):
            objects.append({
                "id": node["id"], "category": node.get("category", "item"),
                "center": [node["center"][0], node.get("elevation", 0), -node["center"][1]],
                "yaw": -float(node.get("yaw", 0)), "size": list(node.get("size", [0, 0, 0])),
                "color": node.get("color", "#e3dfd2"), "layout": node.get("layout", {}),
                "deliveryValidation": {"status": "PASS"}, "_atomicV2": True,
            })
    review = scene.get("review", {})
    return {
        "schemaVersion": scene.get("schemaVersion"), "structures": structures, "objects": objects,
        "levels": [{"id": row["id"], "name": row.get("name", "Level"), "height": row.get("height", 3.05)} for row in levels],
        "pipeline": [{"id": "objects", "status": "REVIEW"}, {"id": "structures", "status": "REVIEW"}, {"id": "author", "status": "REVIEW"}],
        "continuations": review.get("continuations", []), "openings": [],
    }


def object_chairs(item: dict) -> list[np.ndarray]:
    category = item.get("category")
    layout = item.get("layout", {})
    width, _, depth = map(float, item["size"])
    yaw = float(item.get("yaw", 0.0))
    center = np.asarray([float(item["center"][0]), float(item["center"][2])])
    positions: list[tuple[float, float]] = []
    if category == "workstation":
        total = max(0, int(layout.get("seatCount", 4)))
        slots = math.ceil(total / 2) if total else 1
        clearance = min(0.58, max(0.42, float(layout.get("chairClearanceM", 0.56))))
        positions = [(-width / 2 + (index // 2 + 0.5) * width / slots,
                      (-1 if index % 2 == 0 else 1) * (depth / 2 + clearance)) for index in range(total)]
    elif category == "wall-workbench":
        count = max(1, int(layout.get("seatCount", round(width / 1.4))))
        side = int(layout.get("seatSide", 1)) or 1
        offsets = layout.get("chairOffsetsM", [])
        clearance = min(0.58, max(0.42, float(layout.get("chairClearanceM", 0.56))))
        positions = [(-width / 2 + (index + 0.5) * width / count + (offsets[index] if index < len(offsets) else 0),
                      side * (depth / 2 + clearance)) for index in range(count)]
    elif category in {"round-table", "oval-table"}:
        count = max(0, int(layout.get("seatCount", 6 if category == "oval-table" else 4)))
        clearance = min(0.42, max(0.14, float(layout.get("chairClearanceM", 0.38))))
        positions = [(math.sin(index / count * math.tau) * (width / 2 + clearance),
                      math.cos(index / count * math.tau) * (depth / 2 + clearance)) for index in range(count)] if count else []
    elif category == "meeting-table":
        count = max(0, int(layout.get("seatCount", 6)))
        slots = math.ceil(count / 2) if count else 1
        offsets = layout.get("chairOffsetsM", [])
        clearance = min(0.42, max(0.14, float(layout.get("chairClearanceM", 0.38))))
        positions = [(-width / 2 + (index // 2 + 0.5) * width / slots + (offsets[index] if index < len(offsets) else 0),
                      (-1 if index % 2 == 0 else 1) * (depth / 2 + clearance)) for index in range(count)]
    rotation = np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
    return [box((np.asarray(position) @ rotation.T + center).tolist(), 0.58, 0.58, yaw) for position in positions]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    scene = json.loads(args.scene.read_text(encoding="utf-8"))
    if scene.get("schemaVersion") == "2.0" and isinstance(scene.get("nodes"), dict):
        scene = compile_scene_v2_for_review(scene)
    pipeline_by_id = {item.get("id"): item.get("status") for item in scene.get("pipeline", [])}
    delivery_ready = all(pipeline_by_id.get(stage) == "PASS" for stage in ("objects", "structures", "author"))
    figure, axis = plt.subplots(figsize=(12, 12), dpi=180)
    figure.patch.set_facecolor("#0b1013")
    axis.set_facecolor("#0b1013")

    structures = scene.get("structures", [])
    floors = [item for item in structures if item.get("category") == "floor-zone"]
    floor_polygons = [ShapelyPolygon([[point[0], point[2]] for point in item.get("points", [])]) for item in floors]
    floor_union = unary_union([polygon for polygon in floor_polygons if polygon.is_valid and polygon.area > 0.01])
    floor_parts = [floor_union] if floor_union.geom_type == "Polygon" else list(floor_union.geoms)
    for polygon in floor_parts:
        add_polygon(axis, np.asarray(polygon.exterior.coords[:-1]), "#656a68", "none", 0.94, 0, 1)
    zone_colors = ["#526e70", "#6b655b", "#555f72", "#706255"]
    for index, room in enumerate(scene.get("spaces", [])):
        points = np.asarray([[point[0], point[2]] for point in room.get("points", [])], dtype=float)
        if len(points) < 3:
            continue
        add_polygon(axis, points, zone_colors[index % len(zone_colors)], "none", 0.22, 0, 2)
        center = points.mean(axis=0)
        axis.text(center[0], center[1], room.get("name", room.get("id", "SPACE")), color="#d9e8e3",
                  fontsize=7, ha="center", va="center", alpha=.82, zorder=3)

    category_colors = {
        "wall": "#d7d2c7", "solid-wall": "#d7d2c7", "partition": "#c3beb2",
        "column": "#c7c3b9", "glass": "#63d5df", "window": "#6daed4", "door": "#b98b5d",
    }
    structure_count = 0
    for item in structures:
        category = item.get("category")
        if category in {"floor-zone", "ceiling-zone"}:
            continue
        color = category_colors.get(category, "#d7d2c7")
        color = material_color(item, color)
        geometry_type = item.get("geometryType")
        footprint = None
        if geometry_type == "segment":
            start, end = item.get("start"), item.get("end")
            if not start or not end:
                continue
            if float(item.get("baseHeight", 0)) >= 2.0:
                axis.plot([start[0], end[0]], [start[2], end[2]], color=color,
                          linewidth=0.65, linestyle=(0, (4, 3)), alpha=0.75, zorder=6)
                continue
            dx, dz = end[0] - start[0], end[2] - start[2]
            length = math.hypot(dx, dz)
            footprint = box([(start[0] + end[0]) / 2, (start[2] + end[2]) / 2], length,
                            max(0.035, float(item.get("thickness", 0.08))), -math.atan2(dz, dx))
        elif geometry_type == "rectangle":
            center, size = item.get("center"), item.get("size")
            if center and size:
                footprint = box([center[0], center[2]], size[0], size[1], float(item.get("yaw", 0)))
        if footprint is not None:
            alpha = 0.22 if item.get("scanContinuation") else (0.86 if category in {"glass", "window"} else (.72 if "inferred" in str(item.get("status", "")) else 1.0))
            add_polygon(axis, footprint, color, color, alpha, 0.45, 5)
            structure_count += 1

    object_count = 0
    chair_count = 0
    booth_bench_count = 0
    for item in scene.get("objects", []):
        center = [item["center"][0], item["center"][2]]
        accepted = item.get("deliveryValidation", {}).get("status") == "PASS"
        category = item.get("category")
        object_face = {
            "workstation": "#e9e6de", "wall-workbench": "#e8e4dc",
            "booth-desk": "#c99762", "round-table": "#d6d2c9",
            "oval-table": "#d6d2c9", "meeting-table": "#d1a96f",
        }.get(category, "#e3dfd2")
        object_edge = "#f4f1e8" if accepted else "#ff9a55"
        if category == "u-counter":
            width, _, depth = map(float, item["size"])
            yaw = float(item.get("yaw", 0))
            bar_depth = min(.62, max(.42, float(item.get("layout", {}).get("barDepthM", .52))))
            rotation = np.asarray([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
            local_runs = [(0, -depth / 2 + bar_depth / 2, width, bar_depth),
                          (-width / 2 + bar_depth / 2, 0, bar_depth, depth),
                          (width / 2 - bar_depth / 2, 0, bar_depth, depth)]
            for local_x, local_z, run_width, run_depth in local_runs:
                run_center = np.asarray([local_x, local_z]) @ rotation.T + np.asarray(center)
                add_polygon(axis, box(run_center.tolist(), run_width, run_depth, yaw), object_face,
                            object_edge, 1.0, 0.7, 8)
        elif category in {"round-table", "oval-table"}:
            axis.add_patch(Ellipse(center, float(item["size"][0]), float(item["size"][2]),
                                   angle=-math.degrees(float(item.get("yaw", 0))),
                                   facecolor=object_face if accepted else "none", edgecolor=object_edge,
                                   linewidth=0.7 if accepted else 1.4, zorder=8))
        else:
            footprint = box(center, float(item["size"][0]), float(item["size"][2]), float(item.get("yaw", 0)))
            add_polygon(axis, footprint, object_face if accepted else "none",
                        object_edge, 1.0, 0.7 if accepted else 1.4, 8)
        if category == "booth-desk" and accepted:
            yaw = float(item.get("yaw", 0))
            direction = np.asarray([math.cos(yaw), math.sin(yaw)])
            cross = np.asarray([-direction[1], direction[0]])
            center_array = np.asarray(center)
            for side in (-1, 1):
                bench_center = center_array + cross * side * (float(item["size"][2]) / 2 + 0.38)
                bench = box(bench_center.tolist(), float(item["size"][0]) * 0.88, 0.42, yaw)
                add_polygon(axis, bench, "#8b6047", "#d8b18a", 1.0, 0.55, 7)
                booth_bench_count += 1
        for chair in object_chairs(item):
            if accepted:
                add_polygon(axis, chair, "#9da6a5", "#e5e9e5", 1.0, 0.35, 7)
                chair_count += 1
        object_count += 1

    axis.set_aspect("equal", adjustable="datalim")
    axis.autoscale_view()
    axis.margins(0.04)
    axis.axis("off")
    title_cn = "交付级语义平面审查" if delivery_ready else "证据图 · 待交付复核"
    title_en = "Delivery Plan Review" if delivery_ready else "Evidence Review · Not for Delivery"
    axis.set_title(f"AI 室内模型重建 · {title_cn}\nAI Interior Reconstruction · {title_en}",
                   color="#eaf0ed", fontsize=13, pad=12, loc="left", fontweight="bold")
    x_min, x_max = axis.get_xlim()
    y_min, y_max = axis.get_ylim()
    axis.annotate("LOCAL +Z", xy=(x_max - 0.6, y_max - 0.4), xytext=(x_max - 0.6, y_max - 2.2),
                  ha="center", va="center", color="#66e2c2", fontsize=10,
                  arrowprops={"arrowstyle": "-|>", "color": "#66e2c2", "lw": 1.4})
    scale_y = y_min + 0.55
    axis.plot([x_min + 0.65, x_min + 5.65], [scale_y, scale_y], color="#eaf0ed", linewidth=2.0)
    axis.text(x_min + 3.15, scale_y + 0.25, "5 m", color="#eaf0ed", fontsize=8, ha="center")
    for continuation in scene.get("continuations", []):
        point = continuation.get("anchor") or continuation.get("center")
        if not point:
            continue
        origin = scene["coordinateSystem"]["sourceOrigin"]
        display_point = [point[0] - origin[0], -(point[1] - origin[1])]
        axis.scatter(display_point[0], display_point[1], marker=">", s=18, color="#ffbf69", zorder=11)
        axis.text(display_point[0] + .20, display_point[1] + .20, "SCAN LIMIT", color="#ffbf69", fontsize=7, zorder=11)
    for opening in scene.get("openings", []):
        start, end = opening.get("start"), opening.get("end")
        if start and end:
            origin = scene["coordinateSystem"]["sourceOrigin"]
            axis.plot([start[0] - origin[0], end[0] - origin[0]],
                      [-(start[1] - origin[1]), -(end[1] - origin[1])],
                      color="#6fe4bd", linewidth=1.3, linestyle=(0, (2, 2)), zorder=11)
    legend = [
        Line2D([0], [0], color="#d7d2c7", lw=4, label="实体结构 / Solid"),
        Line2D([0], [0], color="#63d5df", lw=4, label="玻璃 / Glass"),
        Line2D([0], [0], color="#e9e6de", lw=4, label="已接受家具 / Accepted"),
        Line2D([0], [0], color="#ff9a55", lw=1.5, label="待审核轮廓 / Review"),
        Line2D([0], [0], color="#d7d2c7", lw=1, linestyle=(0, (4, 3)), label="高位墙头 / Head"),
        Line2D([0], [0], color="#ffbf69", marker=">", lw=0, label="扫描边界 / Scan limit"),
        Line2D([0], [0], color="#8b6047", lw=4, label="内置长凳 / Booth bench"),
    ]
    axis.legend(handles=legend, loc="lower right", frameon=True, facecolor="#11191d",
                edgecolor="#29363c", labelcolor="#dce7e3", fontsize=7, ncol=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, bbox_inches="tight", facecolor=figure.get_facecolor())
    plt.close(figure)
    manifest = {
        "schemaVersion": "1.0", "status": "DELIVERY_REVIEW" if delivery_ready else "EVIDENCE_ONLY",
        "scenePath": str(args.scene.resolve()), "sceneSha256": sha256_file(args.scene),
        "rendererPath": str(Path(__file__).resolve()), "rendererSha256": sha256_file(Path(__file__)),
        "outputPath": str(args.output.resolve()), "outputSha256": sha256_file(args.output),
        "renderedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "semantic-model-top-no-grid", "structureFootprints": structure_count,
        "objects": object_count, "proceduralChairs": chair_count, "boothBenches": booth_bench_count,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
