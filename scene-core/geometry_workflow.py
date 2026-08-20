#!/usr/bin/env python3
"""One-command deterministic geometry preparation and evaluation workflow.

``prepare`` builds a fresh derivative workspace without editing the capture or
Semantic Scene V2.  ``evaluate`` is a separate fail-closed gate for an
authority scene.  The split keeps algorithm candidates out of accepted scene
state and makes every reusable artifact traceable to one CaptureIndex.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from capture_index import CaptureIndex, CaptureIndexError, build_index
from candidate_topology import load_profile, optimize_topology
from capture_readiness import canonical_hash, validate_pose_alignment
from indexed_pointcloud_evidence import render_overview
from pointcloud_scene_metrics import evaluate_scene
from structural_proposals import build_proposals


def _atomic_json(path: Path, value: object) -> None:
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


def _pose_gate(capture_manifest: Path | None, index: CaptureIndex) -> dict[str, Any]:
    if capture_manifest is None:
        return {
            "schemaVersion": "1.0",
            "artifactType": "pose-validation",
            "status": "NOT_AVAILABLE",
            "sourceSetDigest": canonical_hash([]),
            "checks": {
                "format": "NOT_AVAILABLE",
                "coordinateConvention": "NOT_AVAILABLE",
                "imageBindings": "NOT_AVAILABLE",
                "pointCloudAlignment": "NOT_AVAILABLE",
            },
            "frames": [],
            "errors": ["CAPTURE_MANIFEST_REQUIRED_FOR_POSE_VALIDATION"],
        }
    manifest = json.loads(capture_manifest.read_text(encoding="utf-8"))
    return validate_pose_alignment(manifest, index)


def prepare_workspace(
    source: Path,
    output: Path,
    *,
    floor_z: float,
    ceiling_z: float,
    capture_manifest: Path | None = None,
    tile_size_m: float = 8.0,
    index_every: int = 1,
    overview_cell_m: float = 0.08,
    overview_every: int = 1,
    proposal_cell_m: float = 0.05,
    proposal_max_points: int = 2_000_000,
    topology_profile: Path | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    capture_manifest = capture_manifest.resolve() if capture_manifest else None
    topology_profile = topology_profile.resolve() if topology_profile else None
    if output.exists():
        raise CaptureIndexError("geometry workspace already exists; choose a fresh derivative directory")
    if not source.is_file():
        raise CaptureIndexError(f"source point cloud does not exist: {source}")
    if ceiling_z <= floor_z:
        raise ValueError("ceiling_z must be greater than floor_z")
    if capture_manifest is None:
        source_parent = source.parent
        if output == source_parent or source_parent in output.parents:
            raise CaptureIndexError(
                "without a capture manifest, the derivative workspace must stay outside the source directory"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    final_index = output / "capture-index"
    try:
        index_root = temporary / "capture-index"
        index_manifest = build_index(
            source,
            index_root,
            tile_size_m=tile_size_m,
            every=index_every,
            capture_manifest=capture_manifest,
        )
        index = CaptureIndex.open(index_root, validate_source=True)
        index.validate_tiles()
        evidence_root = temporary / "evidence"
        evidence = render_overview(
            index,
            evidence_root,
            cell=overview_cell_m,
            ground_cell=1.0,
            every=overview_every,
        )
        evidence["index"] = str(final_index)
        evidence["lineage"] = {
            "artifactType": "overview-evidence",
            "captureIndexFingerprint": index_manifest["indexFingerprint"],
            "rootContentSha256s": [index_manifest["sourceIdentity"]["contentSha256"]],
        }
        _atomic_json(evidence_root / "evidence-manifest.json", evidence)

        wall_height = ceiling_z - floor_z
        band_max = max(0.75, min(2.4, wall_height - 0.15))
        proposals = build_proposals(
            index,
            floor_z=floor_z,
            band_min_m=min(0.5, band_max * 0.4),
            band_max_m=band_max,
            raster_cell_m=proposal_cell_m,
            max_points=proposal_max_points,
        )
        proposals["index"] = str(final_index)
        proposals["lineage"] = {
            "artifactType": "structural-proposals",
            "captureIndexFingerprint": index_manifest["indexFingerprint"],
            "rootContentSha256s": [index_manifest["sourceIdentity"]["contentSha256"]],
        }
        proposals_path = temporary / "structural-proposals.json"
        _atomic_json(proposals_path, proposals)

        profile = load_profile(topology_profile)
        topology = optimize_topology(
            proposals,
            profile=profile,
            proposals_sha256=hashlib.sha256(proposals_path.read_bytes()).hexdigest(),
        )
        _atomic_json(temporary / "candidate-topology.json", topology)

        pose_report = _pose_gate(capture_manifest, index)
        _atomic_json(temporary / "pose-validation.json", pose_report)
        pose_status = pose_report["status"]
        pose_count = len(pose_report.get("frames", []))
        workflow: dict[str, Any] = {
            "schemaVersion": 1,
            "kind": "geometry-workflow",
            "status": "READY_FOR_AGENT_REVIEW",
            "source": str(source),
            "captureManifest": str(capture_manifest) if capture_manifest else None,
            "indexFingerprint": index_manifest["indexFingerprint"],
            "parameters": {
                "floorZ": floor_z,
                "ceilingZ": ceiling_z,
                "tileSizeM": tile_size_m,
                "indexDecimation": index_every,
                "overviewCellM": overview_cell_m,
                "overviewDecimation": overview_every,
                "proposalCellM": proposal_cell_m,
                "proposalMaxPoints": proposal_max_points,
                "topologyProfileId": profile["id"],
                "topologyProfileSha256": profile["artifactSha256"],
            },
            "artifacts": {
                "captureIndex": "capture-index/capture-index.json",
                "overviewEvidence": "evidence/evidence-manifest.json",
                "structuralProposals": "structural-proposals.json",
                "candidateTopology": "candidate-topology.json",
                "poseValidation": "pose-validation.json",
                "authorityScene": "NOT_CREATED",
                "pointcloudSceneMetrics": "NOT_RUN",
            },
            "gates": {
                "sourceIdentity": "PASS",
                "captureIndex": "PASS",
                "globalEvidence": "PASS",
                "structuralProposals": "CANDIDATES_ONLY",
                "candidateTopology": topology["status"],
                "photoPoseValidation": pose_status,
                "authorityTransaction": "NOT_RUN",
                "pointcloudSceneMetrics": "NOT_RUN",
                "globalOmissionReview": "NOT_RUN",
                "visualReview": "NOT_RUN",
                "publication": "BLOCKED",
            },
            "poseArtifactCount": pose_count,
            "lineage": {
                "sourceContentSha256": index_manifest["sourceIdentity"]["contentSha256"],
                "sourceSetDigest": (
                    index_manifest.get("captureBinding", {}).get("sourceSetDigest")
                    if isinstance(index_manifest.get("captureBinding"), dict)
                    else None
                ),
                "captureIndexFingerprint": index_manifest["indexFingerprint"],
                "poseValidationDigest": canonical_hash(pose_report),
            },
            "modelPolicy": {
                "defaultGeometryAuthority": "indexed local LiDAR",
                "remoteModelApi": "NOT_USED",
                "argusOfficialWeights": "NOT_INTEGRATED_NON_COMMERCIAL_LICENSE",
                "optionalDepthRole": "residual evidence only; never silently replaces measured geometry",
            },
            "nextAction": (
                "Review observation-backed centreline alternatives and the proposed global topology; "
                "record explicit accept/reject transactions in scene-authority.json, then run evaluate."
            ),
        }
        _atomic_json(temporary / "geometry-workflow.json", workflow)
        os.replace(temporary, output)
        return workflow
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def evaluate_authority_scene(
    scene_path: Path,
    workspace: Path,
    output: Path,
    *,
    residual_p90_max_m: float = 0.08,
    support_ratio_min: float = 0.70,
    support_tolerance_m: float = 0.06,
    bin_size_m: float = 0.10,
    point_to_model_grid_size_m: float = 0.12,
    max_unexplained_run_m: float = 1.0,
) -> dict[str, Any]:
    raw_scene = scene_path.read_bytes()
    scene = json.loads(raw_scene.decode("utf-8"))
    index = CaptureIndex.open(workspace / "capture-index", validate_source=True)
    report = evaluate_scene(
        scene,
        index,
        residual_p90_max_m=residual_p90_max_m,
        support_ratio_min=support_ratio_min,
        support_tolerance_m=support_tolerance_m,
        bin_size_m=bin_size_m,
        point_to_model_grid_size_m=point_to_model_grid_size_m,
        max_unexplained_run_m=max_unexplained_run_m,
        scene_sha256=hashlib.sha256(raw_scene).hexdigest(),
    )
    report["scene"] = str(scene_path.resolve())
    report["workflow"] = str((workspace / "geometry-workflow.json").resolve())
    _atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--las", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--floor-z", required=True, type=float)
    prepare.add_argument("--ceiling-z", required=True, type=float)
    prepare.add_argument("--capture-manifest", type=Path)
    prepare.add_argument("--tile-size", type=float, default=8.0)
    prepare.add_argument("--index-every", type=int, default=1)
    prepare.add_argument("--overview-cell", type=float, default=0.08)
    prepare.add_argument("--overview-every", type=int, default=1)
    prepare.add_argument("--proposal-cell", type=float, default=0.05)
    prepare.add_argument("--proposal-max-points", type=int, default=2_000_000)
    prepare.add_argument("--topology-profile", type=Path)

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--scene", required=True, type=Path)
    evaluate.add_argument("--workspace", required=True, type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    evaluate.add_argument("--residual-p90-max", type=float, default=0.08)
    evaluate.add_argument("--support-ratio-min", type=float, default=0.70)
    evaluate.add_argument("--support-tolerance", type=float, default=0.06)
    evaluate.add_argument("--bin-size", type=float, default=0.10)
    evaluate.add_argument("--point-to-model-grid-size", type=float, default=0.12)
    evaluate.add_argument("--max-unexplained-run", type=float, default=1.0)
    args = parser.parse_args()

    if args.command == "prepare":
        result = prepare_workspace(
            args.las,
            args.output,
            floor_z=args.floor_z,
            ceiling_z=args.ceiling_z,
            capture_manifest=args.capture_manifest,
            tile_size_m=args.tile_size,
            index_every=args.index_every,
            overview_cell_m=args.overview_cell,
            overview_every=args.overview_every,
            proposal_cell_m=args.proposal_cell,
            proposal_max_points=args.proposal_max_points,
            topology_profile=args.topology_profile,
        )
        print(json.dumps({"ok": True, "workspace": str(args.output.resolve()), "result": result}, ensure_ascii=False))
        return 0

    report = evaluate_authority_scene(
        args.scene,
        args.workspace,
        args.output,
        residual_p90_max_m=args.residual_p90_max,
        support_ratio_min=args.support_ratio_min,
        support_tolerance_m=args.support_tolerance,
        bin_size_m=args.bin_size,
        point_to_model_grid_size_m=args.point_to_model_grid_size,
        max_unexplained_run_m=args.max_unexplained_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
