#!/usr/bin/env python3
"""Project authoritative scene geometry into posed capture photos.

For each selected element (walls by default) the tool picks the N best posed
frames ("closest camera whose view covers the most of the element"), draws the
element wireframe (cyan) and its opening rectangles (magenta) onto the
undistorted photo, and writes PNG overlays plus a manifest whose lineage
fields follow ``scene_api.evidence_lineage_id``.

Coordinate conventions (verified; see photo_projection docstring): scene plan
x/y == LAS x/y, LAS z = scene elevation + ``--ground-z`` (rtk-house-2:
-0.50), transforms.json is c2w OpenGL in the LAS frame.

  python scene-core/render_photo_overlay.py \
      --scene outputs/rtk-house-2/scene.json \
      --transforms <capture>/transforms.json \
      --element wall_cabin_south --ground-z=-0.5 \
      --output outputs/rtk-house-2/dossiers/overlays
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import photo_projection as pp
from scene_api import evidence_lineage_id

PRODUCER = "render-photo-overlay-v1"
WIREFRAME_COLOR = (0, 255, 255)  # cyan
OPENING_COLOR = (255, 0, 255)  # magenta
LINE_WIDTH = 2


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_scene(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def element_wireframe(scene: dict, element_id: str, ground_z: float) -> dict:
    nodes = scene["nodes"]
    node = nodes.get(element_id)
    if node is None:
        raise KeyError(f"element not found in scene: {element_id}")
    if node.get("type") != "wall" or "start" not in node or "end" not in node:
        raise ValueError(f"photo overlay currently supports wall elements only: {element_id}")
    children = [nodes[cid] for cid in node.get("children", []) if cid in nodes]
    return pp.wall_wireframe(node, ground_z, children)


def _sample_outline(outline: list[list[float]], step: float = 0.25) -> np.ndarray:
    """Densify the closed outline so coverage scoring sees the whole element."""
    points = []
    loop = np.asarray(outline, dtype=np.float64)
    for index in range(len(loop)):
        a = loop[index]
        b = loop[(index + 1) % len(loop)]
        length = float(np.linalg.norm(b - a))
        count = max(2, int(math_ceil(length / step)))
        for t in np.linspace(0.0, 1.0, count, endpoint=False):
            points.append(a + t * (b - a))
    return np.asarray(points)


def math_ceil(value: float) -> int:
    return int(-(-value // 1))


def score_frames(
    frames: list[pp.Frame],
    wire: dict,
    *,
    top_n: int = 3,
    min_coverage: float = 0.5,
    min_separation_m: float = 0.8,
) -> list[tuple[pp.Frame, dict]]:
    """Rank frames by how well they SHOW the element.

    Coverage (fraction of densified outline samples that project inside the
    image) is a gate; the ranking key is projected outline area x coverage x
    incidence (how face-on the camera is to the wall plane), which prefers
    frontal, complete viewpoints over grazing shots taken along the wall or
    extreme close-ups.
    """
    samples = _sample_outline(wire["outline"])
    corners = np.asarray(wire["outline"], dtype=np.float64)
    center = corners.mean(axis=0)
    edge_a = corners[1] - corners[0]
    edge_b = corners[3] - corners[0]
    normal = np.cross(edge_a, edge_b)
    norm_len = float(np.linalg.norm(normal))
    normal = normal / norm_len if norm_len > 0 else np.array([0.0, 0.0, 1.0])
    scored = []
    for frame in frames:
        if frame.image_path is None:
            continue
        uv, depth, in_front = pp.project_points(frame, samples)
        if not in_front.any():
            continue
        visible = pp.in_image(frame, uv, margin=0.0) & in_front
        coverage = float(visible.sum()) / float(len(samples))
        if coverage < min_coverage:
            continue
        to_camera = frame.center - center
        distance = float(np.linalg.norm(to_camera))
        # cameras standing inside (or nearly inside) the wall are degenerate
        if distance < 1.2 or abs(float(np.dot(to_camera, normal))) < 0.8:
            continue
        incidence = abs(float(np.dot(to_camera / distance, normal)))
        corner_uv, _, corner_front = pp.project_points(frame, corners)
        if not corner_front.all():
            continue
        # shoelace area of the projected outline, clamped to the image size
        polygon = corner_uv
        area = 0.5 * abs(
            float(
                np.sum(
                    polygon[:, 0] * np.roll(polygon[:, 1], -1)
                    - np.roll(polygon[:, 0], -1) * polygon[:, 1]
                )
            )
        )
        area = min(area, float(frame.camera.width * frame.camera.height))
        scored.append((area * coverage * incidence, coverage, distance, frame))
    scored.sort(key=lambda item: -item[0])
    picked: list[tuple[pp.Frame, dict]] = []
    for score, coverage, distance, frame in scored:
        if len(picked) >= top_n:
            break
        if any(
            np.linalg.norm(frame.center - other.center) < min_separation_m
            for other, _ in picked
        ):
            continue
        picked.append(
            (
                frame,
                {
                    "coverage": round(coverage, 4),
                    "distanceM": round(distance, 3),
                    "projectedAreaPx": round(score / max(coverage, 1e-9), 1),
                },
            )
        )
    return picked


def draw_overlay(
    frame: pp.Frame,
    wire: dict,
    *,
    element_id: str,
    line_width: int = LINE_WIDTH,
) -> Image.Image:
    image = Image.open(frame.image_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    def draw_loop(points3d, color):
        for polyline in pp.project_polyline(frame, points3d, closed=True):
            draw.line([(u, v) for u, v in polyline], fill=color, width=line_width)

    draw_loop(wire["outline"], WIREFRAME_COLOR)
    for opening in wire["openings"]:
        draw_loop(opening["rect"], OPENING_COLOR)

    # legend (top-left)
    legend = [
        (WIREFRAME_COLOR, f"{element_id} wireframe"),
        (OPENING_COLOR, "openings"),
    ]
    pad, swatch, line_h = 8, 14, 20
    box_w = 240
    box_h = pad * 2 + line_h * len(legend)
    draw.rectangle([4, 4, 4 + box_w, 4 + box_h], fill=(0, 0, 0))
    for row, (color, label) in enumerate(legend):
        y = 4 + pad + row * line_h
        draw.rectangle([4 + pad, y + 2, 4 + pad + swatch, y + 2 + swatch], fill=color)
        draw.text((4 + pad + swatch + 6, y), label, fill=(255, 255, 255))
    return image


def render_element_overlays(
    scene_path: Path,
    transforms_path: Path,
    element_ids: list[str],
    output_dir: Path,
    *,
    ground_z: float,
    top_n: int = 3,
    image_root: Path | None = None,
    min_coverage: float = 0.5,
) -> dict:
    scene = load_scene(scene_path)
    frames = pp.load_frames(transforms_path, image_root=image_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "schemaVersion": "1.0",
        "artifactType": "photo-overlay",
        "producer": PRODUCER,
        "scene": str(scene_path),
        "sceneRevision": scene.get("revision"),
        "transforms": str(transforms_path),
        "transformsContentSha256": _sha256_file(transforms_path),
        "groundZ": ground_z,
        "coordinateNote": "scene plan x/y == LAS x/y; las_z = elevation + groundZ; c2w OpenGL",
        "style": {
            "wireframeColor": "#00ffff",
            "openingColor": "#ff00ff",
            "lineWidthPx": LINE_WIDTH,
        },
        "elements": [],
    }
    for element_id in element_ids:
        wire = element_wireframe(scene, element_id, ground_z)
        picked = score_frames(frames, wire, top_n=top_n, min_coverage=min_coverage)
        record: dict = {"elementId": element_id, "overlays": []}
        for frame, stats in picked:
            safe_frame = frame.file_path.replace("/", "_").replace("\\", "_")
            out_path = output_dir / f"{element_id}--{safe_frame}.png"
            draw_overlay(frame, wire, element_id=element_id).save(out_path)
            image_sha = _sha256_file(frame.image_path)
            parameters = {
                "elementId": element_id,
                "frameId": frame.frame_id,
                "groundZ": ground_z,
                "camera": {
                    "width": frame.camera.width,
                    "height": frame.camera.height,
                    "fx": frame.camera.fx,
                    "fy": frame.camera.fy,
                    "cx": frame.camera.cx,
                    "cy": frame.camera.cy,
                    "model": frame.camera.model,
                },
                "cameraCenter": [round(float(v), 6) for v in frame.center],
            }
            record["overlays"].append(
                {
                    "frameId": frame.frame_id,
                    "imagePath": frame.file_path,
                    "imageContentSha256": image_sha,
                    "output": out_path.name,
                    "outputContentSha256": _sha256_file(out_path),
                    "selection": stats,
                    "projection": parameters,
                    "lineageId": evidence_lineage_id([image_sha], PRODUCER, parameters),
                }
            )
        manifest["elements"].append(record)
    manifest_path = output_dir / "photo-overlay-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--transforms", required=True, type=Path)
    parser.add_argument(
        "--element",
        action="append",
        default=None,
        help="element id (repeatable); default: all walls in the scene",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--ground-z",
        type=float,
        required=True,
        help="LAS z of scene elevation zero (rtk-house-2: -0.5)",
    )
    parser.add_argument("--frames-per-element", type=int, default=3)
    parser.add_argument("--image-root", type=Path, default=None)
    parser.add_argument("--min-coverage", type=float, default=0.5)
    args = parser.parse_args()

    scene = load_scene(args.scene)
    element_ids = args.element or [
        node["id"] for node in scene["nodes"].values() if node.get("type") == "wall"
    ]
    manifest = render_element_overlays(
        args.scene,
        args.transforms,
        element_ids,
        args.output,
        ground_z=args.ground_z,
        top_n=args.frames_per_element,
        image_root=args.image_root,
        min_coverage=args.min_coverage,
    )
    total = sum(len(record["overlays"]) for record in manifest["elements"])
    print(json.dumps({"ok": True, "elements": len(manifest["elements"]), "overlays": total, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
