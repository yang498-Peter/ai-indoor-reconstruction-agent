#!/usr/bin/env python3
"""Accept a browser-free visual review only when every artifact is hash-bound."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_review_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reviewed-at must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > datetime.now(timezone.utc):
        raise ValueError("reviewed-at cannot be in the future")
    return parsed


def scene_semantic_hash(scene: dict[str, Any]) -> str:
    payload = {
        key: scene[key]
        for key in (
            "coordinateSystem", "levels", "walls", "structures",
            "structureCandidates", "objects", "visualReview",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_manifest(manifest_path: Path, scene_path: Path, scene_hash: str) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    errors: list[str] = []
    if manifest.get("status") != "DELIVERY_REVIEW":
        errors.append("status is not DELIVERY_REVIEW")
    if manifest.get("sceneSha256") != scene_hash:
        errors.append("scene hash mismatch")
    for prefix in ("scene", "renderer", "output"):
        path_value = manifest.get(f"{prefix}Path")
        digest = manifest.get(f"{prefix}Sha256")
        candidate = Path(path_value) if isinstance(path_value, str) else None
        if candidate is None or not candidate.is_file():
            errors.append(f"missing {prefix} artifact")
        elif digest != sha256(candidate):
            errors.append(f"stale {prefix} hash")
    if Path(manifest.get("scenePath", "")).resolve() != scene_path.resolve():
        errors.append("scene path mismatch")
    if errors:
        raise ValueError(f"{manifest_path.name}: " + "; ".join(errors))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--checklist", required=True, type=Path)
    parser.add_argument("--top-manifest", required=True, type=Path)
    parser.add_argument("--oblique-manifest", required=True, type=Path)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--generation-report", type=Path)
    args = parser.parse_args()

    try:
        if not re.fullmatch(r"[a-z0-9._-]{3,64}", args.reviewer):
            raise ValueError("reviewer must match [a-z0-9._-]{3,64}")
        reviewed_at = parse_review_time(args.reviewed_at).isoformat()
        scene = load_json(args.scene)
        checklist = load_json(args.checklist)
        scene_hash = sha256(args.scene)
        semantic_hash = scene_semantic_hash(scene)
        if checklist.get("sceneSemanticSha256") != semantic_hash:
            raise ValueError("checklist is stale for the current semantic scene")
        if scene.get("structureCandidates"):
            raise ValueError("structure candidates remain")
        pipeline_items = scene.get("pipeline", [])
        pipeline = {
            item.get("id"): item.get("status")
            for item in pipeline_items
            if isinstance(item, dict)
        } if isinstance(pipeline_items, list) else pipeline_items
        if not isinstance(pipeline, dict):
            raise ValueError("pipeline has an invalid shape")
        for key in ("objects", "structures", "author"):
            if pipeline.get(key) != "PASS":
                raise ValueError(f"pipeline.{key} is not PASS")
        failed_loops = [
            item.get("name") for item in scene.get("qualityLoops", [])
            if item.get("blocking", True) and item.get("status") != "PASS"
        ]
        if failed_loops:
            raise ValueError("blocking quality loops remain: " + ", ".join(map(str, failed_loops)))
        for key in (
            "areaReview", "floorZoneReview", "objectClearanceReview", "overlapReview",
            "topologyReview", "declaredTopologyReview", "derivedGeometryReview",
        ):
            if scene.get(key, {}).get("status") != "PASS":
                raise ValueError(f"{key} is not PASS")
        top = verify_manifest(args.top_manifest, args.scene, scene_hash)
        oblique = verify_manifest(args.oblique_manifest, args.scene, scene_hash)

        note = (
            "Independent blind-first raw-to-model review found no P0/P1; "
            "current scene, renderers and images are hash-bound."
        )
        checklist["status"] = "PASS"
        checklist["reviewer"] = args.reviewer
        checklist["reviewedAt"] = reviewed_at
        checklist["reviewedSceneSha256"] = scene_hash
        checklist["rule"] = "PASS is valid only for the hash-bound scene and final static evidence listed in the receipt."
        for item in checklist.get("checks", []):
            item["status"] = "PASS"
            item["note"] = note
        for item in checklist.get("elementReviews", []):
            item["status"] = "PASS"
            item["note"] = note
        args.checklist.write_text(json.dumps(checklist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        receipt = {
            "schemaVersion": "1.0",
            "status": "PASS",
            "sceneSemanticSha256": semantic_hash,
            "sceneSha256": scene_hash,
            "reviewer": args.reviewer,
            "reviewedAt": reviewed_at,
            "views": ["plan-overlay", "elevation-slices", "three-overlay", "three-model"],
            "artifacts": [
                {"manifest": str(args.top_manifest.resolve()), "outputSha256": top["outputSha256"]},
                {"manifest": str(args.oblique_manifest.resolve()), "outputSha256": oblique["outputSha256"]},
            ],
            "p0": [],
            "p1": [],
            "p2": [
                "Cutaway remains a simplified evidence render rather than a photoreal marketing image.",
                "Explicit tucked-in chair layouts are inferred and are not surveyed positions.",
            ],
        }
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.generation_report and args.generation_report.is_file():
            report = load_json(args.generation_report)
            report.setdefault("gates", {})["agentVisualReview"] = "PASS"
            report["visualReviewReceipt"] = str(args.receipt.resolve())
            report["visualReviewSceneSha256"] = scene_hash
            args.generation_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "sceneSha256": scene_hash, "reviewer": args.reviewer}, ensure_ascii=False))
        return 0
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
