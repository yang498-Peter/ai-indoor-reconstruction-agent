#!/usr/bin/env python3
"""Initialize a fail-closed reconstruction job without touching capture data."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from discover_capture import build_manifest
from reconstruction_loop import initialize_workflow


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--scaffold", type=Path, default=Path("prototypes/litereality-three-demo"))
    parser.add_argument("--point-cloud", help="Exact capture-relative point-cloud path when discovery is ambiguous")
    parser.add_argument(
        "--scene-domain",
        choices=("indoor", "outdoor", "mixed", "unknown"),
        default="unknown",
        help="Manual domain decision from an unannotated overview; only indoor may initialize.",
    )
    args = parser.parse_args()

    data = args.data.resolve()
    work = args.work.resolve()
    scaffold = args.scaffold.resolve()
    if not data.is_dir():
        parser.error(f"capture directory does not exist: {data}")
    if data == work or data in work.parents:
        parser.error("work directory must not be the capture directory or one of its children")
    manifest = build_manifest(data, args.point_cloud)
    discovery_state = str(manifest["state"])
    capture_fingerprint = str(manifest["captureFingerprint"])
    capture_id = capture_fingerprint[:16]
    now = datetime.now(timezone.utc).isoformat()

    if discovery_state.startswith("BLOCKED_"):
        state = discovery_state
    elif args.scene_domain == "unknown":
        state = "BLOCKED_DOMAIN_UNREVIEWED"
    elif args.scene_domain != "indoor":
        state = "BLOCKED_WRONG_DOMAIN"
    else:
        state = discovery_state
    ready = state in {"READY_FULL", "READY_GEOMETRY_ONLY"}

    work.mkdir(parents=True, exist_ok=True)
    job_path = work / "job.json"
    ledger_path = work / "resolution-ledger.json"
    receipt_path = work / "review-receipt.json"
    manifest_path = work / "capture-manifest.json"
    pipeline_path = work / "pipeline-state.json"
    if job_path.exists():
        existing = json.loads(job_path.read_text(encoding="utf-8"))
        if existing.get("captureFingerprint") != capture_fingerprint:
            parser.error("work directory belongs to a different capture fingerprint")
        if not pipeline_path.exists():
            initialize_workflow(job_path, pipeline_path)
        existing_state = str(existing.get("state", "BLOCKED_INVALID_JOB"))
        existing_ready = existing_state in {"READY_FULL", "READY_GEOMETRY_ONLY"}
        print(json.dumps({"ok": existing_ready, "job": str(job_path), "state": existing_state}, ensure_ascii=False))
        return 0 if existing_ready else 2
    if ledger_path.exists() or receipt_path.exists():
        parser.error("work directory contains an unbound ledger or receipt; use a fresh work directory")

    job = {
        "schemaVersion": 2,
        "jobId": f"indoor-{capture_id}",
        "createdAt": now,
        "captureRoot": str(data),
        "captureReadOnly": True,
        "workRoot": str(work),
        "scaffold": str(scaffold),
        "captureFingerprint": capture_fingerprint,
        "captureManifest": str(manifest_path),
        "pipelineState": str(pipeline_path),
        "sceneDomain": args.scene_domain,
        "discoveryCounts": manifest["counts"],
        "state": state,
        "blockedCapabilities": manifest["blockedCapabilities"],
        "requiredNextArtifacts": [
            "ceiling-removed-color-overview",
            "high-structure-slice",
            "furniture-xray",
            "posed-photo-index",
            "region-review-plan",
            "capability-registry",
            "issue-ledger",
        ],
    }
    ledger = {
        "schemaVersion": 1,
        "jobId": job["jobId"],
        "status": "REVIEW",
        "requireNoCandidates": True,
        "elements": [],
        "decisions": [],
        "rule": "Every provisional element ends accepted-measured, accepted-inferred, or rejected.",
    }
    receipt = {
        "sceneSha256": "",
        "reviewer": "",
        "reviewedAt": "",
        "p0": [],
        "p1": [],
        "p2": [],
        "requiredAreaIds": [],
        "areas": [],
    }
    write_json(manifest_path, manifest)
    write_json(job_path, job)
    write_json(ledger_path, ledger)
    write_json(receipt_path, receipt)
    initialize_workflow(job_path, pipeline_path)
    print(json.dumps({"ok": ready, "job": str(job_path), "state": job["state"]}, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
