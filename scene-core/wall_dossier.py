#!/usr/bin/env python3
"""One-page visual dossier for a scene element (wall).

The dossier is the core reference visual for a multimodal agent: it collages
  * a plan orthophoto crop centred on the element (wall line + camera marks),
  * the point-cloud elevation/section for the wall corridor (reused from
    ``outputs/<cap>/sections`` when the scene evidence already references one,
    otherwise rendered on demand from a CaptureIndex),
  * the N best photo overlays (authoritative wireframe projected into posed
    undistorted photos; cyan outline / magenta openings, 2 px, with legend),
  * a text summary of geometry and evidence lineage.

  python scene-core/wall_dossier.py \
      --scene outputs/rtk-house-2/scene.json \
      --transforms <capture>/transforms.json \
      --element wall_cabin_south "--ground-z=-0.5" \
      --output outputs/rtk-house-2/dossiers/wall_cabin_south.png
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import photo_projection as pp
import render_photo_overlay as rpo
from scene_api import evidence_lineage_id

PRODUCER = "wall-dossier-v1"
BACKGROUND = (24, 24, 28)
PANEL = (38, 38, 44)
TEXT = (235, 235, 235)
MUTED = (170, 170, 178)
WALL_COLOR = (0, 255, 255)
OPENING_COLOR = (255, 0, 255)
CAMERA_COLOR = (255, 210, 60)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def plan_crop_panel(
    scene_dir: Path,
    wall: dict,
    picked_frames: list,
    *,
    margin_m: float = 2.5,
    size_px: int = 640,
) -> tuple[Image.Image | None, dict | None]:
    """Crop the existing plan orthophoto around the wall and annotate it."""
    manifest_path = scene_dir / "evidence" / "evidence-manifest.json"
    # band-walls (ground-relative wall band) shows wall footprints; the plain
    # top-surface ortho is often pure tree canopy on wooded sites.
    ortho_path = None
    for name in ("band-walls.png", "ortho-top.png"):
        candidate = scene_dir / "evidence" / name
        if candidate.is_file():
            ortho_path = candidate
            break
    if not manifest_path.is_file() or ortho_path is None:
        return None, None
    manifest = _load_json(manifest_path)
    grid = manifest.get("grid") or {}
    try:
        origin_x = float(grid["originX"])
        origin_y = float(grid["originY"])
        cell = float(grid["cellSizeM"])
    except (KeyError, TypeError, ValueError):
        return None, None

    start = np.asarray(wall["start"], dtype=np.float64)
    end = np.asarray(wall["end"], dtype=np.float64)
    min_x = min(start[0], end[0]) - margin_m
    max_x = max(start[0], end[0]) + margin_m
    min_y = min(start[1], end[1]) - margin_m
    max_y = max(start[1], end[1]) + margin_m

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x) / cell, (origin_y - y) / cell

    left, top = to_px(min_x, max_y)
    right, bottom = to_px(max_x, min_y)
    image = Image.open(ortho_path).convert("RGB")
    left = int(max(0, math.floor(left)))
    top = int(max(0, math.floor(top)))
    right = int(min(image.width, math.ceil(right)))
    bottom = int(min(image.height, math.ceil(bottom)))
    if right <= left or bottom <= top:
        return None, None
    crop = image.crop((left, top, right, bottom))
    scale = size_px / max(crop.width, crop.height)
    crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    draw = ImageDraw.Draw(crop)

    def to_crop(x: float, y: float) -> tuple[float, float]:
        px, py = to_px(x, y)
        return (px - left) * scale, (py - top) * scale

    draw.line([to_crop(*start), to_crop(*end)], fill=WALL_COLOR, width=3)
    for frame, _stats in picked_frames:
        cx, cy = to_crop(frame.center[0], frame.center[1])
        radius = 5
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=CAMERA_COLOR)
    meta = {
        "source": f"evidence/{ortho_path.name}",
        "cropSource": [min_x, min_y, max_x, max_y],
        "scale": scale,
    }
    return crop, meta


def find_existing_section(scene: dict, scene_dir: Path, element_id: str) -> Path | None:
    record = (scene.get("evidence") or {}).get(element_id) or {}
    for source in record.get("sources", []):
        path = str(source.get("path", ""))
        if path.endswith(".png") and "section" in path:
            candidate = scene_dir / path
            if candidate.is_file():
                return candidate
    return None


def render_section_from_index(
    index_root: Path,
    wall: dict,
    output_dir: Path,
    *,
    ground_z: float,
    name: str,
) -> Path | None:
    from capture_index import CaptureIndex
    from indexed_pointcloud_evidence import render_elevation

    index = CaptureIndex.open(index_root)
    base = float(wall.get("baseHeight", 0.0)) + ground_z
    top = base + float(wall["height"])
    render_elevation(
        index,
        output_dir,
        [tuple(wall["start"]), tuple(wall["end"])],
        corridor_width=max(0.6, float(wall.get("thickness", 0.2)) * 3.0),
        zrange=(base - 1.0, top + 1.0),
        name=name,
    )
    return output_dir / f"{name}.png"


def _wrap(text: str, limit: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        joined = f"{current} {word}".strip()
        if len(joined) > limit and current:
            lines.append(current)
            current = word
        else:
            current = joined
    if current:
        lines.append(current)
    return lines


def build_dossier(
    scene_path: Path,
    transforms_path: Path,
    element_id: str,
    output_path: Path,
    *,
    ground_z: float,
    top_n: int = 3,
    image_root: Path | None = None,
    index_root: Path | None = None,
    min_coverage: float = 0.5,
) -> dict:
    scene = _load_json(scene_path)
    scene_dir = scene_path.parent
    nodes = scene["nodes"]
    wall = nodes.get(element_id)
    if wall is None:
        raise KeyError(f"element not found: {element_id}")
    if wall.get("type") != "wall":
        raise ValueError(f"wall dossier currently supports walls only: {element_id}")
    children = [nodes[cid] for cid in wall.get("children", []) if cid in nodes]
    wire = pp.wall_wireframe(wall, ground_z, children)

    frames = pp.load_frames(transforms_path, image_root=image_root)
    picked = rpo.score_frames(frames, wire, top_n=top_n, min_coverage=min_coverage)

    plan_panel, plan_meta = plan_crop_panel(scene_dir, wall, picked)
    section_path = find_existing_section(scene, scene_dir, element_id)
    section_origin = "scene-evidence"
    if section_path is None and index_root is not None:
        section_path = render_section_from_index(
            index_root,
            wall,
            output_path.parent / "sections",
            ground_z=ground_z,
            name=f"{element_id}-section",
        )
        section_origin = "rendered-from-index"

    photo_panels: list[tuple[Image.Image, pp.Frame, dict]] = []
    for frame, stats in picked:
        overlay = rpo.draw_overlay(frame, wire, element_id=element_id)
        photo_panels.append((overlay, frame, stats))

    # ---- compose ------------------------------------------------------
    photo_h = 720
    pad = 16
    header_h = 56
    top_row_h = 660
    text_w = 560

    plan_img = plan_panel if plan_panel is not None else Image.new("RGB", (640, 640), PANEL)
    plan_scale = (top_row_h - 2 * pad) / plan_img.height
    plan_img = plan_img.resize(
        (int(plan_img.width * plan_scale), int(plan_img.height * plan_scale))
    )
    section_img = None
    if section_path is not None and section_path.is_file():
        section_img = Image.open(section_path).convert("RGB")
        section_scale = min(
            (top_row_h - 2 * pad) / section_img.height,
            900 / section_img.width,
        )
        section_img = section_img.resize(
            (
                max(1, int(section_img.width * section_scale)),
                max(1, int(section_img.height * section_scale)),
            )
        )

    photos_scaled = []
    for overlay, frame, stats in photo_panels:
        scale = photo_h / overlay.height
        photos_scaled.append(
            (overlay.resize((int(overlay.width * scale), photo_h)), frame, stats)
        )

    top_widths = plan_img.width + (section_img.width if section_img else 320) + text_w + 4 * pad
    bottom_widths = sum(item[0].width for item in photos_scaled) + pad * (len(photos_scaled) + 1)
    width = max(top_widths, bottom_widths, 1600)
    height = header_h + top_row_h + photo_h + 3 * pad + 40

    page = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(page)

    dataset = scene.get("dataset", "?")
    title = f"WALL DOSSIER - {element_id}  [{dataset}]"
    draw.text((pad, pad + 8), title, fill=TEXT)
    draw.text(
        (pad, pad + 26),
        f"scene plan == LAS x/y; las_z = elevation + ({ground_z}); wireframe=cyan, openings=magenta",
        fill=MUTED,
    )

    x_cursor = pad
    y0 = header_h
    page.paste(plan_img, (x_cursor, y0 + pad))
    draw.text((x_cursor, y0 + pad + plan_img.height + 4), "plan ortho crop (wall line + cameras)", fill=MUTED)
    x_cursor += plan_img.width + pad
    if section_img is not None:
        page.paste(section_img, (x_cursor, y0 + pad))
        draw.text(
            (x_cursor, y0 + pad + section_img.height + 4),
            f"point-cloud section ({section_origin}: {section_path.name})",
            fill=MUTED,
        )
        x_cursor += section_img.width + pad
    else:
        draw.text((x_cursor, y0 + pad), "no section available", fill=MUTED)
        x_cursor += 320 + pad

    # text panel
    start = wall["start"]
    end = wall["end"]
    lines: list[str] = []
    lines.append(f"start=({start[0]:.3f}, {start[1]:.3f})  end=({end[0]:.3f}, {end[1]:.3f})")
    lines.append(
        f"length={wire['lengthM']:.3f} m  height={wall['height']:.2f} m  "
        f"base={wall.get('baseHeight', 0.0):.2f} m  thickness={wall.get('thickness', 0.0):.2f} m"
    )
    material = wall.get("material") or {}
    if material:
        lines.append(f"material: {material.get('color', '?')} {material.get('description', '')}")
    for opening in wire["openings"]:
        rect = opening["rect"]
        lines.append(
            f"opening {opening['id']} ({opening['type']}): sill z={rect[0][2]:.2f}, head z={rect[2][2]:.2f} (LAS)"
        )
    record = (scene.get("evidence") or {}).get(element_id) or {}
    lines.append(f"evidence status: {record.get('status', 'none')}")
    for source in record.get("sources", [])[:4]:
        note = str(source.get("note", ""))[:70]
        lines.append(f"  - {source.get('type')}: {source.get('path')}")
        if note:
            lines.append(f"      {note}")
    for _, frame, stats in photos_scaled:
        lines.append(
            f"photo {frame.file_path}: coverage={stats['coverage']}, dist={stats['distanceM']} m"
        )
    text_y = y0 + pad
    for raw_line in lines:
        for line in _wrap(raw_line, 66):
            draw.text((x_cursor, text_y), line, fill=TEXT)
            text_y += 18
        if text_y > y0 + top_row_h - 18:
            break

    y1 = header_h + top_row_h + 2 * pad
    x_cursor = pad
    for overlay, frame, stats in photos_scaled:
        page.paste(overlay, (x_cursor, y1))
        draw.text((x_cursor, y1 + photo_h + 4), frame.file_path, fill=MUTED)
        x_cursor += overlay.width + pad

    output_path.parent.mkdir(parents=True, exist_ok=True)
    page.save(output_path)

    transforms_sha = _sha256_file(transforms_path)
    parameters = {
        "elementId": element_id,
        "groundZ": ground_z,
        "framesPerElement": top_n,
        "frameIds": [frame.frame_id for _, frame, _ in photos_scaled],
        "sectionOrigin": section_origin if section_path else None,
    }
    manifest = {
        "schemaVersion": "1.0",
        "artifactType": "wall-dossier",
        "producer": PRODUCER,
        "elementId": element_id,
        "scene": str(scene_path),
        "sceneRevision": scene.get("revision"),
        "transformsContentSha256": transforms_sha,
        "groundZ": ground_z,
        "planCrop": plan_meta,
        "section": str(section_path) if section_path else None,
        "photos": [
            {
                "frameId": frame.frame_id,
                "imagePath": frame.file_path,
                "selection": stats,
            }
            for _, frame, stats in photos_scaled
        ],
        "output": str(output_path),
        "outputContentSha256": _sha256_file(output_path),
        "lineageId": evidence_lineage_id([transforms_sha], PRODUCER, parameters),
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--transforms", required=True, type=Path)
    parser.add_argument("--element", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ground-z", type=float, required=True,
                        help="LAS z of scene elevation zero (rtk-house-2: -0.5)")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--image-root", type=Path, default=None)
    parser.add_argument("--index", type=Path, default=None,
                        help="CaptureIndex root used to render a section when the scene has none")
    parser.add_argument("--min-coverage", type=float, default=0.5)
    args = parser.parse_args()

    manifest = build_dossier(
        args.scene,
        args.transforms,
        args.element,
        args.output,
        ground_z=args.ground_z,
        top_n=args.frames,
        image_root=args.image_root,
        index_root=args.index,
        min_coverage=args.min_coverage,
    )
    print(json.dumps({"ok": True, "output": manifest["output"], "photos": len(manifest["photos"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
