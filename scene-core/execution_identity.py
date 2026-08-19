#!/usr/bin/env python3
"""Fail-closed execution identities and checked-in tool policies."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "schemas" / "tool-policies-v1.json"
POLICIES = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["policies"]
MUTATION_OPERATIONS = {
    "scene:create", "scene:mutate", "scene:delete", "scene:undo",
    "evidence:attach", "pipeline:open-issue", "pipeline:patch",
    "pipeline:update-stage", "pipeline:restore", "pipeline:invalidate",
}
REVIEW_ROLES = {"reviewer", "deterministic-checker"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class IdentityError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}:{message or code}")
        self.code = code


def canonical_digest(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def policy_digest(policy_id: str) -> str:
    policy = POLICIES.get(policy_id)
    if not isinstance(policy, dict):
        raise IdentityError("TOOL_POLICY_UNKNOWN", policy_id)
    return canonical_digest(policy)


def policy_operations(identity: dict[str, Any]) -> set[str]:
    policy = POLICIES.get(str(identity.get("policyId") or ""))
    if not isinstance(policy, dict):
        return set()
    return {str(value) for value in policy.get("operations", [])}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _deterministic_binding_valid(identity: dict[str, Any]) -> bool:
    binding = identity.get("deterministicBinding")
    return (
        isinstance(binding, dict)
        and SHA256_RE.fullmatch(str(binding.get("codeSha256") or "")) is not None
        and SHA256_RE.fullmatch(str(binding.get("configDigest") or "")) is not None
        and isinstance(binding.get("inputDigests"), list)
        and bool(binding["inputDigests"])
        and len(set(binding["inputDigests"])) == len(binding["inputDigests"])
        and all(SHA256_RE.fullmatch(str(value)) is not None for value in binding["inputDigests"])
    )


def normalize_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IdentityError("EXECUTION_IDENTITY_REQUIRED")
    allowed = {
        "schemaVersion", "actorId", "runId", "role", "reviewerClass", "provider",
        "model", "policyId", "toolPolicyHash", "startedAt", "attestation",
        "deterministicBinding",
    }
    if set(value) - allowed:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "unknown fields")
    if value.get("schemaVersion") != "1.0":
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "schemaVersion")
    actor_id = str(value.get("actorId") or "").strip().casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", actor_id) is None:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "actorId")
    run_id = str(value.get("runId") or "").strip().casefold()
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "runId")
    role = value.get("role")
    if role not in {"author", "reviewer", "deterministic-checker", "publisher"}:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "role")
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or "").strip()
    if not provider or not model:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "provider/model")
    policy_id = str(value.get("policyId") or "")
    expected_policy_hash = policy_digest(policy_id)
    if value.get("toolPolicyHash") != expected_policy_hash:
        raise IdentityError("TOOL_POLICY_HASH_MISMATCH", policy_id)
    started_at = _parse_time(value.get("startedAt"))
    if started_at is None or started_at > datetime.now(timezone.utc):
        raise IdentityError("EXECUTION_IDENTITY_INVALID", "startedAt")
    attestation = value.get("attestation")
    if not isinstance(attestation, dict) or not str(attestation.get("issuer") or "").strip():
        raise IdentityError("TOOL_POLICY_ATTESTATION_INVALID")
    if attestation.get("enforcementMode") not in {
        "application-enforced", "sandbox-enforced", "deterministic-local",
    }:
        raise IdentityError("TOOL_POLICY_ATTESTATION_INVALID")
    if role in REVIEW_ROLES:
        reviewer_class = value.get("reviewerClass")
        if reviewer_class not in {"standard", "regional", "adversarial", "deterministic"}:
            raise IdentityError("EXECUTION_IDENTITY_INVALID", "reviewerClass")
        if policy_operations(value) & MUTATION_OPERATIONS:
            raise IdentityError("REVIEWER_TOOL_POLICY_NOT_READ_ONLY")
        if "pipeline:submit-verdict" not in policy_operations(value):
            raise IdentityError("REVIEWER_TOOL_POLICY_NOT_READ_ONLY")
    if role == "deterministic-checker":
        if (
            policy_id != "deterministic-checker-v1"
            or value.get("reviewerClass") != "deterministic"
            or provider != "deterministic-checker"
        ):
            raise IdentityError("DETERMINISTIC_CHECKER_IDENTITY_INVALID")
        if not _deterministic_binding_valid(value):
            raise IdentityError("DETERMINISTIC_CHECKER_BINDING_REQUIRED")
    return json.loads(json.dumps({**value, "actorId": actor_id, "runId": run_id}))


def require_operation(identity: object, operation: str, roles: set[str] | None = None) -> dict[str, Any]:
    normalized = normalize_identity(identity)
    if roles is not None and normalized["role"] not in roles:
        raise IdentityError("EXECUTION_ROLE_NOT_ALLOWED", normalized["role"])
    if operation not in policy_operations(normalized):
        raise IdentityError("EXECUTION_OPERATION_FORBIDDEN", operation)
    return normalized


def require_independent_reviewer(
    author_identity: object,
    reviewer_identity: object,
    *,
    severity: str | None = None,
    required_input_digests: set[str] | None = None,
) -> dict[str, Any]:
    author = normalize_identity(author_identity)
    reviewer = require_operation(
        reviewer_identity, "pipeline:submit-verdict", roles=REVIEW_ROLES,
    )
    deterministic = reviewer["role"] == "deterministic-checker"
    if deterministic:
        bound_inputs = set(reviewer["deterministicBinding"]["inputDigests"])
        required_inputs = set(required_input_digests or ())
        if not required_inputs or not required_inputs.issubset(bound_inputs):
            raise IdentityError("DETERMINISTIC_CHECKER_BINDING_STALE")
    if not deterministic and reviewer["actorId"] == author["actorId"]:
        raise IdentityError("SELF_REVIEW_FORBIDDEN")
    if reviewer["runId"] == author["runId"]:
        raise IdentityError("REVIEWER_RUN_NOT_INDEPENDENT")
    if severity in {"P0", "P1"} and not deterministic and reviewer.get("reviewerClass") not in {
        "regional", "adversarial",
    }:
        raise IdentityError("P0_P1_INDEPENDENT_REVIEW_REQUIRED")
    return reviewer


def identity_digest(identity: object) -> str:
    return canonical_digest(normalize_identity(identity))


def load_identity(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdentityError("EXECUTION_IDENTITY_INVALID", str(path)) from error
    return normalize_identity(value)
