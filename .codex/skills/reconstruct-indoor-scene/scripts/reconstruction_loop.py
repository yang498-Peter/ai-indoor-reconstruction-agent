#!/usr/bin/env python3
"""Resumable, fail-closed orchestration for evidence-backed scene authoring."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCENE_CORE_ROOT = REPOSITORY_ROOT / "scene-core"
if str(SCENE_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(SCENE_CORE_ROOT))

import execution_identity as execution_identity_api  # noqa: E402

PIPELINE_CONTRACT_PATH = REPOSITORY_ROOT / "schemas" / "pipeline-contract-v2.json"
PIPELINE_CONTRACT = json.loads(PIPELINE_CONTRACT_PATH.read_text(encoding="utf-8"))
PIPELINE_CONTRACT_DIGEST = hashlib.sha256(PIPELINE_CONTRACT_PATH.read_bytes()).hexdigest()
STAGE_ORDER = tuple(stage["name"] for stage in PIPELINE_CONTRACT["stages"])
STAGE_SPECS = {stage["name"]: stage for stage in PIPELINE_CONTRACT["stages"]}
STAGE_REQUIREMENTS = {
    stage["name"]: tuple(stage["requiredCapabilities"])
    for stage in PIPELINE_CONTRACT["stages"]
}
STAGE_STATES = {"PENDING", "IN_PROGRESS", "REVIEW", "PASS", "BLOCKED", "FAILED"}
CAPABILITY_STATES = {"UNVERIFIED", "AVAILABLE", "DEGRADED", "BLOCKED"}
ISSUE_STATES = {"OPEN", "PATCHED", "NEEDS_RECHECK", "RESOLVED"}
EVIDENCE_ROLES = {
    "raw", "overlay", "render", "elevation", "photo", "topology", "collision", "score", "tool"
}


class WorkflowError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: str = "WORKFLOW_ERROR",
        next_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_actions = list(next_actions or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "nextActions": self.next_actions,
        }


def validate_pipeline_contract() -> None:
    if (
        PIPELINE_CONTRACT.get("schemaVersion") != "2.0"
        or PIPELINE_CONTRACT.get("artifactType") != "pipeline-contract-v2"
        or PIPELINE_CONTRACT.get("stateSchemaVersion") != 2
    ):
        raise WorkflowError(
            "pipeline contract identity is invalid",
            "PIPELINE_CONTRACT_INVALID",
        )
    if not STAGE_ORDER or len(set(STAGE_ORDER)) != len(STAGE_ORDER):
        raise WorkflowError(
            "pipeline contract stage names must be unique",
            "PIPELINE_CONTRACT_INVALID",
        )
    seen: set[str] = set()
    for stage in PIPELINE_CONTRACT.get("stages", []):
        required = {
            "name",
            "dependsOn",
            "requiredCapabilities",
            "requiredArtifacts",
            "requiredArtifactChecks",
            "sceneBinding",
            "blocksOnOpenIssues",
            "evaluator",
        }
        if (
            set(stage) != required
            or any(name not in seen for name in stage["dependsOn"])
            or set(stage["requiredArtifactChecks"]) != set(stage["requiredArtifacts"])
        ):
            raise WorkflowError(
                "pipeline stages must have exact fields and depend only on earlier stages",
                "PIPELINE_CONTRACT_INVALID",
            )
        seen.add(stage["name"])
    for change in PIPELINE_CONTRACT.get("changeInvalidation", {}).values():
        names = list(change.get("pendingStages", []))
        if change.get("reviewStage") is not None:
            names.append(change["reviewStage"])
        if any(name not in STAGE_SPECS for name in names):
            raise WorkflowError(
                "pipeline invalidation references an unknown stage",
                "PIPELINE_CONTRACT_INVALID",
            )


validate_pipeline_contract()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def actor_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) not in {"Cf", "Cc", "Cs"}
    )
    normalized = " ".join(normalized.split()).casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", normalized):
        raise WorkflowError("actor identity must be a 3-64 character ASCII id using letters, digits, dot, underscore, or hyphen")
    return normalized


def bind_execution(
    state: dict[str, Any],
    identity_path: Path | None,
    operation: str,
    roles: set[str] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    if identity_path is None:
        raise WorkflowError(
            "execution identity is required for this operation",
            "EXECUTION_IDENTITY_REQUIRED",
            ["provide --execution <execution-identity-v1.json>"],
        )
    try:
        identity = execution_identity_api.load_identity(identity_path.resolve())
        identity = execution_identity_api.require_operation(identity, operation, roles)
        digest = execution_identity_api.identity_digest(identity)
    except execution_identity_api.IdentityError as error:
        raise WorkflowError(str(error), error.code) from error
    if actor and actor_key(actor) != identity["actorId"]:
        raise WorkflowError(
            "--actor does not match the execution identity",
            "EXECUTION_ACTOR_MISMATCH",
        )
    executions = state.setdefault("executions", {})
    existing = executions.get(identity["runId"])
    if isinstance(existing, dict) and existing.get("identityDigest") != digest:
        raise WorkflowError(
            "runId is already registered to a different actor, role, or tool policy",
            "EXECUTION_RUN_ROLE_CONFLICT",
        )
    executions[identity["runId"]] = {"identity": identity, "identityDigest": digest}
    return identity


def registered_identity(state: dict[str, Any], run_id: object) -> dict[str, Any]:
    record = state.get("executions", {}).get(str(run_id or ""))
    if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
        raise WorkflowError(
            "recorded execution identity is missing",
            "EXECUTION_IDENTITY_REQUIRED",
        )
    return record["identity"]


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def inside(root: Path, path: Path) -> bool:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def command_exercises_evidence(command: list[str], evidence_paths: list[Path], cwd: Path) -> bool:
    executable = Path(command[0]).name.casefold()
    expected = {str(path.resolve()).casefold() for path in evidence_paths}

    def matches(value: str) -> bool:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return str(candidate.resolve()).casefold() in expected

    if matches(command[0]):
        return True
    if executable in {"python", "python.exe", "python3", "python3.exe", Path(sys.executable).name.casefold()}:
        return len(command) >= 2 and command[1] not in {"-c", "-m"} and matches(command[1])
    if executable in {"node", "node.exe"}:
        if len(command) >= 2 and command[1] not in {"-e", "--eval"} and matches(command[1]):
            return True
        return len(command) >= 3 and command[1] == "--check" and matches(command[2])
    if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        lowered = [part.casefold() for part in command]
        if "-file" in lowered:
            index = lowered.index("-file")
            return index + 1 < len(command) and matches(command[index + 1])
    return False


def run_probe(command: list[str], evidence_paths: list[Path], cwd: Path) -> None:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise WorkflowError("capability probeCommand must be a non-empty argv list")
    if not command_exercises_evidence(command, evidence_paths, cwd):
        raise WorkflowError("capability probe command must directly execute or syntax-check a registered evidence tool")
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if completed.returncode != 0:
        raise WorkflowError("capability probe command failed")


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@contextmanager
def state_write_lock(path: Path):
    lock_path = path.resolve().with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise WorkflowError(
            f"pipeline state is locked by another writer: {lock_path}",
            "STATE_BUSY",
            ["retry after the active state mutation finishes"],
        ) from error
    try:
        os.write(descriptor, f"pid={os.getpid()} at={now_iso()}\n".encode("utf-8"))
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact(path: Path, role: str | None = None) -> dict[str, str]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise WorkflowError(f"artifact does not exist: {resolved}")
    result = {"path": str(resolved), "sha256": sha256_file(resolved)}
    if role:
        result["role"] = role
    return result


def parse_evidence(values: list[str]) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for value in values:
        if "=" not in value:
            raise WorkflowError("evidence must use role=path")
        role, raw_path = value.split("=", 1)
        role = role.strip().lower()
        if role not in EVIDENCE_ROLES:
            raise WorkflowError(f"unsupported evidence role {role!r}")
        parsed.append(artifact(Path(raw_path), role))
    return parsed


def event(state: dict[str, Any], actor: str, action: str, details: dict[str, Any]) -> None:
    state["revision"] = int(state.get("revision", 0)) + 1
    state.setdefault("events", []).append(
        {"at": now_iso(), "actor": actor, "action": action, "details": details}
    )


def initial_capabilities(job: dict[str, Any], job_path: Path) -> dict[str, dict[str, Any]]:
    names = {
        capability
        for required in STAGE_REQUIREMENTS.values()
        for capability in required
    } | {"capture-selection", "posed-photo-association", "material-review"}
    capabilities = {
        name: {"status": "UNVERIFIED", "reason": "not yet bound to a real tool or artifact", "evidence": []}
        for name in sorted(names)
    }
    blocked = set(job.get("blockedCapabilities", []))
    capabilities["capture-selection"] = {
        "status": "AVAILABLE" if str(job.get("state", "")).startswith("READY_") else "BLOCKED",
        "reason": "capture fingerprint and unit selection are recorded in the job",
        "evidence": [artifact(job_path, "tool")],
    }
    capabilities["posed-photo-association"]["status"] = (
        "BLOCKED" if "posed-photo-association" in blocked else "UNVERIFIED"
    )
    capabilities["material-review"]["status"] = (
        "BLOCKED" if "material-acceptance" in blocked else "UNVERIFIED"
    )
    return capabilities


def blank_stage(stage_name: str) -> dict[str, Any]:
    return {
        "status": "PENDING",
        "attempt": 0,
        "updatedAt": None,
        "actor": None,
        "note": "",
        "artifacts": [],
        "sceneSha256": None,
        "evaluator": STAGE_SPECS[stage_name]["evaluator"],
        "evaluation": None,
        "executionRunId": None,
        "identityDigest": None,
        "capabilityDegradations": [],
    }


def initialize_workflow(job_path: Path, state_path: Path) -> dict[str, Any]:
    job_path = job_path.resolve()
    state_path = state_path.resolve()
    job = load_json(job_path)
    job_hash = sha256_file(job_path)
    if state_path.exists():
        existing = read_state(state_path)
        if (
            Path(str(existing.get("job", ""))).resolve() != job_path
            or existing.get("jobSha256") != job_hash
            or existing.get("jobId") != job.get("jobId")
            or existing.get("captureFingerprint") != job.get("captureFingerprint")
        ):
            raise WorkflowError("existing pipeline state is bound to a different or modified job")
        return existing
    ready = str(job.get("state", "")) in {"READY_FULL", "READY_GEOMETRY_ONLY"}
    stages = {name: blank_stage(name) for name in STAGE_ORDER}
    stages["intake"].update(
        {
            "status": "PASS" if ready else "BLOCKED",
            "attempt": 1,
            "updatedAt": now_iso(),
            "actor": "init_reconstruction_job",
            "note": str(job.get("state", "BLOCKED_INVALID_JOB")),
            "artifacts": [artifact(job_path, "tool")],
            "evaluation": {
                "evaluator": "initialize_intake",
                "evaluatorCodeSha256": sha256_file(Path(__file__)),
                "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
                "result": "PASS" if ready else "BLOCKED",
            },
        }
    )
    state = {
        "schemaVersion": 2,
        "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
        "jobId": job.get("jobId"),
        "captureFingerprint": job.get("captureFingerprint"),
        "job": str(job_path),
        "jobSha256": job_hash,
        "createdAt": now_iso(),
        "revision": 0,
        "stageOrder": list(STAGE_ORDER),
        "stages": stages,
        "capabilities": initial_capabilities(job, job_path),
        "executions": {},
        "issues": [],
        "currentSceneSha256": None,
        "lastCheckpoint": None,
        "events": [],
    }
    event(state, "init_reconstruction_job", "initialize", {"jobState": job.get("state")})
    with state_write_lock(state_path):
        if state_path.exists():
            raise WorkflowError(
                "pipeline state was created concurrently",
                "STATE_REVISION_CONFLICT",
                ["read the existing pipeline state before retrying initialization"],
            )
        write_json_atomic(state_path, state)
    return state


def read_state(path: Path) -> dict[str, Any]:
    state = load_json(path)
    if state.get("schemaVersion") == 1:
        raise WorkflowError(
            "pipeline state V1 must be migrated explicitly before use",
            "PIPELINE_STATE_MIGRATION_REQUIRED",
            [f"python {Path(__file__).name} migrate-state --state {path} --actor <migration-actor>"],
        )
    if state.get("schemaVersion") != 2 or state.get("stageOrder") != list(STAGE_ORDER):
        raise WorkflowError(
            "unsupported or invalid pipeline state",
            "PIPELINE_STATE_INVALID",
        )
    if state.get("pipelineContractDigest") != PIPELINE_CONTRACT_DIGEST:
        raise WorkflowError(
            "pipeline state was created for a different pipeline contract",
            "PIPELINE_CONTRACT_CHANGED_MIGRATION_REQUIRED",
            [f"python {Path(__file__).name} migrate-state --state {path} --actor <migration-actor>"],
        )
    job_path = Path(str(state.get("job", ""))).resolve()
    if (
        not job_path.is_file()
        or sha256_file(job_path) != state.get("jobSha256")
        or load_json(job_path).get("captureFingerprint") != state.get("captureFingerprint")
    ):
        raise WorkflowError("pipeline job binding is missing, modified, or belongs to another capture")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = path.resolve()
    expected_revision = int(state.get("revision", -1)) - 1
    with state_write_lock(path):
        current = load_json(path)
        current_revision = int(current.get("revision", -1))
        if current_revision != expected_revision:
            raise WorkflowError(
                f"pipeline state revision changed from {expected_revision} to {current_revision}",
                "STATE_REVISION_CONFLICT",
                ["reload pipeline-state.json and retry the mutation"],
            )
        write_json_atomic(path, state)


def command_migrate_state(args: argparse.Namespace) -> None:
    actor_key(args.actor)
    path = args.state.resolve()
    legacy = load_json(path)
    source_version = legacy.get("schemaVersion")
    if source_version == 2 and legacy.get("pipelineContractDigest") == PIPELINE_CONTRACT_DIGEST:
        raise WorkflowError(
            "pipeline state already uses the current V2 contract",
            "PIPELINE_STATE_ALREADY_CURRENT",
        )
    if source_version not in {1, 2}:
        raise WorkflowError(
            "only pipeline state V1 or an older V2 contract can be migrated",
            "PIPELINE_STATE_MIGRATION_UNSUPPORTED",
        )
    job_path = Path(str(legacy.get("job", ""))).resolve()
    if (
        not job_path.is_file()
        or sha256_file(job_path) != legacy.get("jobSha256")
        or load_json(job_path).get("captureFingerprint") != legacy.get("captureFingerprint")
    ):
        raise WorkflowError(
            "pipeline job binding is missing, modified, or belongs to another capture",
            "PIPELINE_JOB_BINDING_INVALID",
        )

    migrated_stages = {name: blank_stage(name) for name in STAGE_ORDER}
    source_stages = legacy.get("stages", {})
    intake = source_stages.get("intake") if isinstance(source_stages, dict) else None
    if isinstance(intake, dict) and intake.get("status") in {"PASS", "BLOCKED"}:
        migrated_stages["intake"].update(
            {
                "status": intake["status"],
                "attempt": int(intake.get("attempt", 0)),
                "updatedAt": intake.get("updatedAt"),
                "actor": intake.get("actor"),
                "note": f"migrated intake only; downstream artifacts require V2 reevaluation: {intake.get('note', '')}",
                "artifacts": list(intake.get("artifacts", [])),
                "evaluation": {
                    "evaluator": "initialize_intake",
                    "evaluatorCodeSha256": sha256_file(Path(__file__)),
                    "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
                    "result": intake["status"],
                    "migratedFromSchemaVersion": source_version,
                },
            }
        )

    migrated_issues: list[dict[str, Any]] = []
    for legacy_issue in legacy.get("issues", []):
        if not isinstance(legacy_issue, dict):
            continue
        migrated_issue = dict(legacy_issue)
        legacy_status = str(migrated_issue.get("status") or "OPEN")
        migrated_issue["legacyStatus"] = legacy_status
        migrated_issue["status"] = "OPEN"
        migrated_issue["identityRecheckRequired"] = True
        migrated_issue["migrationReason"] = "LEGACY_REVIEW_IDENTITY_RECHECK_REQUIRED"
        migrated_issue.pop("patch", None)
        migrated_issue.pop("resolution", None)
        migrated_issue.pop("resolvedAt", None)
        migrated_issue.pop("resolvedBy", None)
        migrated_issue.pop("resolvedByRunId", None)
        migrated_issues.append(migrated_issue)

    migrated = dict(legacy)
    migrated.update(
        {
            "schemaVersion": 2,
            "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
            "stageOrder": list(STAGE_ORDER),
            "stages": migrated_stages,
            "executions": {},
            "issues": migrated_issues,
        }
    )
    event(
        migrated,
        args.actor,
        "migrate-state",
        {
            "fromSchemaVersion": source_version,
            "toSchemaVersion": 2,
            "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
            "downstreamDisposition": "PENDING_REEVALUATION",
        },
    )
    save_state(path, migrated)


def dependent_stages(stage_name: str) -> list[str]:
    dependents: list[str] = []
    frontier = [stage_name]
    while frontier:
        parent = frontier.pop(0)
        for name in STAGE_ORDER:
            if name not in dependents and parent in STAGE_SPECS[name]["dependsOn"]:
                dependents.append(name)
                frontier.append(name)
    return dependents


def reset_stage(state: dict[str, Any], stage_name: str, status: str, reason: str) -> None:
    stage = state["stages"][stage_name]
    stage.update(
        {
            "status": status,
            "updatedAt": now_iso(),
            "actor": "pipeline",
            "note": f"invalidated: {reason}",
            "artifacts": [],
            "sceneSha256": None,
            "evaluation": None,
            "executionRunId": None,
            "identityDigest": None,
        }
    )


def invalidate_after(state: dict[str, Any], stage_name: str, reason: str) -> None:
    for later_name in dependent_stages(stage_name):
        later = state["stages"][later_name]
        if later["status"] != "PENDING":
            reset_stage(state, later_name, "PENDING", reason)


def apply_change_invalidation(state: dict[str, Any], change: str, reason: str) -> None:
    rule = PIPELINE_CONTRACT.get("changeInvalidation", {}).get(change)
    if not isinstance(rule, dict):
        raise WorkflowError(
            f"unsupported pipeline change kind: {change}",
            "INVALIDATION_KIND_UNKNOWN",
        )
    review_stage = rule.get("reviewStage")
    if isinstance(review_stage, str):
        reset_stage(state, review_stage, "REVIEW", reason)
    for name in rule.get("pendingStages", []):
        reset_stage(state, name, "PENDING", reason)


def require_capabilities(state: dict[str, Any], stage_name: str) -> list[dict[str, str]]:
    """Gate a stage on its capabilities; DEGRADED passes only with a recorded reason."""
    missing: list[str] = []
    degraded: list[dict[str, str]] = []
    for name in STAGE_REQUIREMENTS.get(stage_name, ()):
        record = state.get("capabilities", {}).get(name, {})
        status = record.get("status")
        if status == "AVAILABLE":
            continue
        reason = str(record.get("reason", "") or "").strip()
        if status == "DEGRADED" and reason:
            degraded.append({"capability": name, "degradationReason": reason})
            continue
        missing.append(name)
    if missing:
        raise WorkflowError(
            f"stage {stage_name} lacks AVAILABLE or reasoned DEGRADED capabilities: {', '.join(missing)}"
        )
    return degraded


def collect_capability_degradations(
    state: dict[str, Any], publish_degradations: list[dict[str, str]],
) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    for name in STAGE_ORDER:
        for item in state["stages"][name].get("capabilityDegradations") or []:
            entry = {"stage": name, **item}
            if entry not in collected:
                collected.append(entry)
    for item in publish_degradations:
        entry = {"stage": "publish", **item}
        if entry not in collected:
            collected.append(entry)
    return collected


def publish_scope(blocked: set[str]) -> tuple[str, list[str]]:
    """Decide the publish scope from job-level blocked capabilities, fail-closed."""
    if "whole-scene-acceptance" in blocked:
        raise WorkflowError(
            "job lacks whole-scene geometry acceptance; publication remains blocked",
            "PUBLISH_WHOLE_SCENE_BLOCKED",
        )
    pending = sorted(blocked.intersection({"material-acceptance", "posed-photo-association"}))
    return ("geometry-only" if pending else "full"), pending


def require_prerequisites(state: dict[str, Any], stage_name: str) -> None:
    current_code_sha = sha256_file(Path(__file__))
    def artifacts_are_current(stage: dict[str, Any]) -> bool:
        for item in stage.get("artifacts", []):
            path_text = item.get("path") if isinstance(item, dict) else None
            expected_sha = item.get("sha256") if isinstance(item, dict) else None
            if not path_text or not expected_sha:
                return False
            path = Path(str(path_text))
            if not path.is_file() or sha256_file(path) != expected_sha:
                return False
        return True

    missing = [
        name for name in STAGE_SPECS[stage_name]["dependsOn"]
        if (
            state["stages"][name]["status"] != "PASS"
            or not isinstance(state["stages"][name].get("evaluation"), dict)
            or state["stages"][name]["evaluation"].get("pipelineContractDigest")
            != PIPELINE_CONTRACT_DIGEST
            or state["stages"][name]["evaluation"].get("evaluatorCodeSha256")
            != current_code_sha
            or not artifacts_are_current(state["stages"][name])
        )
    ]
    if missing:
        raise WorkflowError(
            f"stage {stage_name} has incomplete or stale prerequisites: {', '.join(missing)}",
            "STAGE_PREREQUISITE_INCOMPLETE_OR_STALE",
            [f"reevaluate prerequisite stage {name}" for name in missing],
        )


def required_upstream_input_bindings(
    state: dict[str, Any], stage_name: str,
) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for prerequisite_name in STAGE_SPECS[stage_name]["dependsOn"]:
        prerequisite = state["stages"][prerequisite_name]
        required_types = list(STAGE_SPECS[prerequisite_name]["requiredArtifacts"])
        for index, item in enumerate(prerequisite.get("artifacts", [])):
            if not isinstance(item, dict) or not item.get("sha256"):
                continue
            artifact_type = item.get("artifactType")
            if not artifact_type and index < len(required_types):
                artifact_type = required_types[index]
            if not artifact_type or str(artifact_type).endswith("-checkpoint"):
                continue
            binding = {
                "artifactType": str(artifact_type),
                "artifactSha256": str(item["sha256"]),
            }
            if binding not in bindings:
                bindings.append(binding)
    return bindings


def open_issues(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [issue for issue in state.get("issues", []) if issue.get("status") != "RESOLVED"]


def invalidate_resolved_issues(state: dict[str, Any], scene_sha: str, except_id: str) -> None:
    for issue in state.get("issues", []):
        if issue.get("id") != except_id and issue.get("status") == "RESOLVED" and issue.get("resolvedOnSha256") != scene_sha:
            issue["status"] = "NEEDS_RECHECK"
            issue["staleResolutionSha256"] = issue.get("resolvedOnSha256")


def issue_by_id(state: dict[str, Any], issue_id: str) -> dict[str, Any]:
    matches = [issue for issue in state.get("issues", []) if issue.get("id") == issue_id]
    if len(matches) != 1:
        raise WorkflowError(f"issue does not exist uniquely: {issue_id}")
    return matches[0]


def scene_artifact(path: Path) -> dict[str, str]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise WorkflowError("semantic scene must be a JSON object")
    return artifact(path, "tool")


def validate_scene_authority(path: Path, state: dict[str, Any]) -> dict[str, str]:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schemaVersion") != "2.0":
        raise WorkflowError(
            "stage evaluator requires a Semantic Scene V2 authority artifact",
            "STAGE_SCENE_V2_REQUIRED",
            ["migrate the scene explicitly before evaluating the stage"],
        )
    item = artifact(path, "tool")
    item["artifactType"] = "scene-authority"
    return item


def validate_typed_artifact(
    path: Path,
    expected_type: str,
    state: dict[str, Any],
    scene_sha: str | None,
    required_inputs: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    if expected_type == "scene-authority":
        return validate_scene_authority(path, state)
    value = load_json(path)
    required_fields = {
        "schemaVersion",
        "artifactType",
        "jobId",
        "captureFingerprint",
        "payloadDigest",
        "producer",
        "inputs",
        "createdAt",
        "payload",
    }
    producer_fields = {
        "name",
        "version",
        "gitSha",
        "command",
        "configDigest",
        "environmentDigest",
        "randomSeed",
    }
    producer = value.get("producer") if isinstance(value, dict) else None
    created_at = parse_time(value.get("createdAt")) if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != required_fields
        or value.get("schemaVersion") != "1.0"
        or value.get("artifactType") != expected_type
        or value.get("jobId") != state.get("jobId")
        or value.get("captureFingerprint") != state.get("captureFingerprint")
        or value.get("payloadDigest") != canonical_digest(value.get("payload"))
        or not isinstance(producer, dict)
        or set(producer) != producer_fields
        or not re.fullmatch(r"[0-9a-f]{40}", str(producer.get("gitSha", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(producer.get("configDigest", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(producer.get("environmentDigest", "")))
        or not isinstance(producer.get("name"), str)
        or not producer["name"].strip()
        or not isinstance(producer.get("version"), str)
        or not producer["version"].strip()
        or not isinstance(producer.get("command"), list)
        or not producer["command"]
        or any(not isinstance(part, str) or not part for part in producer["command"])
        or not isinstance(producer.get("randomSeed"), int)
        or created_at is None
        or created_at > datetime.now(timezone.utc)
        or not isinstance(value.get("inputs"), list)
    ):
        raise WorkflowError(
            f"typed artifact {expected_type} is invalid or belongs to another job",
            "TYPED_ARTIFACT_INVALID",
            [f"regenerate {expected_type} with the pipeline artifact V1 envelope"],
        )
    inputs = value["inputs"]
    if any(
        not isinstance(item, dict)
        or set(item) != {"artifactType", "artifactSha256"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("artifactSha256", "")))
        for item in inputs
    ):
        raise WorkflowError(
            f"typed artifact {expected_type} has invalid input bindings",
            "TYPED_ARTIFACT_INPUT_INVALID",
        )
    if scene_sha and not any(
        item.get("artifactType") == "scene-authority"
        and item.get("artifactSha256") == scene_sha
        for item in inputs
    ):
        raise WorkflowError(
            f"typed artifact {expected_type} is stale for the current scene authority",
            "TYPED_ARTIFACT_SCENE_BINDING_STALE",
            [f"regenerate {expected_type} from the current scene authority"],
        )
    missing_inputs = [item for item in (required_inputs or []) if item not in inputs]
    if missing_inputs:
        raise WorkflowError(
            f"typed artifact {expected_type} is stale for prerequisite artifacts",
            "TYPED_ARTIFACT_PREREQUISITE_BINDING_STALE",
            ["regenerate the artifact from the current direct prerequisite artifacts"],
        )
    item = artifact(path, "tool")
    item["artifactType"] = expected_type
    item["payloadDigest"] = value["payloadDigest"]
    return item


def parse_stage_artifacts(
    values: list[str],
    state: dict[str, Any],
    stage_spec: dict[str, Any],
    scene_path: Path | None,
    scene_sha: str | None,
) -> list[dict[str, str]]:
    declared: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise WorkflowError(
                "stage artifact must use artifactType=path",
                "TYPED_ARTIFACT_SYNTAX_INVALID",
            )
        artifact_type, raw_path = value.split("=", 1)
        artifact_type = artifact_type.strip()
        if artifact_type in declared:
            raise WorkflowError(
                f"stage artifact type is duplicated: {artifact_type}",
                "TYPED_ARTIFACT_DUPLICATE",
            )
        declared[artifact_type] = Path(raw_path)
    if scene_path is not None and "scene-authority" in stage_spec["requiredArtifacts"]:
        declared.setdefault("scene-authority", scene_path)
    missing = [
        name for name in stage_spec["requiredArtifacts"] if name not in declared
    ]
    if missing:
        raise WorkflowError(
            f"stage {stage_spec['name']} lacks required typed artifacts: {', '.join(missing)}",
            "STAGE_ARTIFACT_MISSING",
            [
                f"rerun evaluate-stage with "
                + " ".join(f"--artifact {name}=<path>" for name in missing)
            ],
        )
    unexpected = [name for name in declared if name not in stage_spec["requiredArtifacts"]]
    if unexpected:
        raise WorkflowError(
            f"stage {stage_spec['name']} received unexpected artifacts: {', '.join(unexpected)}",
            "STAGE_ARTIFACT_UNEXPECTED",
        )
    return [
        validate_typed_artifact(
            declared[name],
            name,
            state,
            scene_sha if stage_spec["sceneBinding"] == "authority" and name != "scene-authority" else None,
            required_upstream_input_bindings(state, stage_spec["name"])
            if name != "scene-authority" else None,
        )
        for name in stage_spec["requiredArtifacts"]
    ]


def evaluate_required_artifact_checks(context: dict[str, Any]) -> dict[str, list[str]]:
    passed: dict[str, list[str]] = {}
    for item in context["artifacts"]:
        artifact_type = item["artifactType"]
        required_checks = context["stage"]["requiredArtifactChecks"].get(
            artifact_type, []
        )
        if not required_checks:
            passed[artifact_type] = []
            continue
        value = load_json(Path(item["path"]))
        payload = value.get("payload") if isinstance(value, dict) else None
        checks = payload.get("checks") if isinstance(payload, dict) else None
        failed = [
            name
            for name in required_checks
            if not isinstance(checks, dict) or checks.get(name) is not True
        ]
        if not isinstance(payload, dict) or payload.get("status") != "PASS" or failed:
            raise WorkflowError(
                f"typed artifact {artifact_type} did not pass required checks: "
                + ", ".join(failed or ["status"]),
                "STAGE_ARTIFACT_CHECK_FAILED",
                [f"regenerate {artifact_type} after resolving every required check"],
            )
        passed[artifact_type] = list(required_checks)
    return passed


def evaluate_evidence(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "typedEvidenceBundle": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_macro_hypothesis(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "typedMacroHypothesis": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_seed(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "sceneSchemaVersion": "2.0",
        "checkpointRequired": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_author(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "unresolvedIssueCount": len(open_issues(context["state"])),
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_presentation_review(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "authorityBoundPresentation": True,
        "independentReview": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_regional_review(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "authorityBoundRegionalReview": True,
        "omissionAuditPresent": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


def evaluate_global_review(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "authorityBoundGlobalReview": True,
        "independentReview": True,
        "passedArtifactChecks": evaluate_required_artifact_checks(context),
    }


STAGE_EVALUATORS = {
    "evaluate_evidence": evaluate_evidence,
    "evaluate_macro_hypothesis": evaluate_macro_hypothesis,
    "evaluate_seed": evaluate_seed,
    "evaluate_author": evaluate_author,
    "evaluate_presentation_review": evaluate_presentation_review,
    "evaluate_regional_review": evaluate_regional_review,
    "evaluate_global_review": evaluate_global_review,
}


def save_checkpoint(state_path: Path, scene_path: Path, label: str) -> dict[str, str]:
    scene = scene_artifact(scene_path)
    checkpoint_root = state_path.resolve().parent / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_root / f"{label}-{scene['sha256'][:12]}.json"
    if checkpoint_path.exists():
        if sha256_file(checkpoint_path) != scene["sha256"]:
            raise WorkflowError("checkpoint path collision")
    else:
        shutil.copyfile(scene_path.resolve(), checkpoint_path)
    return artifact(checkpoint_path, "tool")


def command_capability(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    actor_key(args.actor)
    if args.name not in state.get("capabilities", {}):
        raise WorkflowError(f"capability is not registered: {args.name}")
    if not args.reason.strip():
        raise WorkflowError("capability reason must not be empty")
    evidence = [artifact(Path(value), "tool") for value in args.evidence]
    if args.status == "AVAILABLE" and not evidence:
        raise WorkflowError("AVAILABLE capability requires an existing evidence path")
    receipt = None
    if args.status == "AVAILABLE":
        if not args.receipt:
            raise WorkflowError("AVAILABLE capability requires a probe receipt")
        receipt_path = args.receipt.resolve()
        receipt = load_json(receipt_path)
        checked_at = parse_time(receipt.get("checkedAt")) if isinstance(receipt, dict) else None
        expected_hashes = sorted(item["sha256"] for item in evidence)
        probe_command = receipt.get("probeCommand") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("capability") != args.name
            or receipt.get("status") != "PASS"
            or actor_key(str(receipt.get("checkedBy", ""))) == actor_key(args.actor)
            or checked_at is None
            or checked_at > datetime.now(timezone.utc)
            or not isinstance(probe_command, list)
            or not probe_command
            or sorted(receipt.get("evidenceSha256s", [])) != expected_hashes
        ):
            raise WorkflowError("capability probe receipt is invalid, stale, self-attested, or not bound to evidence")
        run_probe(
            probe_command,
            [Path(item["path"]) for item in evidence],
            args.state.resolve().parent,
        )
        receipt = artifact(receipt_path, "tool")
    state["capabilities"][args.name] = {
        "status": args.status,
        "reason": args.reason.strip(),
        "evidence": evidence,
        "probeReceipt": receipt,
        "updatedAt": now_iso(),
        "actor": args.actor,
    }
    event(state, args.actor, "capability", {"name": args.name, "status": args.status})
    save_state(args.state, state)


def command_evaluate_stage(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    actor_key(args.actor)
    if args.name in {"intake", "publish"}:
        raise WorkflowError(
            f"stage {args.name} is owned by its dedicated pipeline command",
            "DEDICATED_STAGE_COMMAND_REQUIRED",
            ["use init for intake" if args.name == "intake" else "use publish for publication"],
        )
    stage_spec = STAGE_SPECS[args.name]
    scene_path = args.scene.resolve() if args.scene else None
    scene_item = validate_scene_authority(scene_path, state) if scene_path else None
    scene_sha = scene_item["sha256"] if scene_item else None
    artifacts = parse_stage_artifacts(
        args.artifact,
        state,
        stage_spec,
        scene_path,
        scene_sha,
    )
    require_prerequisites(state, args.name)
    capability_degradations = require_capabilities(state, args.name)
    if stage_spec["sceneBinding"] == "authority" and scene_item is None:
        raise WorkflowError(
            f"stage {args.name} requires --scene bound to Semantic Scene V2 authority",
            "STAGE_SCENE_REQUIRED",
        )
    if (
        scene_sha
        and args.name != "seed"
        and state.get("currentSceneSha256") != scene_sha
    ):
        raise WorkflowError(
            "stage scene differs from the latest authority checkpoint",
            "STAGE_SCENE_STALE",
            ["patch or seed the current authority before evaluating this stage"],
        )
    if stage_spec["blocksOnOpenIssues"] and open_issues(state):
        raise WorkflowError(
            f"stage {args.name} cannot pass with unresolved issues",
            "STAGE_BLOCKED_BY_OPEN_ISSUES",
        )
    if args.name in {"presentation-review", "regional-review", "global-review"}:
        reviewer_identity = bind_execution(
            state, getattr(args, "execution", None), "pipeline:submit-verdict",
            execution_identity_api.REVIEW_ROLES, args.actor,
        )
        author_identity = registered_identity(
            state, state["stages"]["author"].get("executionRunId"),
        )
        try:
            execution_identity_api.require_independent_reviewer(
                author_identity,
                reviewer_identity,
                severity="P1" if args.name in {"regional-review", "global-review"} else None,
                required_input_digests={
                    *[item["sha256"] for item in artifacts],
                    *([scene_sha] if scene_sha else []),
                },
            )
        except execution_identity_api.IdentityError as error:
            raise WorkflowError(str(error), error.code) from error
    else:
        reviewer_identity = bind_execution(
            state, getattr(args, "execution", None), "pipeline:patch", {"author"}, args.actor,
        )

    evaluator_name = stage_spec["evaluator"]
    evaluator = STAGE_EVALUATORS.get(evaluator_name)
    if evaluator is None:
        raise WorkflowError(
            f"no dedicated evaluator is registered for stage {args.name}",
            "STAGE_EVALUATOR_MISSING",
        )
    checks = evaluator(
        {
            "state": state,
            "stage": stage_spec,
            "artifacts": artifacts,
            "scene": scene_item,
            "actor": args.actor,
        }
    )
    artifact_set_digest = canonical_digest(
        sorted(
            (
                item["artifactType"],
                item["sha256"],
                item.get("payloadDigest", ""),
            )
            for item in artifacts
        )
    )
    current = state["stages"][args.name]
    previous_evaluation = current.get("evaluation") or {}
    if (
        current.get("status") == "PASS"
        and previous_evaluation.get("artifactSetDigest") != artifact_set_digest
    ):
        invalidate_after(state, args.name, f"{args.name} artifact set changed")
    if args.name == "seed":
        checkpoint = save_checkpoint(args.state, scene_path, "seed")
        artifacts.append(checkpoint | {"artifactType": "scene-authority-checkpoint"})
        state["currentSceneSha256"] = scene_sha
        state["currentScenePath"] = str(scene_path)
        state["lastCheckpoint"] = checkpoint
    evaluation = {
        "evaluator": evaluator_name,
        "evaluatorCodeSha256": sha256_file(Path(__file__)),
        "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
        "artifactSetDigest": artifact_set_digest,
        "prerequisiteArtifactSetDigest": canonical_digest(
            required_upstream_input_bindings(state, args.name)
        ),
        "result": "PASS",
        "checks": checks,
        "evaluatedAt": now_iso(),
    }
    current.update(
        {
            "status": "PASS",
            "attempt": int(current.get("attempt", 0)) + 1,
            "updatedAt": now_iso(),
            "actor": args.actor,
            "note": args.note,
            "artifacts": artifacts,
            "sceneSha256": scene_sha,
            "evaluation": evaluation,
            "executionRunId": reviewer_identity["runId"] if reviewer_identity else None,
            "identityDigest": (
                execution_identity_api.identity_digest(reviewer_identity)
                if reviewer_identity else None
            ),
            "capabilityDegradations": capability_degradations,
        }
    )
    event(
        state,
        args.actor,
        "evaluate-stage",
        {
            "name": args.name,
            "result": "PASS",
            "sceneSha256": scene_sha,
            "artifactSetDigest": artifact_set_digest,
            "capabilityDegradations": capability_degradations,
        },
    )
    save_state(args.state, state)


def command_stage(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:update-stage", {"author"}, args.actor,
    )
    if args.name in {"intake", "publish"}:
        raise WorkflowError(f"{args.name} is owned by its dedicated pipeline command")
    if args.status == "PASS":
        raise WorkflowError(
            "generic stage command cannot write PASS; a dedicated evaluator must decide",
            "GENERIC_STAGE_PASS_FORBIDDEN",
            [
                f"python {Path(__file__).name} evaluate-stage --state {args.state} "
                f"--actor {args.actor} --execution <execution-identity.json> "
                f"--name {args.name} --artifact <artifactType=path>"
            ],
        )
    artifacts = [artifact(Path(value)) for value in args.artifact]
    scene_sha = None
    if args.scene:
        scene_item = scene_artifact(args.scene)
        scene_sha = scene_item["sha256"]
        artifacts.append(scene_item)
        if state.get("currentSceneSha256") and scene_sha != state["currentSceneSha256"]:
            raise WorkflowError("stage scene differs from the latest checkpoint")
        if args.name == "seed" and args.status == "PASS":
            checkpoint = save_checkpoint(args.state, args.scene, "seed")
            artifacts.append(checkpoint)
            state["currentSceneSha256"] = scene_sha
            state["currentScenePath"] = str(args.scene.resolve())
            state["lastCheckpoint"] = checkpoint
    if args.status == "PASS" and not artifacts:
        raise WorkflowError(f"stage {args.name} PASS requires at least one current artifact")
    current = state["stages"][args.name]
    current.update(
        {
            "status": args.status,
            "attempt": int(current.get("attempt", 0)) + 1,
            "updatedAt": now_iso(),
            "actor": identity["actorId"],
            "note": args.note,
            "artifacts": artifacts,
            "sceneSha256": scene_sha,
            "evaluation": None,
            "executionRunId": identity["runId"],
            "identityDigest": execution_identity_api.identity_digest(identity),
        }
    )
    if args.status != "PASS":
        invalidate_after(state, args.name, f"{args.name} became {args.status}")
    event(
        state,
        identity["actorId"],
        "stage",
        {"name": args.name, "status": args.status, "sceneSha256": scene_sha},
    )
    save_state(args.state, state)


def command_open_issue(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:open-issue", {"author"}, args.actor,
    )
    evidence = parse_evidence(args.evidence)
    if not evidence or not {item["role"] for item in evidence}.intersection(
        {"raw", "overlay", "elevation", "photo"}
    ):
        raise WorkflowError("opening an issue requires raw/overlay/elevation/photo evidence")
    issue_id = f"I{len(state.get('issues', [])) + 1:04d}"
    issue = {
        "id": issue_id,
        "status": "OPEN",
        "area": args.area,
        "severity": args.severity,
        "kind": args.kind,
        "targets": args.target,
        "summary": args.summary,
        "openedAt": now_iso(),
        "openedBy": identity["actorId"],
        "openedByRunId": identity["runId"],
        "evidence": evidence,
        "attempts": [],
        "stagnationCount": 0,
        "strategyChangeRequired": False,
        "patch": None,
        "resolvedOnSha256": None,
    }
    state.setdefault("issues", []).append(issue)
    state["stages"]["author"].update(
        {"status": "REVIEW", "updatedAt": now_iso(), "actor": args.actor, "note": f"open issue {issue_id}"}
    )
    invalidate_after(state, "author", f"opened {issue_id}")
    event(state, args.actor, "open-issue", {"id": issue_id, "severity": args.severity, "area": args.area})
    save_state(args.state, state)
    print(issue_id)


def command_patch(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:patch", {"author"}, args.actor,
    )
    issue = issue_by_id(state, args.issue)
    if issue.get("status") == "RESOLVED":
        raise WorkflowError("resolved issue must be reopened as a new issue")
    if issue.get("strategyChangeRequired") and not args.strategy_change:
        raise WorkflowError("stagnation gate requires --strategy-change describing a different approach")
    scene = scene_artifact(args.scene)
    attempts = issue.get("attempts", [])
    if attempts and attempts[-1].get("sceneSha256") == scene["sha256"]:
        raise WorkflowError("patch did not change the scene since the previous review")
    if args.checkpoint_dir:
        checkpoint_root = args.checkpoint_dir.resolve()
        if not inside(args.state.resolve().parent, checkpoint_root):
            raise WorkflowError("checkpoint directory must stay inside the reconstruction work directory")
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_root / f"{issue['id']}-a{len(attempts) + 1}-{scene['sha256'][:12]}.json"
        if checkpoint_path.exists():
            if sha256_file(checkpoint_path) != scene["sha256"]:
                raise WorkflowError("checkpoint path collision")
        else:
            shutil.copyfile(args.scene.resolve(), checkpoint_path)
        checkpoint = artifact(checkpoint_path, "tool")
    else:
        checkpoint = save_checkpoint(args.state, args.scene, f"{issue['id']}-a{len(attempts) + 1}")
    issue.update(
        {
            "status": "PATCHED",
            "patch": {
                "author": identity["actorId"],
                "authorExecutionRunId": identity["runId"],
                "authorIdentityDigest": execution_identity_api.identity_digest(identity),
                "at": now_iso(),
                "sceneSha256": scene["sha256"],
                "checkpoint": checkpoint,
                "note": args.note,
                "strategyChange": args.strategy_change or "",
            },
            "strategyChangeRequired": False,
        }
    )
    state["currentSceneSha256"] = scene["sha256"]
    state["currentScenePath"] = str(args.scene.resolve())
    state["lastCheckpoint"] = issue["patch"]["checkpoint"]
    invalidate_resolved_issues(state, scene["sha256"], issue["id"])
    state["stages"]["author"].update(
        {"status": "REVIEW", "updatedAt": now_iso(), "actor": args.actor, "note": f"patched {issue['id']}"}
    )
    invalidate_after(state, "author", f"scene changed for {issue['id']}")
    event(state, args.actor, "patch", {"id": issue["id"], "sceneSha256": scene["sha256"]})
    save_state(args.state, state)


def command_review(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    reviewer_identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:submit-verdict",
        execution_identity_api.REVIEW_ROLES, args.actor,
    )
    issue = issue_by_id(state, args.issue)
    recheck = issue.get("status") == "NEEDS_RECHECK"
    if not recheck and (issue.get("status") != "PATCHED" or not isinstance(issue.get("patch"), dict)):
        raise WorkflowError("issue must be PATCHED or NEEDS_RECHECK before review")
    scene = scene_artifact(args.scene)
    patch = issue.get("patch") or {}
    if not recheck and scene["sha256"] != patch.get("sceneSha256"):
        raise WorkflowError("review scene does not match the patched checkpoint")
    if recheck and scene["sha256"] != state.get("currentSceneSha256"):
        raise WorkflowError("recheck must use the current scene checkpoint")
    evidence = parse_evidence(args.evidence)
    roles = {item["role"] for item in evidence}
    if "render" not in roles or not roles.intersection({"raw", "overlay", "elevation", "photo"}):
        raise WorkflowError("review requires render evidence plus raw/overlay/elevation/photo evidence")
    if len({item["sha256"] for item in evidence}) < 2:
        raise WorkflowError("review evidence must contain at least two distinct artifacts")
    patch_author = patch.get("author") or issue.get("openedBy")
    author_run_id = patch.get("authorExecutionRunId") or issue.get("openedByRunId")
    author_identity = registered_identity(state, author_run_id)
    try:
        execution_identity_api.require_independent_reviewer(
            author_identity,
            reviewer_identity,
            severity=issue.get("severity"),
            required_input_digests={scene["sha256"], *[item["sha256"] for item in evidence]},
        )
    except execution_identity_api.IdentityError as error:
        raise WorkflowError(str(error), error.code) from error
    if args.verdict == "PASS" and args.score < 85:
        raise WorkflowError("PASS review score must be at least 85")
    previous_scores = [attempt.get("score") for attempt in issue.get("attempts", [])]
    previous_score = previous_scores[-1] if previous_scores else None
    attempt = {
        "at": now_iso(),
        "reviewer": reviewer_identity["actorId"],
        "reviewerExecutionRunId": reviewer_identity["runId"],
        "reviewerIdentityDigest": execution_identity_api.identity_digest(reviewer_identity),
        "verdict": args.verdict,
        "score": args.score,
        "sceneSha256": scene["sha256"],
        "evidence": evidence,
        "note": args.note,
        "patchAuthor": patch_author,
    }
    issue.setdefault("attempts", []).append(attempt)
    if args.verdict == "PASS":
        issue.update(
            {
                "status": "RESOLVED",
                "resolvedOnSha256": scene["sha256"],
                "resolvedAt": now_iso(),
                "resolvedBy": reviewer_identity["actorId"],
                "resolvedByRunId": reviewer_identity["runId"],
                "stagnationCount": 0,
                "strategyChangeRequired": False,
            }
        )
    else:
        improved = previous_score is None or args.score > previous_score
        issue["stagnationCount"] = 0 if improved else int(issue.get("stagnationCount", 0)) + 1
        issue["strategyChangeRequired"] = issue["stagnationCount"] >= 2
        issue["status"] = "OPEN"
        issue["patch"] = None
    event(
        state,
        args.actor,
        "review",
        {"id": issue["id"], "verdict": args.verdict, "score": args.score, "stagnation": issue["stagnationCount"]},
    )
    save_state(args.state, state)


def command_restore(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:restore", {"author"}, args.actor,
    )
    checkpoint = state.get("lastCheckpoint")
    if not isinstance(checkpoint, dict):
        raise WorkflowError("no recorded checkpoint is available")
    source = Path(str(checkpoint.get("path", ""))).resolve()
    if not source.is_file() or sha256_file(source) != checkpoint.get("sha256"):
        raise WorkflowError("recorded checkpoint is missing or tampered")
    destination = args.scene.resolve()
    recorded_destination = Path(str(state.get("currentScenePath", ""))).resolve()
    work_root = args.state.resolve().parent
    if destination != recorded_destination or (destination != work_root and work_root not in destination.parents):
        raise WorkflowError("restore destination must be the recorded semantic scene inside the reconstruction work directory")
    if destination.exists() and sha256_file(destination) == checkpoint["sha256"]:
        return
    shutil.copyfile(source, destination)
    state["currentSceneSha256"] = checkpoint["sha256"]
    invalidate_after(state, "author", "restored last checkpoint")
    event(state, identity["actorId"], "restore", {
        "scene": str(destination), "sha256": checkpoint["sha256"], "runId": identity["runId"],
    })
    save_state(args.state, state)


def command_invalidate(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:invalidate", {"author"}, args.actor,
    )
    if not args.reason.strip():
        raise WorkflowError(
            "invalidation reason must not be empty",
            "INVALIDATION_REASON_REQUIRED",
        )
    if args.change == "authority":
        if not args.scene:
            raise WorkflowError(
                "authority invalidation requires the new --scene checkpoint",
                "AUTHORITY_INVALIDATION_SCENE_REQUIRED",
            )
        scene = validate_scene_authority(args.scene.resolve(), state)
        if scene["sha256"] == state.get("currentSceneSha256"):
            raise WorkflowError(
                "authority invalidation scene did not change",
                "AUTHORITY_INVALIDATION_NO_CHANGE",
            )
        checkpoint = save_checkpoint(args.state, args.scene, "authority-change")
        state["currentSceneSha256"] = scene["sha256"]
        state["currentScenePath"] = str(args.scene.resolve())
        state["lastCheckpoint"] = checkpoint
        invalidate_resolved_issues(state, scene["sha256"], "")
    apply_change_invalidation(state, args.change, args.reason.strip())
    event(
        state,
        identity["actorId"],
        "invalidate",
        {"change": args.change, "reason": args.reason.strip()},
    )
    save_state(args.state, state)


def command_revalidate_intake(args: argparse.Namespace) -> None:
    """Upgrade job capabilities from a PASS pose-validation artifact.

    job.json is hash-locked into the pipeline state, so this is the only legal
    path that rewrites both documents together: job.blockedCapabilities loses
    posed-photo-association, state.jobSha256 follows, and the upgrade evidence
    (artifact sha256) is recorded for audit.
    """
    state = read_state(args.state)
    identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:update-stage", {"author"}, args.actor,
    )
    report_path = args.pose_validation.resolve()
    report = load_json(report_path)
    checks = report.get("checks") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("schemaVersion") != "1.0"
        or report.get("artifactType") != "pose-validation"
        or report.get("status") != "PASS"
        or not isinstance(checks, dict)
        or any(
            checks.get(name) != "PASS"
            for name in ("format", "coordinateConvention", "imageBindings", "pointCloudAlignment")
        )
    ):
        raise WorkflowError(
            "capability upgrade requires a pose-validation artifact with status PASS and every check PASS",
            "INTAKE_REVALIDATION_ARTIFACT_INVALID",
            ["run scene-core/capture_readiness.py validate-pose and retry with its PASS output"],
        )
    if not any(
        isinstance(item, dict)
        and item.get("artifactType") == "capture-manifest"
        and item.get("payloadDigest") == state.get("captureFingerprint")
        for item in report.get("inputs", [])
    ):
        raise WorkflowError(
            "pose-validation artifact is not bound to this job's capture fingerprint",
            "INTAKE_REVALIDATION_ARTIFACT_UNBOUND",
        )
    report_item = artifact(report_path, "tool")
    report_item["artifactType"] = "pose-validation"

    job_path = Path(str(state["job"])).resolve()
    previous_job_sha = str(state.get("jobSha256"))
    job = load_json(job_path)
    blocked = list(job.get("blockedCapabilities", []))
    if "posed-photo-association" not in blocked:
        raise WorkflowError(
            "job does not block posed-photo-association; there is nothing to revalidate",
            "INTAKE_REVALIDATION_NOT_REQUIRED",
        )
    job["blockedCapabilities"] = [name for name in blocked if name != "posed-photo-association"]
    if not job["blockedCapabilities"] and job.get("state") == "READY_GEOMETRY_ONLY":
        job["state"] = "READY_FULL"
    job.setdefault("intakeRevalidations", []).append(
        {
            "at": now_iso(),
            "actor": identity["actorId"],
            "runId": identity["runId"],
            "unblocked": ["posed-photo-association"],
            "poseValidationSha256": report_item["sha256"],
        }
    )
    state["capabilities"]["posed-photo-association"] = {
        "status": "AVAILABLE",
        "reason": "pose-validation artifact passed format, image-binding, and point-cloud alignment checks",
        "evidence": [report_item],
        "updatedAt": now_iso(),
        "actor": identity["actorId"],
        "upgrade": {"artifactType": "pose-validation", "artifactSha256": report_item["sha256"]},
    }
    event(
        state,
        identity["actorId"],
        "revalidate-intake",
        {
            "unblocked": ["posed-photo-association"],
            "jobState": job.get("state"),
            "poseValidationSha256": report_item["sha256"],
        },
    )
    state_path = args.state.resolve()
    expected_revision = int(state.get("revision", -1)) - 1
    # save_state cannot be reused here: the job rewrite and the state rewrite
    # must share one lock so read_state never observes a torn job binding.
    with state_write_lock(state_path):
        current = load_json(state_path)
        if int(current.get("revision", -1)) != expected_revision:
            raise WorkflowError(
                f"pipeline state revision changed from {expected_revision} to {current.get('revision')}",
                "STATE_REVISION_CONFLICT",
                ["reload pipeline-state.json and retry the mutation"],
            )
        if sha256_file(job_path) != previous_job_sha:
            raise WorkflowError(
                "job.json changed concurrently; revalidation was not applied",
                "STATE_REVISION_CONFLICT",
                ["reload pipeline-state.json and retry the mutation"],
            )
        write_json_atomic(job_path, job)
        new_job_sha = sha256_file(job_path)
        state["jobSha256"] = new_job_sha
        intake = state["stages"]["intake"]
        intake["artifacts"] = [{"path": str(job_path), "sha256": new_job_sha, "role": "tool"}]
        intake["note"] = str(job.get("state", intake.get("note", "")))
        intake["updatedAt"] = now_iso()
        write_json_atomic(state_path, state)
    print(json.dumps({"ok": True, "jobState": job.get("state"), "unblocked": ["posed-photo-association"]}, ensure_ascii=False))


def command_publish(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    publisher_identity = bind_execution(
        state, getattr(args, "execution", None), "pipeline:publish", {"publisher"}, args.actor,
    )
    require_prerequisites(state, "publish")
    publish_capability_degradations = require_capabilities(state, "publish")
    if open_issues(state):
        raise WorkflowError("cannot publish with unresolved issues")
    scene = scene_artifact(args.scene)
    scene_payload = load_json(args.scene)
    if scene_payload.get("schemaVersion") != "2.0":
        if "structures" in scene_payload or "structureCandidates" in scene_payload:
            raise WorkflowError(
                "LEGACY_SCENE_REQUIRES_MIGRATION:run scene-core/migrate_scene_v1_to_v2.py"
            )
        raise WorkflowError("SCHEMA_VERSION_MISMATCH:publish requires Semantic Scene V2")
    review = load_json(args.review)
    review_identity = review.get("reviewer")
    try:
        normalized_review_identity = execution_identity_api.normalize_identity(review_identity)
    except execution_identity_api.IdentityError as error:
        raise WorkflowError(str(error), error.code) from error
    global_review_run = state["stages"]["global-review"].get("executionRunId")
    if normalized_review_identity.get("runId") != global_review_run:
        raise WorkflowError(
            "review receipt identity differs from the global-review execution",
            "REVIEW_RECEIPT_IDENTITY_STALE",
        )
    quality_report_path = getattr(args, "quality_report", None)
    if quality_report_path is None:
        quality_report_path = getattr(args, "score", None)
    if quality_report_path is None:
        raise WorkflowError("publish requires --quality-report")
    quality_report = load_json(quality_report_path)
    if scene["sha256"] != state.get("currentSceneSha256"):
        raise WorkflowError("publish scene is not the latest checkpoint")
    if state["stages"]["global-review"].get("sceneSha256") != scene["sha256"]:
        raise WorkflowError("global review is stale for this scene")
    if quality_report.get("status") != "PASS":
        raise WorkflowError("quality report is not PASS for this scene")
    if quality_report.get("artifactSha256") != scene["sha256"]:
        raise WorkflowError("quality report is stale for this scene artifact")
    evaluator = Path(__file__).resolve().parents[4] / "scene-core" / "quality_report_v2.py"
    with tempfile.TemporaryDirectory(prefix="reconstruction-publish-quality-") as temp_root:
        recomputed_path = Path(temp_root) / "quality-report.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(evaluator),
                "--scene",
                str(args.scene.resolve()),
                "--review",
                str(args.review.resolve()),
                "--output",
                str(recomputed_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WorkflowError(
                "independent V2 quality recomputation failed; publication remains blocked"
                + (f": {detail}" if detail else "")
            )
        recomputed = load_json(recomputed_path)
    if recomputed != quality_report:
        raise WorkflowError("supplied quality report differs from independent V2 recomputation")
    job = load_json(Path(state["job"]))
    # Fail-closed by capability, not structurally: geometry-only publishing is
    # legal while unvalidated pose/material scopes stay excluded and visible.
    scope, scope_blocked_capabilities = publish_scope(set(job.get("blockedCapabilities", [])))
    geometry_digest = quality_report.get("geometryDigest")
    if not isinstance(geometry_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", geometry_digest):
        raise WorkflowError("quality report has no valid geometryDigest")
    publish_dir = args.output.resolve() / geometry_digest[:16]
    if publish_dir.exists():
        raise WorkflowError(f"immutable publish directory already exists: {publish_dir}")
    publish_dir.mkdir(parents=True)
    files = {}
    for name, source in (
        ("scene-authority.json", args.scene),
        ("review-receipt.json", args.review),
        ("quality-report.json", quality_report_path),
    ):
        destination = publish_dir / name
        shutil.copyfile(source.resolve(), destination)
        files[name] = artifact(destination)
    manifest = {
        "schemaVersion": "2.0",
        "publishedAt": now_iso(),
        "publisher": publisher_identity,
        "jobId": state.get("jobId"),
        "captureFingerprint": state.get("captureFingerprint"),
        "geometryDigest": geometry_digest,
        "artifactSha256": scene["sha256"],
        "evidenceSetDigest": quality_report.get("evidenceSetDigest"),
        "configDigest": quality_report.get("configDigest"),
        "publishScope": scope,
        "scopeBlockedCapabilities": scope_blocked_capabilities,
        "capabilityDegradations": collect_capability_degradations(
            state, publish_capability_degradations
        ),
        "files": files,
    }
    write_json_atomic(publish_dir / "publish-manifest.json", manifest)
    for published_file in publish_dir.iterdir():
        if published_file.is_file():
            os.chmod(published_file, 0o444)
    stage = state["stages"]["publish"]
    stage.update(
        {
            "status": "PASS",
            "attempt": int(stage.get("attempt", 0)) + 1,
            "updatedAt": now_iso(),
            "actor": publisher_identity["actorId"],
            "executionRunId": publisher_identity["runId"],
            "identityDigest": execution_identity_api.identity_digest(publisher_identity),
            "note": args.note,
            "artifacts": [artifact(publish_dir / "publish-manifest.json")],
            "sceneSha256": scene["sha256"],
            "evaluation": {
                "evaluator": STAGE_SPECS["publish"]["evaluator"],
                "evaluatorCodeSha256": sha256_file(Path(__file__)),
                "pipelineContractDigest": PIPELINE_CONTRACT_DIGEST,
                "result": "PASS",
                "geometryDigest": geometry_digest,
                "publishScope": scope,
            },
            "capabilityDegradations": publish_capability_degradations,
        }
    )
    event(
        state,
        publisher_identity["actorId"],
        "publish",
        {
            "directory": str(publish_dir),
            "geometryDigest": geometry_digest,
            "artifactSha256": scene["sha256"],
            "publishScope": scope,
        },
    )
    save_state(args.state, state)
    print(str(publish_dir))


def command_status(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    published = state["stages"]["publish"]
    if published.get("status") == "PASS":
        manifests = published.get("artifacts", [])
        manifest_path = Path(str(manifests[0].get("path", ""))).resolve() if manifests else None
        valid = bool(manifest_path and manifest_path.is_file())
        manifest = load_json(manifest_path) if valid else {}
        for item in manifest.get("files", {}).values() if isinstance(manifest, dict) else []:
            path = Path(str(item.get("path", ""))).resolve() if isinstance(item, dict) else None
            if not path or not path.is_file() or sha256_file(path) != item.get("sha256"):
                valid = False
                break
        if not valid:
            published.update(
                {"status": "FAILED", "updatedAt": now_iso(), "actor": "pipeline", "note": "published snapshot was modified"}
            )
            event(state, "pipeline", "publish-invalidated", {"reason": "snapshot hash mismatch"})
            save_state(args.state, state)
    summary = {
        "jobId": state.get("jobId"),
        "revision": state.get("revision"),
        "currentSceneSha256": state.get("currentSceneSha256"),
        "stages": {
            name: {
                "status": state["stages"][name]["status"],
                "note": state["stages"][name].get("note", ""),
                "sceneSha256": state["stages"][name].get("sceneSha256"),
            }
            for name in STAGE_ORDER
        },
        "capabilities": {name: value.get("status") for name, value in state.get("capabilities", {}).items()},
        "issues": {
            status: sum(1 for issue in state.get("issues", []) if issue.get("status") == status)
            for status in sorted(ISSUE_STATES)
        },
        "strategyChangeRequired": [
            issue.get("id") for issue in state.get("issues", []) if issue.get("strategyChangeRequired")
        ],
        "nextActions": next_actions(state, args.state),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def next_actions(state: dict[str, Any], state_path: Path) -> list[str]:
    for name in STAGE_ORDER:
        if state["stages"][name]["status"] == "PASS":
            continue
        if name == "intake":
            return ["repair the reconstruction job and rerun init"]
        if name == "publish":
            return [
                f"python {Path(__file__).name} publish --state {state_path} "
                "--actor <publisher> --execution <publisher-identity.json> "
                "--scene <scene-authority.json> --review <review-receipt.json> "
                "--quality-report <quality-report.json> --output <publish-root>"
            ]
        required = " ".join(
            f"--artifact {artifact_type}=<path>"
            for artifact_type in STAGE_SPECS[name]["requiredArtifacts"]
            if artifact_type != "scene-authority"
        )
        scene = " --scene <scene-authority.json>" if STAGE_SPECS[name]["sceneBinding"] == "authority" else ""
        return [
            f"python {Path(__file__).name} evaluate-stage --state {state_path} "
            f"--actor <stage-actor> --execution <execution-identity.json> "
            f"--name {name}{scene} {required}".strip()
        ]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--job", required=True, type=Path)
    init.add_argument("--state", required=True, type=Path)

    migrate = subparsers.add_parser("migrate-state")
    migrate.add_argument("--state", required=True, type=Path)
    migrate.add_argument("--actor", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--state", required=True, type=Path)

    capability = subparsers.add_parser("capability")
    capability.add_argument("--state", required=True, type=Path)
    capability.add_argument("--actor", required=True)
    capability.add_argument("--name", required=True)
    capability.add_argument("--status", required=True, choices=sorted(CAPABILITY_STATES))
    capability.add_argument("--reason", required=True)
    capability.add_argument("--evidence", action="append", default=[])
    capability.add_argument("--receipt", type=Path)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--state", required=True, type=Path)
    stage.add_argument("--actor", required=True)
    stage.add_argument("--execution", required=True, type=Path)
    stage.add_argument("--name", required=True, choices=STAGE_ORDER)
    stage.add_argument("--status", required=True, choices=sorted(STAGE_STATES - {"PASS"}))
    stage.add_argument("--artifact", action="append", default=[])
    stage.add_argument("--scene", type=Path)
    stage.add_argument("--note", default="")

    evaluate = subparsers.add_parser("evaluate-stage")
    evaluate.add_argument("--state", required=True, type=Path)
    evaluate.add_argument("--actor", required=True)
    evaluate.add_argument("--execution", required=True, type=Path)
    evaluate.add_argument("--name", required=True, choices=STAGE_ORDER)
    evaluate.add_argument("--artifact", action="append", default=[])
    evaluate.add_argument("--scene", type=Path)
    evaluate.add_argument("--note", default="")

    issue = subparsers.add_parser("open-issue")
    issue.add_argument("--state", required=True, type=Path)
    issue.add_argument("--actor", required=True)
    issue.add_argument("--execution", required=True, type=Path)
    issue.add_argument("--area", required=True)
    issue.add_argument("--severity", required=True, choices=("P0", "P1", "P2"))
    issue.add_argument("--kind", required=True)
    issue.add_argument("--target", action="append", default=[])
    issue.add_argument("--summary", required=True)
    issue.add_argument("--evidence", action="append", default=[])

    patch = subparsers.add_parser("patch")
    patch.add_argument("--state", required=True, type=Path)
    patch.add_argument("--actor", required=True)
    patch.add_argument("--execution", required=True, type=Path)
    patch.add_argument("--issue", required=True)
    patch.add_argument("--scene", required=True, type=Path)
    patch.add_argument("--checkpoint-dir", type=Path)
    patch.add_argument("--note", required=True)
    patch.add_argument("--strategy-change")

    review = subparsers.add_parser("review")
    review.add_argument("--state", required=True, type=Path)
    review.add_argument("--actor", required=True)
    review.add_argument("--execution", required=True, type=Path)
    review.add_argument("--issue", required=True)
    review.add_argument("--scene", required=True, type=Path)
    review.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    review.add_argument("--score", required=True, type=int, choices=range(0, 101))
    review.add_argument("--evidence", action="append", default=[])
    review.add_argument("--note", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--state", required=True, type=Path)
    restore.add_argument("--actor", required=True)
    restore.add_argument("--execution", required=True, type=Path)
    restore.add_argument("--scene", required=True, type=Path)

    invalidate = subparsers.add_parser("invalidate")
    invalidate.add_argument("--state", required=True, type=Path)
    invalidate.add_argument("--actor", required=True)
    invalidate.add_argument("--execution", required=True, type=Path)
    invalidate.add_argument(
        "--change",
        required=True,
        choices=sorted(PIPELINE_CONTRACT["changeInvalidation"]),
    )
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--scene", type=Path)

    revalidate = subparsers.add_parser("revalidate-intake")
    revalidate.add_argument("--state", required=True, type=Path)
    revalidate.add_argument("--actor", required=True)
    revalidate.add_argument("--execution", required=True, type=Path)
    revalidate.add_argument("--pose-validation", required=True, type=Path, dest="pose_validation")

    publish = subparsers.add_parser("publish")
    publish.add_argument("--state", required=True, type=Path)
    publish.add_argument("--actor", required=True)
    publish.add_argument("--execution", required=True, type=Path)
    publish.add_argument("--scene", required=True, type=Path)
    publish.add_argument("--review", required=True, type=Path)
    publish.add_argument("--quality-report", required=True, type=Path)
    publish.add_argument("--output", required=True, type=Path)
    publish.add_argument("--note", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            initialize_workflow(args.job, args.state)
        elif args.command == "migrate-state":
            command_migrate_state(args)
        elif args.command == "status":
            command_status(args)
        elif args.command == "capability":
            command_capability(args)
        elif args.command == "stage":
            command_stage(args)
        elif args.command == "evaluate-stage":
            command_evaluate_stage(args)
        elif args.command == "open-issue":
            command_open_issue(args)
        elif args.command == "patch":
            command_patch(args)
        elif args.command == "review":
            command_review(args)
        elif args.command == "restore":
            command_restore(args)
        elif args.command == "invalidate":
            command_invalidate(args)
        elif args.command == "revalidate-intake":
            command_revalidate_intake(args)
        elif args.command == "publish":
            command_publish(args)
        else:
            raise WorkflowError(f"unknown command: {args.command}")
    except WorkflowError as error:
        print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        wrapped = WorkflowError(str(error), "WORKFLOW_IO_OR_DATA_ERROR")
        print(json.dumps({"ok": False, "error": wrapped.to_dict()}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
