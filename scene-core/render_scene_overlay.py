#!/usr/bin/env python3
"""Overlay a Scene V2 graph onto an evidence orthophoto for visual QA.

The quality loop compares authority geometry against raw evidence in the
same pixel frame: walls, openings, items and zones are drawn over the
orthophoto using the evidence-manifest grid transform. Acceptance states
keep their meaning - accepted geometry is solid, candidates are dashed
orange, nothing rejected is drawn.

  python scene-core/render_scene_overlay.py --scene scene.json \
      --manifest evidence/evidence-manifest.json \
      --base evidence/ortho-top.png --output overlay.png
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

ACCEPTED_WALL = (80, 255, 120)
CANDIDATE = (255, 155, 80)
OPENING_DOOR = (255, 210, 90)
OPENING_WINDOW = (120, 210, 255)
ITEM_BOX = (90, 200, 255)
TREE = (110, 235, 110)
ZONE = (190, 140, 255)


def load_grid(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest["grid"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dim", type=float, default=0.55, help="base image dim factor")
    args = parser.parse_args()

    scene = json.loads(args.scene.read_text(encoding="utf-8"))
    grid = load_grid(args.manifest)
    origin_x, origin_y, cell = grid["originX"], grid["originY"], grid["cellSizeM"]

    base = Image.open(args.base).convert("RGB")
    base = Image.eval(base, lambda v: int(v * args.dim))
    draw = ImageDraw.Draw(base)

    def to_px(point):
        return ((point[0] - origin_x) / cell, (origin_y - point[1]) / cell)

    def dashed_line(a, b, color, width=2, dash=6):
        (x0, y0), (x1, y1) = a, b
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length / dash))
        for index in range(0, steps, 2):
            t0, t1 = index / steps, min(1, (index + 1) / steps)
            draw.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                       (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)], fill=color, width=width)

    nodes = scene.get("nodes", {})
    evidence = scene.get("evidence", {})

    def status_of(node_id):
        return evidence.get(node_id, {}).get("status", "candidate")

    for node_id, node in nodes.items():
        status = status_of(node_id)
        if status == "rejected":
            continue
        accepted = status.startswith("accepted")
        node_type = node.get("type")
        if node_type == "wall":
            a, b = to_px(node["start"]), to_px(node["end"])
            if accepted:
                draw.line([a, b], fill=ACCEPTED_WALL, width=max(2, int(node.get("thickness", 0.12) / cell)))
            else:
                dashed_line(a, b, CANDIDATE, width=3)
            direction = (node["end"][0] - node["start"][0], node["end"][1] - node["start"][1])
            length = math.hypot(*direction) or 1.0
            unit = (direction[0] / length, direction[1] / length)
            for child_id in node.get("children", []):
                child = nodes.get(child_id)
                if not child or status_of(child_id) == "rejected":
                    continue
                center = child.get("hostOffsetM", 0)
                half = child.get("width", 0.9) / 2
                p0 = to_px((node["start"][0] + unit[0] * (center - half), node["start"][1] + unit[1] * (center - half)))
                p1 = to_px((node["start"][0] + unit[0] * (center + half), node["start"][1] + unit[1] * (center + half)))
                color = OPENING_DOOR if child.get("type") == "door" else OPENING_WINDOW
                draw.line([p0, p1], fill=color, width=5)
        elif node_type == "item":
            cx, cy = node["center"]
            width, _, depth = node["size"]
            yaw = node.get("yaw", 0.0)
            color = TREE if node.get("category") == "tree" else ITEM_BOX
            if node.get("category") == "tree":
                radius = width / 2 / cell
                px, py = to_px((cx, cy))
                outline = color if accepted else CANDIDATE
                draw.ellipse([px - radius, py - radius, px + radius, py + radius], outline=outline, width=2)
                draw.line([(px - 4, py), (px + 4, py)], fill=outline, width=1)
                draw.line([(px, py - 4), (px, py + 4)], fill=outline, width=1)
            else:
                cos_y, sin_y = math.cos(yaw), math.sin(yaw)
                corners = []
                for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    dx, dy = sx * width / 2, sy * depth / 2
                    corners.append(to_px((cx + dx * cos_y - dy * sin_y, cy + dx * sin_y + dy * cos_y)))
                draw.polygon(corners, outline=color if accepted else CANDIDATE)
        elif node_type in {"zone", "slab"}:
            points = [to_px(p) for p in node.get("polygon", [])]
            if len(points) >= 3 and node_type == "zone":
                draw.polygon(points, outline=ZONE)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    base.save(args.output)
    print(json.dumps({"ok": True, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
