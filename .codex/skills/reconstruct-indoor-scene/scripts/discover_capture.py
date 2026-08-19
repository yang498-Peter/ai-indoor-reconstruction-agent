#!/usr/bin/env python3
"""Create a read-only, capture-unit-aware reconstruction manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[4]
CORE_ROOT = REPO_ROOT / "scene-core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from capture_readiness import (  # noqa: E402
    adapter_registry,
    canonical_hash,
    normalize_coordinate_frame,
    point_cloud_adapter,
    sha256_file,
    validate_pose_alignment,
    validate_pose_sources,
    write_json_atomic,
)


POINT_CLOUD = {".las", ".laz", ".e57", ".ply", ".pcd", ".xyz"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
POSE_HINTS = (
    "pose",
    "trajectory",
    "camera",
    "transform",
    "odom",
    "pano",
    "calib",
    "intrinsic",
    "extrinsic",
)
GROUP_NAMES = ("pointClouds", "images", "posesAndTransforms", "other")


def classify(path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in POINT_CLOUD:
        return "pointClouds"
    if suffix in IMAGES:
        return "images"
    if suffix in {".csv", ".json", ".yaml", ".yml", ".txt"} and any(
        hint in name for hint in POSE_HINTS
    ):
        return "posesAndTransforms"
    return "other"


def _entry(root: Path, path: Path) -> dict[str, object]:
    stat = path.stat()
    entry: dict[str, object] = {
        "path": path.relative_to(root).as_posix(),
        "bytes": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
        "extension": path.suffix.lower(),
    }
    if classify(path) != "other":
        entry["contentSha256"] = sha256_file(path)
        entry["lineageId"] = f"source:{entry['contentSha256']}"
    return entry


def _capture_key(root: Path, point_cloud: Path) -> str:
    """Treat each point-cloud directory as a capture boundary; never cross siblings."""
    relative_parent = point_cloud.parent.relative_to(root)
    return "." if relative_parent == Path(".") else relative_parent.as_posix()


def _fingerprint(root: Path, entries: list[dict[str, object]]) -> str:
    del root
    reconstruction_sources = [
        {
            "path": item["path"],
            "bytes": item["bytes"],
            "contentSha256": item["contentSha256"],
        }
        for item in entries
        if isinstance(item.get("contentSha256"), str)
    ]
    return canonical_hash(sorted(reconstruction_sources, key=lambda value: str(value["path"])))


def _select_primary_cloud(
    clouds: list[dict[str, object]], selected_relative_path: str | None = None
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    if selected_relative_path:
        selected = [item for item in clouds if str(item["path"]) == selected_relative_path]
        if len(selected) != 1:
            raise ValueError(f"selected point cloud is not unique in this capture: {selected_relative_path}")
        return selected[0], [item for item in clouds if item is not selected[0]]
    if len(clouds) == 1:
        return clouds[0], []
    preferred = [
        item for item in clouds
        if "colorized" in Path(str(item["path"])).stem.casefold()
        and not any(marker in Path(str(item["path"])).stem.casefold() for marker in ("realtime", "preview", "sample"))
    ]
    if len(preferred) == 1:
        return preferred[0], [item for item in clouds if item is not preferred[0]]
    return None, clouds


def _summarize(groups: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {
        "counts": {name: len(groups[name]) for name in GROUP_NAMES},
        "bytes": {
            name: sum(int(item["bytes"]) for item in groups[name])
            for name in GROUP_NAMES
        },
    }


def build_manifest(
    root: Path,
    selected_point_cloud: str | None = None,
    *,
    coordinate_frame: dict[str, object] | None = None,
) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"capture directory does not exist: {root}")

    paths = sorted(path for path in root.rglob("*") if path.is_file())
    entries_by_path = {path: _entry(root, path) for path in paths}
    groups: dict[str, list[dict[str, object]]] = {name: [] for name in GROUP_NAMES}
    for path in paths:
        groups[classify(path)].append(entries_by_path[path])

    point_clouds = [path for path in paths if classify(path) == "pointClouds"]
    unit_keys = sorted({_capture_key(root, path) for path in point_clouds})
    units: list[dict[str, object]] = []
    for key in unit_keys:
        unit_root = root if key == "." else root / key
        unit_clouds = [path for path in point_clouds if _capture_key(root, path) == key]

        # Supporting evidence may span sibling folders only when the root has exactly
        # one point-cloud unit. With multiple units, containment is the safe boundary.
        support_paths = paths if len(unit_keys) == 1 else [
            path for path in paths if path == unit_root or unit_root in path.parents
        ]
        unit_groups: dict[str, list[dict[str, object]]] = {name: [] for name in GROUP_NAMES}
        unit_cloud_set = set(unit_clouds)
        for path in support_paths:
            group = classify(path)
            if group == "pointClouds" and path not in unit_cloud_set:
                continue
            unit_groups[group].append(entries_by_path[path])

        ordered_clouds = sorted(
            unit_groups["pointClouds"], key=lambda item: int(item["bytes"]), reverse=True
        )
        selected_for_unit = selected_point_cloud if len(unit_keys) == 1 else None
        primary_cloud, auxiliary_clouds = _select_primary_cloud(ordered_clouds, selected_for_unit)
        adapter = point_cloud_adapter(root / str(primary_cloud["path"])) if primary_cloud else None
        warnings: list[str] = []
        if primary_cloud is None:
            warnings.append("Multiple plausible point clouds remain; select one explicitly before modeling.")
        elif adapter and adapter["status"] != "SUPPORTED":
            warnings.append(
                f"Selected point cloud has no usable adapter: {adapter['id']} ({adapter['status']})."
            )
        if not unit_groups["images"]:
            warnings.append("No images: geometry work may continue, but material acceptance is blocked.")
        if not unit_groups["posesAndTransforms"]:
            warnings.append("No pose/transform evidence: posed-photo association is blocked.")
        units.append(
            {
                "unitId": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                "relativeRoot": key,
                "supportScope": "." if len(unit_keys) == 1 else key,
                **_summarize(unit_groups),
                "primaryPointCloud": primary_cloud,
                "auxiliaryPointClouds": auxiliary_clouds,
                "pointCloudSelection": "UNAMBIGUOUS" if primary_cloud else "AMBIGUOUS",
                "pointCloudAdapter": adapter,
                "files": unit_groups,
                "warnings": warnings,
            }
        )

    normalized_frame, coordinate_errors = normalize_coordinate_frame(coordinate_frame)
    pose_entries = groups["posesAndTransforms"]
    pose_validation = validate_pose_sources(root, pose_entries)
    warnings: list[str] = []
    if not point_clouds:
        state = "BLOCKED_NO_POINT_CLOUD"
        warnings.append("No recognized point-cloud file was found.")
    elif len(units) > 1:
        state = "BLOCKED_MULTI_CAPTURE_ROOT"
        warnings.append(
            "Multiple capture units were found. Select one unit root; no cloud/photo/pose pairing was recommended."
        )
    elif units[0]["primaryPointCloud"] is None:
        state = "BLOCKED_AMBIGUOUS_CLOUD"
        warnings.append("The selected capture unit contains multiple plausible primary point clouds.")
    elif units[0]["pointCloudAdapter"]["status"] != "SUPPORTED":  # type: ignore[index]
        state = "BLOCKED_UNSUPPORTED_POINT_CLOUD"
        warnings.append("The selected point cloud is discoverable but has no validated local parser adapter.")
    elif normalized_frame is None:
        state = "BLOCKED_COORDINATE_FRAME_REQUIRED"
        warnings.extend(coordinate_errors)
    else:
        state = "READY_GEOMETRY_ONLY"
        warnings.extend(units[0]["warnings"])  # type: ignore[arg-type]
        if pose_validation["status"] == "ALIGNMENT_REQUIRED":
            warnings.append("Pose format and image bindings pass; point-cloud alignment remains required.")
        elif pose_validation["status"] == "FAIL":
            warnings.append("Pose evidence failed format, coordinate-convention, or image-binding validation.")

    all_entries = [entries_by_path[path] for path in paths]
    if len(units) == 1:
        selected_counts = units[0]["counts"]
        blocked_capabilities = [
            capability
            for capability, blocked in (
                ("material-acceptance", int(selected_counts["images"]) == 0),  # type: ignore[index]
                (
                    "posed-photo-association",
                    pose_validation.get("status") != "PASS",
                ),
                ("coordinate-frame", normalized_frame is None),
                ("whole-scene-acceptance", True),
            )
            if blocked
        ]
    else:
        blocked_capabilities = [
            "capture-selection",
            "geometry-modeling",
            "material-acceptance",
            "posed-photo-association",
            "whole-scene-acceptance",
        ]

    manifest: dict[str, object] = {
        "schemaVersion": 3,
        "captureRoot": str(root),
        "captureFingerprint": _fingerprint(root, all_entries),
        "sourceSetDigest": _fingerprint(root, all_entries),
        "adapterRegistryDigest": adapter_registry()[1],
        "coordinateFrame": normalized_frame,
        "readOnlyDiscovery": True,
        "state": state,
        "captureUnitCount": len(units),
        "captureUnits": units,
        "blockedCapabilities": blocked_capabilities,
        "sceneDomainGate": "MANUAL_REVIEW_REQUIRED",
        "photoPoseAssociationGate": (
            "BLOCKED_ALIGNMENT_NOT_RUN"
            if pose_validation["status"] == "ALIGNMENT_REQUIRED"
            else "BLOCKED_POSE_VALIDATION_FAILED"
            if pose_validation["status"] == "FAIL"
            else "BLOCKED_NO_POSES"
        ),
        "poseValidation": pose_validation,
        **_summarize(groups),
        "warnings": warnings,
    }
    if (
        len(units) == 1
        and units[0]["primaryPointCloud"] is not None
        and units[0]["pointCloudAdapter"]["status"] == "SUPPORTED"  # type: ignore[index]
    ):
        manifest["recommended"] = {
            "pointCloud": units[0]["primaryPointCloud"],
            "images": units[0]["files"]["images"],  # type: ignore[index]
            "posesAndTransforms": units[0]["files"]["posesAndTransforms"],  # type: ignore[index]
        }
    else:
        manifest["recommended"] = None
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--point-cloud", help="Exact capture-relative point-cloud path when selection is ambiguous")
    parser.add_argument("--length-unit", choices=("metre", "foot", "us-survey-foot"))
    parser.add_argument("--up-axis", choices=("Z",))
    parser.add_argument("--coordinate-reference", help="Explicit local-frame name or CRS identifier")
    args = parser.parse_args()

    try:
        coordinate_frame = None
        if args.length_unit or args.up_axis or args.coordinate_reference:
            coordinate_frame = {
                "lengthUnit": args.length_unit,
                "upAxis": args.up_axis,
                "reference": args.coordinate_reference,
            }
        manifest = build_manifest(args.data, args.point_cloud, coordinate_frame=coordinate_frame)
    except ValueError as error:
        parser.error(str(error))
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.resolve()
        capture_root = Path(str(manifest["captureRoot"]))
        if output == capture_root or capture_root in output.parents:
            parser.error("output must be outside the read-only capture directory")
        write_json_atomic(args.output, manifest)
    else:
        print(payload, end="")
    return 2 if str(manifest["state"]).startswith("BLOCKED_") else 0


if __name__ == "__main__":
    raise SystemExit(main())
