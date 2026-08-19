#!/usr/bin/env python3
"""Deterministic quality report for Semantic Scene V2 authority artifacts.

This evaluator deliberately has no compatibility path for legacy V1 scenes.
Legacy input must be migrated before evaluation so publication can never obtain
a PASS by reading obsolete ``structures`` or ``qualityLoops`` fields.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


SCENE_CORE = Path(__file__).resolve().parent
if str(SCENE_CORE) not in sys.path:
    sys.path.insert(0, str(SCENE_CORE))

import scene_api as api  # noqa: E402


REPORT_SCHEMA_VERSION = "2.0"
PRODUCER_VERSION = "quality-report-v2/1"
QUALITY_CONFIG = {
    "requireResolvedEvidence": True,
    "requireDeclaredSpace": True,
    "blockingIssueSeverities": ["P0", "P1"],
}


class QualityReportError(RuntimeError):
    """The input is not eligible for Semantic Scene V2 evaluation."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_digest(value: object) -> str:
    content = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(content)


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise QualityReportError(f"ARTIFACT_NOT_READABLE:{path}") from error
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualityReportError(f"ARTIFACT_INVALID_JSON:{path}") from error
    if not isinstance(value, dict):
        raise QualityReportError(f"ARTIFACT_NOT_OBJECT:{path}")
    return value, raw


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _resolve_source(scene_path: Path, source_path: str) -> Path:
    local = (scene_path.parent / source_path).resolve()
    if local.is_file():
        return local
    return (scene_path.parent.parent / source_path).resolve()


def _evidence_file_errors(scene: dict[str, Any], scene_path: Path) -> list[str]:
    errors: list[str] = []
    for node_id, entry in sorted(scene.get("evidence", {}).items()):
        status = entry.get("status")
        if status not in {"accepted-measured", "accepted-inferred"}:
            continue
        for index, source in enumerate(entry.get("sources", [])):
            if not isinstance(source, dict):
                errors.append(f"EVIDENCE_SOURCE_INVALID:{node_id}:{index}")
                continue
            source_path = source.get("path")
            source_sha = source.get("sha256")
            if status == "accepted-measured" and (
                not isinstance(source_path, str)
                or not source_path
                or not isinstance(source_sha, str)
                or len(source_sha) != 64
            ):
                errors.append(f"MEASURED_SOURCE_NOT_HASH_BOUND:{node_id}:{index}")
                continue
            if not source_path:
                continue
            resolved = _resolve_source(scene_path, source_path)
            if not resolved.is_file():
                errors.append(f"EVIDENCE_FILE_MISSING:{node_id}:{index}")
                continue
            actual = _sha256_bytes(resolved.read_bytes())
            if source_sha != actual:
                errors.append(f"EVIDENCE_HASH_STALE:{node_id}:{index}")
    return errors


def evaluate_scene(
    scene: dict[str, Any],
    scene_path: Path,
    artifact_sha256: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    if scene.get("schemaVersion") != api.SCHEMA_VERSION:
        if "structures" in scene or "structureCandidates" in scene:
            raise QualityReportError(
                "LEGACY_SCENE_REQUIRES_MIGRATION:run scene-core/migrate_scene_v1_to_v2.py"
            )
        raise QualityReportError(
            f"SCHEMA_VERSION_MISMATCH:expected {api.SCHEMA_VERSION}, got {scene.get('schemaVersion')}"
        )

    errors: list[str] = []
    try:
        api.validate_scene(scene)
        scene_valid = True
    except api.SceneError as error:
        scene_valid = False
        errors.append(f"SCENE_V2_INVALID:{error}")

    geometry_digest = api.geometry_digest(scene)
    evidence_digest = api.evidence_set_digest(scene)
    nodes = scene.get("nodes", {})
    evidence = scene.get("evidence", {})
    unresolved = sorted(
        node_id
        for node_id, node in nodes.items()
        if node.get("type") != "level"
        and evidence.get(node_id, {}).get("status") not in {
            "accepted-measured",
            "accepted-inferred",
            "rejected",
        }
    )
    if unresolved:
        errors.append("UNRESOLVED_EVIDENCE:" + ",".join(unresolved))

    topology = scene.get("review", {}).get("topology", {})
    spaces = topology.get("spaces", []) if isinstance(topology, dict) else []
    declared_space = isinstance(spaces, list) and bool(spaces)
    if not declared_space:
        errors.append("DECLARED_SPACE_REQUIRED")

    blocking_issues = sorted(
        str(issue.get("id", "unknown"))
        for issue in scene.get("review", {}).get("issues", [])
        if isinstance(issue, dict)
        and issue.get("severity") in {"P0", "P1"}
        and issue.get("status") != "RESOLVED"
    )
    if blocking_issues:
        errors.append("BLOCKING_ISSUES:" + ",".join(blocking_issues))

    evidence_file_errors = _evidence_file_errors(scene, scene_path)
    errors.extend(evidence_file_errors)

    reviewer = review.get("reviewer")
    reviewed_at = _parse_time(review.get("reviewedAt"))
    review_identity_valid = (
        isinstance(reviewer, dict)
        and isinstance(reviewer.get("actorId"), str)
        and len(reviewer["actorId"].strip()) >= 3
        and isinstance(reviewer.get("runId"), str)
        and len(reviewer["runId"].strip()) >= 16
        and reviewer.get("role") == "reviewer"
        and isinstance(reviewer.get("provider"), str)
        and bool(reviewer["provider"].strip())
        and reviewed_at is not None
        and reviewed_at <= datetime.now(timezone.utc)
    )
    if not review_identity_valid:
        errors.append("REVIEW_IDENTITY_INVALID")

    review_binding_valid = (
        review.get("geometryDigest") == geometry_digest
        and review.get("evidenceSetDigest") == evidence_digest
        and review.get("artifactSha256") == artifact_sha256
    )
    if not review_binding_valid:
        errors.append("REVIEW_BINDING_STALE")

    no_review_findings = review.get("p0") == [] and review.get("p1") == []
    if not no_review_findings:
        errors.append("REVIEW_HAS_BLOCKING_FINDINGS")

    checks = {
        "sceneV2Valid": scene_valid,
        "unresolvedEvidenceCount": len(unresolved),
        "declaredSpaceCount": len(spaces) if isinstance(spaces, list) else 0,
        "blockingIssueCount": len(blocking_issues),
        "evidenceFilesValid": not evidence_file_errors,
        "reviewIdentityValid": review_identity_valid,
        "reviewBindingCurrent": review_binding_valid,
        "reviewHasNoP0P1": no_review_findings,
    }
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "artifactType": "quality-report-v2",
        "sceneSchemaVersion": scene.get("schemaVersion"),
        "status": "PASS" if not errors else "FAIL",
        "geometryDigest": geometry_digest,
        "artifactSha256": artifact_sha256,
        "evidenceSetDigest": evidence_digest,
        "configDigest": _canonical_digest(QUALITY_CONFIG),
        "producer": {
            "name": PRODUCER_VERSION,
            "codeSha256": _sha256_bytes(Path(__file__).read_bytes()),
        },
        "checks": checks,
        "errors": errors,
    }


def evaluate_files(scene_path: Path, review_path: Path) -> dict[str, Any]:
    scene_path = scene_path.resolve()
    review_path = review_path.resolve()
    scene, scene_raw = _load_json(scene_path)
    review, _ = _load_json(review_path)
    return evaluate_scene(scene, scene_path, _sha256_bytes(scene_raw), review)


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a Semantic Scene V2 authority artifact")
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate_files(args.scene, args.review)
        write_json_atomic(args.output.resolve(), report)
    except QualityReportError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"{report['status']} {args.output.resolve()}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
