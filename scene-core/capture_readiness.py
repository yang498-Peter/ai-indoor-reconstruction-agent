#!/usr/bin/env python3
"""Fail-closed capture adapter, coordinate-frame and pose readiness helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import laspy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_REGISTRY_PATH = ROOT / "schemas" / "capture-adapters-v1.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def adapter_registry() -> tuple[dict[str, Any], str]:
    value = json.loads(ADAPTER_REGISTRY_PATH.read_text(encoding="utf-8"))
    return value, sha256_file(ADAPTER_REGISTRY_PATH)


def point_cloud_adapter(path: Path) -> dict[str, Any]:
    registry, registry_digest = adapter_registry()
    suffix = path.suffix.casefold()
    selected = next(
        (
            dict(item)
            for item in registry["pointCloudAdapters"]
            if suffix in item.get("extensions", [])
        ),
        {
            "id": "unknown-point-cloud-v1",
            "extensions": [suffix],
            "status": "UNSUPPORTED",
            "parser": None,
            "coordinatePolicy": "none",
        },
    )
    selected["registryDigest"] = registry_digest
    status = selected["status"]
    if status == "CONDITIONAL" and suffix == ".laz":
        status = "SUPPORTED" if laspy.LazBackend.detect_available() else "UNSUPPORTED"
        selected["backendAvailable"] = status == "SUPPORTED"
    if status == "SUPPORTED":
        try:
            with laspy.open(path) as reader:
                selected["pointCount"] = int(reader.header.point_count)
                selected["pointFormat"] = int(reader.header.point_format.id)
        except Exception as error:
            status = "INVALID"
            selected["probeError"] = type(error).__name__
    selected["status"] = status
    return selected


def normalize_coordinate_frame(value: dict[str, object] | None) -> tuple[dict[str, str] | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["coordinate frame is not declared"]
    aliases = {
        "m": "metre",
        "meter": "metre",
        "meters": "metre",
        "metres": "metre",
        "metre": "metre",
        "ft": "foot",
        "foot": "foot",
        "feet": "foot",
        "us-ft": "us-survey-foot",
        "us-survey-foot": "us-survey-foot",
    }
    unit = aliases.get(str(value.get("lengthUnit", "")).strip().casefold())
    up_axis = str(value.get("upAxis", "")).strip().upper()
    reference = str(value.get("reference", "")).strip()
    errors: list[str] = []
    if unit is None:
        errors.append("length unit must be metre, foot, or us-survey-foot")
    if up_axis != "Z":
        errors.append("the current indoor geometry adapter supports only explicit Z-up input")
    if not reference:
        errors.append("coordinate reference must identify local or CRS semantics")
    if errors:
        return None, errors
    reference_type = "crs" if reference.casefold().startswith(("epsg:", "urn:ogc:def:crs:")) else "local"
    unit_to_metre = {
        "metre": 1.0,
        "foot": 0.3048,
        "us-survey-foot": 1200.0 / 3937.0,
    }[unit]
    return {
        "lengthUnit": unit,
        "unitToMetre": unit_to_metre,
        "upAxis": up_axis,
        "reference": reference,
        "referenceType": reference_type,
    }, []


def _within(root: Path, path: Path) -> bool:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _resolve_image(root: Path, pose_path: Path, relative: str) -> Path | None:
    normalized = relative.replace("\\", "/").strip()
    if not normalized or Path(normalized).is_absolute():
        return None
    candidates = [
        pose_path.parent / normalized,
        pose_path.parent / "undistort" / normalized,
        root / normalized,
        root / "undistort" / normalized,
    ]
    for candidate in candidates:
        if _within(root, candidate) and candidate.is_file():
            return candidate.resolve()
    return None


def _camera_model_valid(value: dict[str, Any]) -> bool:
    model = value.get("undistort_camera_model") or value.get("camera_model")
    if not isinstance(model, dict):
        model = value
    intrinsic = model.get("intrinsic")
    if isinstance(intrinsic, list):
        try:
            matrix = np.asarray(intrinsic, dtype=np.float64)
            width = float(model["width"])
            height = float(model["height"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            matrix.shape == (3, 3)
            and np.isfinite(matrix).all()
            and matrix[0, 0] > 0
            and matrix[1, 1] > 0
            and width > 0
            and height > 0
            and np.allclose(matrix[2], np.asarray([0.0, 0.0, 1.0]), atol=1e-9)
        )
    required = ("width", "height", "fl_x", "fl_y", "cx", "cy")
    try:
        numbers = [float(model[name]) for name in required]
    except (KeyError, TypeError, ValueError):
        return False
    return all(math.isfinite(number) and number > 0 for number in numbers[:4]) and all(
        math.isfinite(number) for number in numbers[4:]
    )


def _rigid_c2w(matrix_value: object) -> tuple[bool, list[float] | None]:
    try:
        matrix = np.asarray(matrix_value, dtype=np.float64)
    except (TypeError, ValueError):
        return False, None
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        return False, None
    if not np.allclose(matrix[3], np.asarray([0.0, 0.0, 0.0, 1.0]), atol=1e-6):
        return False, None
    rotation = matrix[:3, :3]
    determinant = float(np.linalg.det(rotation))
    if determinant < 0.999 or determinant > 1.001:
        return False, None
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-3):
        return False, None
    return True, [float(value) for value in matrix[:3, 3]]


def validate_pose_sources(capture_root: Path, entries: list[dict[str, object]]) -> dict[str, Any]:
    root = capture_root.resolve()
    registry, _ = adapter_registry()
    transforms_names = {
        name.casefold()
        for item in registry["poseAdapters"]
        if item["id"] == "transforms-json-c2w-opengl-v1"
        for name in item["fileNames"]
    }
    transforms_entries = [item for item in entries if Path(str(item["path"])).name.casefold() in transforms_names]
    base = {
        "schemaVersion": "1.0",
        "artifactType": "pose-validation",
        "sourceSetDigest": canonical_hash(
            sorted(str(item.get("contentSha256", "")) for item in entries if item.get("contentSha256"))
        ),
        "sources": [],
        "frames": [],
        "errors": [],
    }
    if not transforms_entries:
        return {
            **base,
            "status": "NOT_AVAILABLE",
            "checks": {
                "format": "NOT_AVAILABLE",
                "coordinateConvention": "NOT_AVAILABLE",
                "imageBindings": "NOT_AVAILABLE",
                "pointCloudAlignment": "NOT_AVAILABLE",
            },
        }

    format_ok = True
    coordinate_ok = True
    bindings_ok = True
    for entry in transforms_entries:
        pose_path = root / str(entry["path"])
        source_record = {
            "adapterId": "transforms-json-c2w-opengl-v1",
            "path": str(entry["path"]),
            "contentSha256": entry.get("contentSha256"),
        }
        base["sources"].append(source_record)
        try:
            value = json.loads(pose_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            format_ok = False
            base["errors"].append(f"POSE_FORMAT_INVALID:{entry['path']}:{type(error).__name__}")
            continue
        frames = value.get("frames")
        if not isinstance(frames, list) or not frames or not _camera_model_valid(value):
            format_ok = False
            base["errors"].append(f"POSE_FORMAT_INVALID:{entry['path']}:camera-or-frames")
            continue
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict) or not isinstance(frame.get("file_path"), str):
                format_ok = False
                base["errors"].append(f"POSE_FRAME_INVALID:{entry['path']}:{index}")
                continue
            rigid, center = _rigid_c2w(frame.get("transform_matrix"))
            if not rigid or center is None:
                coordinate_ok = False
                base["errors"].append(f"POSE_COORDINATE_CONVENTION_INVALID:{entry['path']}:{index}")
                continue
            image = _resolve_image(root, pose_path, str(frame["file_path"]))
            if image is None:
                bindings_ok = False
                image_relative = None
                image_sha = None
                base["errors"].append(f"POSE_IMAGE_MISSING:{entry['path']}:{frame['file_path']}")
            else:
                image_relative = image.relative_to(root).as_posix()
                image_sha = sha256_file(image)
            base["frames"].append(
                {
                    "frameId": f"{Path(str(entry['path'])).as_posix()}#{index}",
                    "sourcePath": str(entry["path"]),
                    "imagePath": image_relative,
                    "imageContentSha256": image_sha,
                    "center": center,
                    "matrixDirection": "camera-to-world",
                    "cameraConvention": "OpenGL",
                }
            )
    checks = {
        "format": "PASS" if format_ok else "FAIL",
        "coordinateConvention": "PASS" if coordinate_ok and base["frames"] else "FAIL",
        "imageBindings": "PASS" if bindings_ok and base["frames"] else "FAIL",
        "pointCloudAlignment": "NOT_RUN",
    }
    return {
        **base,
        "status": "ALIGNMENT_REQUIRED" if all(value == "PASS" for value in checks.values() if value != "NOT_RUN") else "FAIL",
        "checks": checks,
    }


def validate_pose_alignment(manifest: dict[str, Any], index: Any) -> dict[str, Any]:
    pose = manifest.get("poseValidation")
    if not isinstance(pose, dict) or pose.get("status") not in {"ALIGNMENT_REQUIRED", "PASS"}:
        source_set_digest = manifest.get("sourceSetDigest")
        if not isinstance(source_set_digest, str) or len(source_set_digest) != 64:
            source_set_digest = canonical_hash([])
        return {
            "schemaVersion": "1.0",
            "artifactType": "pose-validation",
            "status": "FAIL",
            "sourceSetDigest": source_set_digest,
            "checks": {
                "format": "FAIL",
                "coordinateConvention": "FAIL",
                "imageBindings": "FAIL",
                "pointCloudAlignment": "NOT_RUN",
            },
            "frames": [],
            "errors": ["POSE_DISCOVERY_NOT_READY"],
        }
    frames = pose.get("frames", [])
    bounds = index.manifest["bounds"]
    width = float(bounds["maxX"]) - float(bounds["minX"])
    depth = float(bounds["maxY"]) - float(bounds["minY"])
    height = float(bounds["maxZ"]) - float(bounds["minZ"])
    plan_margin = max(2.0, 0.05 * math.hypot(width, depth))
    z_margin = max(2.0, 0.25 * height)
    radius = max(1.0, min(5.0, 0.10 * max(width, depth)))
    aligned = 0
    checked_frames: list[dict[str, Any]] = []
    for frame in frames:
        x, y, z = (float(value) for value in frame["center"])
        in_bounds = (
            float(bounds["minX"]) - plan_margin <= x <= float(bounds["maxX"]) + plan_margin
            and float(bounds["minY"]) - plan_margin <= y <= float(bounds["maxY"]) + plan_margin
            and float(bounds["minZ"]) - z_margin <= z <= float(bounds["maxZ"]) + z_margin
        )
        nearby_points = 0
        if in_bounds:
            nearby_points = index.query_bbox(x - radius, y - radius, x + radius, y + radius).point_count
        frame_aligned = in_bounds and nearby_points > 0
        aligned += int(frame_aligned)
        checked_frames.append(
            {
                **frame,
                "insideExpandedPointCloudBounds": in_bounds,
                "nearbyPointCount": nearby_points,
                "aligned": frame_aligned,
            }
        )
    required = max(1, int(math.ceil(0.80 * len(checked_frames)))) if checked_frames else 1
    alignment_pass = aligned >= required
    checks = dict(pose["checks"])
    checks["pointCloudAlignment"] = "PASS" if alignment_pass else "FAIL"
    return {
        **pose,
        "status": "PASS" if alignment_pass else "FAIL",
        "checks": checks,
        "frames": checked_frames,
        "alignedFrameCount": aligned,
        "requiredAlignedFrameCount": required,
        "indexFingerprint": index.manifest["indexFingerprint"],
        "inputs": [
            {"artifactType": "capture-manifest", "payloadDigest": manifest.get("captureFingerprint")},
            {"artifactType": "capture-index", "payloadDigest": index.manifest["indexFingerprint"]},
        ],
    }


def _frame_lookup_by_id(capture_root: Path, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Map pose-validation frameIds to projection frames (lazy import)."""
    import photo_projection

    lookup: dict[str, Any] = {}
    for source in sources:
        relative = str(source.get("path", ""))
        transforms_path = capture_root / relative
        if not transforms_path.is_file():
            continue
        try:
            frames = photo_projection.load_frames(transforms_path, image_root=capture_root)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        for index, frame in enumerate(frames):
            lookup[f"{Path(relative).as_posix()}#{index}"] = frame
    return lookup


def _reprojection_score(
    frame: Any,
    xyz: np.ndarray,
    gray: np.ndarray,
    *,
    block_px: int,
) -> tuple[float | None, int]:
    """Color-consistency score for one frame.

    Projects the LAS sample into the frame, keeps the nearest point per
    ``block_px`` pixel block (a coarse z-buffer that suppresses see-through
    leakage), and correlates the block's LAS gray value with the photo pixel
    under it.  Returns ``(score, block_count)``; score is the Pearson
    correlation clamped to [0, 1] (a wrong pose decorrelates the two signals,
    so ~0 means "no evidence this pose looks at this cloud").
    """
    import photo_projection
    from PIL import Image

    uv, depth, in_front = photo_projection.project_points(frame, xyz)
    visible = in_front & photo_projection.in_image(frame, uv)
    count = int(visible.sum())
    if count < 32:
        return None, 0
    u = uv[visible, 0]
    v = uv[visible, 1]
    d = depth[visible]
    g = gray[visible]
    columns = frame.camera.width // block_px + 2
    key = (v // block_px).astype(np.int64) * columns + (u // block_px).astype(np.int64)
    order = np.argsort(d, kind="stable")
    ordered_key = key[order]
    first = np.unique(ordered_key, return_index=True)[1]
    selected = order[first]
    if len(selected) < 8:
        return None, len(selected)
    image = np.asarray(Image.open(frame.image_path).convert("L"), dtype=np.float64)
    px = np.clip(u[selected].astype(np.int64), 0, frame.camera.width - 1)
    py = np.clip(v[selected].astype(np.int64), 0, frame.camera.height - 1)
    photo_gray = image[py, px]
    las_gray = g[selected]
    if float(np.std(photo_gray)) < 1e-9 or float(np.std(las_gray)) < 1e-9:
        return 0.0, len(selected)
    correlation = float(np.corrcoef(photo_gray, las_gray)[0, 1])
    if not math.isfinite(correlation):
        return 0.0, len(selected)
    return max(0.0, correlation), len(selected)


def validate_pose_reprojection(
    manifest: dict[str, Any],
    index: Any,
    *,
    capture_root: Path | None = None,
    block_px: int = 16,
    min_blocks: int = 120,
    min_score: float = 0.2,
    min_usable_cap: int = 20,
    min_usable_fraction: float = 0.30,
    max_sample_points: int = 400_000,
) -> dict[str, Any]:
    """Per-frame quantified pose gate (pose-validation-v1 compatible).

    Upgrade over :func:`validate_pose_alignment`: instead of the all-or-
    nothing "80% of camera centres sit inside the expanded bounds" rule, every
    frame gets a quantified alignment score (projected LAS color vs photo
    pixel color, block-median z-buffered; see :func:`_reprojection_score`) and
    frames pass or fail **individually**.  The artifact keeps every field the
    v1 schema had (status/checks/frames/...) and adds ``usableFrames`` /
    ``rejectedFrames`` so downstream stages can consume good frames from a
    batch that contains a stretch of bad poses.  Overall PASS requires
    ``usable >= min(min_usable_cap, ceil(min_usable_fraction * total))``.

    The coarse bounds check stays as a fast-path prefilter: frames it rejects
    are marked ``REJECTED_COARSE`` without any image IO.
    """
    coarse = validate_pose_alignment(manifest, index)
    reprojection_meta: dict[str, Any] = {
        "blockPx": block_px,
        "minBlocks": min_blocks,
        "minScore": min_score,
        "metric": "block-median z-buffered LAS-gray vs photo-gray Pearson correlation, clamped to [0,1]",
    }
    if not coarse.get("frames"):
        return {
            **coarse,
            "reprojection": reprojection_meta,
            "usableFrames": [],
            "rejectedFrames": [
                {"frameId": frame.get("frameId"), "reason": "REJECTED_COARSE"}
                for frame in coarse.get("frames", [])
            ],
            "usableFrameCount": 0,
            "requiredUsableFrameCount": 1,
        }

    root = Path(capture_root) if capture_root is not None else Path(str(manifest.get("captureRoot", "")))
    pose = manifest.get("poseValidation") or {}
    lookup = _frame_lookup_by_id(root, pose.get("sources", []))

    every = max(1, int(index.manifest["indexedPointCount"]) // max_sample_points)
    sample = index.query_all(every=every)
    xyz = np.stack([sample.x, sample.y, sample.z], axis=1)
    has_color = bool(index.manifest.get("hasColor")) and sample.point_count > 0
    gray = sample.rgb.astype(np.float64).mean(axis=1) if has_color else np.zeros(len(xyz))

    frames_out: list[dict[str, Any]] = []
    usable: list[str] = []
    rejected: list[dict[str, Any]] = []
    for frame_record in coarse["frames"]:
        frame_id = str(frame_record.get("frameId"))
        record = dict(frame_record)
        projection_frame = lookup.get(frame_id)
        score: float | None = None
        blocks = 0
        if not frame_record.get("aligned"):
            status = "REJECTED_COARSE"
        elif projection_frame is None or projection_frame.image_path is None:
            status = "REJECTED_NO_IMAGE"
        elif not has_color:
            # Without color the quantified metric is undefined; fall back to
            # the coarse geometric result rather than silently passing.
            status = "USABLE_GEOMETRY_ONLY"
        else:
            score, blocks = _reprojection_score(
                projection_frame, xyz, gray, block_px=block_px
            )
            if score is None or blocks < min_blocks:
                status = "REJECTED_LOW_OVERLAP"
            elif score >= min_score:
                status = "USABLE"
            else:
                status = "REJECTED_LOW_SCORE"
        record["reprojectionScore"] = None if score is None else round(score, 4)
        record["reprojectionBlocks"] = blocks
        record["status"] = status
        frames_out.append(record)
        if status.startswith("USABLE"):
            usable.append(frame_id)
        else:
            rejected.append({"frameId": frame_id, "reason": status})

    total = len(frames_out)
    required = max(1, min(min_usable_cap, int(math.ceil(min_usable_fraction * total))))
    overall_pass = len(usable) >= required
    checks = dict(coarse["checks"])
    checks["reprojectionPerFrame"] = "PASS" if overall_pass else "FAIL"
    return {
        **coarse,
        "status": "PASS" if overall_pass else "FAIL",
        "checks": checks,
        "frames": frames_out,
        "reprojection": {
            **reprojection_meta,
            "sampleEvery": every,
            "samplePointCount": int(len(xyz)),
            "hasColor": has_color,
        },
        "usableFrames": usable,
        "rejectedFrames": rejected,
        "usableFrameCount": len(usable),
        "requiredUsableFrameCount": required,
    }


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("adapters", help="print the checked-in adapter registry")
    pose = subparsers.add_parser("validate-pose", help="run pose format, binding and point-cloud alignment gates")
    pose.add_argument("--manifest", required=True, type=Path)
    pose.add_argument("--index", required=True, type=Path)
    pose.add_argument("--output", required=True, type=Path)
    reproj = subparsers.add_parser(
        "validate-pose-reprojection",
        help="per-frame quantified pose gate: coarse prefilter + LAS-vs-photo color consistency",
    )
    reproj.add_argument("--manifest", required=True, type=Path)
    reproj.add_argument("--index", required=True, type=Path)
    reproj.add_argument("--output", required=True, type=Path)
    reproj.add_argument("--capture-root", type=Path, default=None)
    reproj.add_argument("--min-score", type=float, default=0.2)
    reproj.add_argument("--block-px", type=int, default=16)
    args = parser.parse_args()

    if args.command == "adapters":
        registry, digest = adapter_registry()
        print(json.dumps({"ok": True, "registryDigest": digest, "registry": registry}, ensure_ascii=False, indent=2))
        return 0

    from capture_index import CaptureIndex

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    index = CaptureIndex.open(args.index, validate_source=True)
    index.validate_tiles()
    if args.command == "validate-pose-reprojection":
        report = validate_pose_reprojection(
            manifest,
            index,
            capture_root=args.capture_root,
            min_score=args.min_score,
            block_px=args.block_px,
        )
    else:
        report = validate_pose_alignment(manifest, index)
    write_json_atomic(args.output, report)
    print(json.dumps({"ok": report["status"] == "PASS", "status": report["status"], "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
