#!/usr/bin/env python3
"""Fail-closed acceptance check for the current generated scene and visual receipt."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", type=Path, default=Path(__file__).with_name("generated"))
    parser.add_argument("--area-plan", type=Path, default=Path(__file__).with_name("area-review-plan.json"))
    parser.add_argument("--visual-review", type=Path, default=Path(__file__).with_name("quality-review.json"))
    args = parser.parse_args()
    scene_path = args.generated / "scene.json"
    score_script = Path(__file__).resolve().parents[2] / ".codex" / "skills" / "reconstruct-indoor-scene" / "scripts" / "score_scene.py"
    if args.visual_review.is_file() and score_script.is_file() and scene_path.is_file():
        score_output = args.generated / "scene-score.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(score_script),
                "--scene", str(scene_path),
                "--visual-review", str(args.visual_review),
                "--output", str(score_output),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        return completed.returncode
    checklist_path = args.generated / "agent-review-checklist.json"
    receipt_path = args.generated / "agent-review-receipt.json"
    endpoint_suggestions_path = args.generated / "structure-endpoint-suggestions.json"
    blockers: list[dict] = []
    if not scene_path.is_file() or not checklist_path.is_file():
        print(json.dumps({"status": "NO_GO", "blockers": [{"id": "missing-generated-scene"}]}, ensure_ascii=False))
        return 1

    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    checklist = json.loads(checklist_path.read_text(encoding="utf-8"))
    candidates = scene.get("structureCandidates", [])
    if candidates:
        blockers.append({
            "id": "structure-candidates-remain",
            "count": len(candidates),
            "elements": [item["id"] for item in candidates],
        })
    failed_loops = [
        {"name": item["name"], "status": item["status"]}
        for item in scene.get("qualityLoops", []) if item.get("status") != "PASS"
    ]
    if failed_loops:
        blockers.append({"id": "quality-loops-not-pass", "items": failed_loops})
    topology_spaces = scene.get("topologyReview", {}).get("spaces", [])
    incomplete_spaces = [
        {
            "id": item.get("id"),
            "geometryClosure": item.get("geometryClosure"),
            "missingElementIds": item.get("missingElementIds", []),
            "unpublishedElementIds": item.get("unpublishedElementIds", []),
            "danglingEndpoints": item.get("danglingEndpoints", []),
            "regularityViolations": item.get("regularityViolations", []),
        }
        for item in topology_spaces if item.get("status") != "PASS"
    ]
    if not topology_spaces or incomplete_spaces:
        blockers.append({
            "id": "room-topology-not-pass",
            "count": len(incomplete_spaces),
            "spaces": incomplete_spaces,
        })
    if not endpoint_suggestions_path.is_file():
        blockers.append({"id": "endpoint-suggestions-missing"})
    else:
        endpoint_suggestions = json.loads(endpoint_suggestions_path.read_text(encoding="utf-8"))
        structure_by_id = {
            item["id"]: item for item in [*scene.get("structures", []), *scene.get("structureCandidates", [])]
        }
        unresolved_endpoint_suggestions = []
        for item in endpoint_suggestions.get("items", []):
            structure = structure_by_id.get(item.get("id"), {})
            decision = structure.get("decision", {})
            trusted_anchor = decision.get("trustedAnchor")
            start_gain = float(item.get("fixedStart", {}).get("scoreGain", 0))
            end_gain = float(item.get("fixedEnd", {}).get("scoreGain", 0))
            relevant_gain = start_gain if trusted_anchor == "start" else end_gain if trusted_anchor == "end" else max(start_gain, end_gain)
            if relevant_gain > 0.08 and decision.get("endpointSuggestionDisposition") not in {
                "confirmed-adjusted", "false-positive-nonwall-return"
            }:
                unresolved_endpoint_suggestions.append({
                    "id": item.get("id"),
                    "trustedAnchor": trusted_anchor,
                    "relevantGain": relevant_gain,
                    "fixedStartGain": start_gain,
                    "fixedEndGain": end_gain,
                })
        if unresolved_endpoint_suggestions:
            blockers.append({
                "id": "endpoint-suggestions-unresolved",
                "count": len(unresolved_endpoint_suggestions),
                "elements": unresolved_endpoint_suggestions,
            })
    if checklist.get("status") != "PASS":
        blockers.append({"id": "agent-checklist-not-pass", "status": checklist.get("status")})
    pending_elements = [
        item["id"] for item in checklist.get("elementReviews", []) if item.get("status") != "PASS"
    ]
    if pending_elements:
        blockers.append({"id": "element-reviews-not-pass", "count": len(pending_elements), "elements": pending_elements})

    if not args.area_plan.is_file():
        blockers.append({"id": "area-review-plan-missing"})
    else:
        area_plan = json.loads(args.area_plan.read_text(encoding="utf-8"))
        pending_regions = [
            item["id"] for item in area_plan.get("regions", []) if item.get("status") != "PASS"
        ]
        if pending_regions:
            blockers.append({"id": "area-reviews-not-pass", "count": len(pending_regions), "regions": pending_regions})
        if area_plan.get("globalOmissionSweep", {}).get("status") != "PASS":
            blockers.append({"id": "global-omission-sweep-not-pass"})

    if not receipt_path.is_file():
        blockers.append({"id": "visual-receipt-missing"})
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("sceneSemanticSha256") != checklist.get("sceneSemanticSha256"):
            blockers.append({"id": "visual-receipt-scene-hash-mismatch"})
        required_views = {"plan-overlay", "elevation-slices", "three-overlay", "three-model"}
        supplied_views = set(receipt.get("views", []))
        if not required_views.issubset(supplied_views):
            blockers.append({"id": "visual-receipt-views-missing", "missing": sorted(required_views - supplied_views)})

    status = "GO" if not blockers else "NO_GO"
    print(json.dumps({
        "status": status,
        "sceneSemanticSha256": checklist.get("sceneSemanticSha256"),
        "acceptedStructureCount": len(scene.get("structures", [])),
        "candidateStructureCount": len(candidates),
        "blockers": blockers,
    }, ensure_ascii=False, indent=2))
    return 0 if status == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
