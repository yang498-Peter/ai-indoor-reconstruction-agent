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
    "reviewScoreGate": {"minAreaScore": 85, "minTotalScore": 90},
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


def _review_area_scores(review: dict[str, Any]) -> tuple[list[tuple[str, int]], int] | None:
    """Parse per-area scores and the total score from a review receipt.

    Returns None when scores are absent or malformed; the caller fails closed
    with REVIEW_SCORE_MISSING rather than assuming an unscored PASS.
    """
    areas = review.get("areas")
    total = review.get("score")
    if (
        not isinstance(areas, list)
        or not areas
        or not isinstance(total, int)
        or isinstance(total, bool)
        or not 0 <= total <= 100
    ):
        return None
    parsed: list[tuple[str, int]] = []
    for area in areas:
        if not isinstance(area, dict):
            return None
        area_id = area.get("id") or area.get("areaId")
        score = area.get("score")
        if (
            not isinstance(area_id, str)
            or not area_id.strip()
            or not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 100
        ):
            return None
        parsed.append((area_id.strip(), score))
    return parsed, total


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
            source_sha = source.get("contentSha256")
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
            if source_sha != actual or source.get("sha256") != actual:
                errors.append(f"EVIDENCE_HASH_STALE:{node_id}:{index}")
            if not source.get("lineageId") or not source.get("rootContentSha256s") or not source.get("producer"):
                errors.append(f"EVIDENCE_PROVENANCE_INCOMPLETE:{node_id}:{index}")
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
    authority_layer_valid = scene.get("sceneLayer", "authority") == "authority"
    if not authority_layer_valid:
        errors.append("SCENE_LAYER_NOT_AUTHORITY")
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

    accepted_entries = [
        (node_id, entry)
        for node_id, entry in evidence.items()
        if entry.get("status") in {"accepted-measured", "accepted-inferred"}
    ]
    claims_current = True
    accepted_source_digests_current = True
    evidence_lineages_distinct = True
    shared_root_count = 0
    for node_id, entry in accepted_entries:
        try:
            current_claim = api.claim_payload(scene, node_id)
            if (
                entry.get("claimSnapshot") != current_claim
                or entry.get("claimHash") != api.claim_hash(scene, node_id)
            ):
                claims_current = False
        except api.SceneError:
            claims_current = False
        sources = entry.get("sources", [])
        if entry.get("acceptedSourceDigest") != api._accepted_source_digest(sources):
            accepted_source_digests_current = False
        if entry.get("status") == "accepted-inferred":
            lineages = {source.get("lineageId") for source in sources if source.get("contentSha256")}
            contents = {source.get("contentSha256") for source in sources if source.get("contentSha256")}
            if (
                len(lineages) < 2
                or len(contents) < 2
                or not api.has_two_independent_sources(sources)
            ):
                evidence_lineages_distinct = False
            # Shared roots are expected for pure point-cloud captures (every
            # derived artifact descends from one LAS); they inform the
            # reviewer but never fail the report.
            if api.sources_share_roots(sources):
                shared_root_count += 1
    if not claims_current:
        errors.append("ACCEPTED_CLAIMS_STALE")
    if not accepted_source_digests_current:
        errors.append("ACCEPTED_SOURCE_DIGESTS_STALE")
    if not evidence_lineages_distinct:
        errors.append("EVIDENCE_LINEAGES_NOT_DISTINCT")

    reviewer = review.get("reviewer")
    reviewed_at = _parse_time(review.get("reviewedAt"))
    review_identity_valid = False
    review_identity_independent = False
    try:
        normalized_reviewer = api.execution_identity_api.normalize_identity(reviewer)
        if normalized_reviewer["role"] not in api.execution_identity_api.REVIEW_ROLES:
            raise api.execution_identity_api.IdentityError("EXECUTION_ROLE_NOT_ALLOWED")
        review_identity_valid = reviewed_at is not None and reviewed_at <= datetime.now(timezone.utc)
        author_identities = []
        executions = scene.get("meta", {}).get("executions", {})
        for node_id, entry in evidence.items():
            if entry.get("status") not in {"accepted-measured", "accepted-inferred"}:
                continue
            run_id = nodes.get(node_id, {}).get("meta", {}).get("createdRunId")
            author = executions.get(run_id)
            if isinstance(author, dict):
                author_identities.append(author)
        for author in author_identities:
            api.execution_identity_api.require_independent_reviewer(
                author,
                normalized_reviewer,
                severity="P1",
                required_input_digests={artifact_sha256, geometry_digest, evidence_digest},
            )
        review_identity_independent = bool(author_identities)
    except (api.execution_identity_api.IdentityError, TypeError, ValueError):
        review_identity_valid = False
    if not review_identity_valid:
        errors.append("REVIEW_IDENTITY_INVALID")
    if not review_identity_independent:
        errors.append("REVIEW_IDENTITY_NOT_INDEPENDENT")

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

    # SKILL score promise, machine-enforced: every reviewed area scores at
    # least 85 and the receipt total is at least 90. A receipt without scores
    # cannot pass.
    gate = QUALITY_CONFIG["reviewScoreGate"]
    review_area_scores = _review_area_scores(review)
    if review_area_scores is None:
        review_min_area_score: int | None = None
        review_total_score: int | None = None
        review_score_gate = False
        errors.append("REVIEW_SCORE_MISSING")
    else:
        area_entries, review_total_score = review_area_scores
        review_min_area_score = min(score for _, score in area_entries)
        review_score_gate = (
            review_min_area_score >= gate["minAreaScore"]
            and review_total_score >= gate["minTotalScore"]
        )
        if not review_score_gate:
            errors.append("REVIEW_SCORE_BELOW_GATE:" + ",".join(
                f"{area_id}={score}"
                for area_id, score in area_entries
                if score < gate["minAreaScore"]
            ) + f";total={review_total_score}")

    checks = {
        "sceneV2Valid": scene_valid,
        "authorityLayerValid": authority_layer_valid,
        "unresolvedEvidenceCount": len(unresolved),
        "declaredSpaceCount": len(spaces) if isinstance(spaces, list) else 0,
        "blockingIssueCount": len(blocking_issues),
        "evidenceFilesValid": not evidence_file_errors,
        "claimsCurrent": claims_current,
        "acceptedSourceDigestsCurrent": accepted_source_digests_current,
        "evidenceLineagesDistinct": evidence_lineages_distinct,
        "sharedRootCount": shared_root_count,
        "reviewIdentityValid": review_identity_valid,
        "reviewIdentityIndependent": review_identity_independent,
        "reviewBindingCurrent": review_binding_valid,
        "reviewHasNoP0P1": no_review_findings,
        "reviewAreaCount": (
            len(review_area_scores[0]) if review_area_scores is not None else 0
        ),
        "reviewMinAreaScore": review_min_area_score,
        "reviewTotalScore": review_total_score,
        "reviewScoreGate": review_score_gate,
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
