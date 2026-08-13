#!/usr/bin/env python3
"""Resumable, fail-closed orchestration for evidence-backed scene authoring."""

from __future__ import annotations

import argparse
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


STAGE_ORDER = (
    "intake",
    "evidence",
    "seed",
    "author",
    "regional-review",
    "global-review",
    "publish",
)
STAGE_REQUIREMENTS = {
    "evidence": ("point-cloud-sections",),
    "seed": ("semantic-scene-compiler",),
    "author": ("semantic-edit", "deterministic-render"),
    "regional-review": ("deterministic-render", "visual-inspection"),
    "global-review": ("visual-inspection", "topology-check", "overlap-check"),
    "publish": ("score-gate",),
}
STAGE_STATES = {"PENDING", "IN_PROGRESS", "REVIEW", "PASS", "BLOCKED", "FAILED"}
CAPABILITY_STATES = {"UNVERIFIED", "AVAILABLE", "DEGRADED", "BLOCKED"}
ISSUE_STATES = {"OPEN", "PATCHED", "NEEDS_RECHECK", "RESOLVED"}
EVIDENCE_ROLES = {
    "raw", "overlay", "render", "elevation", "photo", "topology", "collision", "score", "tool"
}


class WorkflowError(RuntimeError):
    pass


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
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


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
    stages = {
        name: {
            "status": "PENDING",
            "attempt": 0,
            "updatedAt": None,
            "actor": None,
            "note": "",
            "artifacts": [],
            "sceneSha256": None,
        }
        for name in STAGE_ORDER
    }
    stages["intake"].update(
        {
            "status": "PASS" if ready else "BLOCKED",
            "attempt": 1,
            "updatedAt": now_iso(),
            "actor": "init_reconstruction_job",
            "note": str(job.get("state", "BLOCKED_INVALID_JOB")),
            "artifacts": [artifact(job_path, "tool")],
        }
    )
    state = {
        "schemaVersion": 1,
        "jobId": job.get("jobId"),
        "captureFingerprint": job.get("captureFingerprint"),
        "job": str(job_path),
        "jobSha256": job_hash,
        "createdAt": now_iso(),
        "revision": 0,
        "stageOrder": list(STAGE_ORDER),
        "stages": stages,
        "capabilities": initial_capabilities(job, job_path),
        "issues": [],
        "currentSceneSha256": None,
        "lastCheckpoint": None,
        "events": [],
    }
    event(state, "init_reconstruction_job", "initialize", {"jobState": job.get("state")})
    write_json_atomic(state_path, state)
    return state


def read_state(path: Path) -> dict[str, Any]:
    state = load_json(path)
    if state.get("schemaVersion") != 1 or state.get("stageOrder") != list(STAGE_ORDER):
        raise WorkflowError("unsupported or invalid pipeline state")
    job_path = Path(str(state.get("job", ""))).resolve()
    if (
        not job_path.is_file()
        or sha256_file(job_path) != state.get("jobSha256")
        or load_json(job_path).get("captureFingerprint") != state.get("captureFingerprint")
    ):
        raise WorkflowError("pipeline job binding is missing, modified, or belongs to another capture")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    write_json_atomic(path.resolve(), state)


def invalidate_after(state: dict[str, Any], stage_name: str, reason: str) -> None:
    index = STAGE_ORDER.index(stage_name)
    for later_name in STAGE_ORDER[index + 1 :]:
        later = state["stages"][later_name]
        if later["status"] != "PENDING":
            later.update(
                {
                    "status": "PENDING",
                    "updatedAt": now_iso(),
                    "actor": "pipeline",
                    "note": f"invalidated: {reason}",
                    "artifacts": [],
                    "sceneSha256": None,
                }
            )


def require_capabilities(state: dict[str, Any], stage_name: str) -> None:
    missing = [
        name
        for name in STAGE_REQUIREMENTS.get(stage_name, ())
        if state.get("capabilities", {}).get(name, {}).get("status") != "AVAILABLE"
    ]
    if missing:
        raise WorkflowError(f"stage {stage_name} lacks AVAILABLE capabilities: {', '.join(missing)}")


def require_prerequisites(state: dict[str, Any], stage_name: str) -> None:
    index = STAGE_ORDER.index(stage_name)
    missing = [
        name for name in STAGE_ORDER[:index]
        if state["stages"][name]["status"] != "PASS"
    ]
    if missing:
        raise WorkflowError(f"stage {stage_name} has incomplete prerequisites: {', '.join(missing)}")


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


def command_stage(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    actor_key(args.actor)
    if args.name in {"intake", "publish"}:
        raise WorkflowError(f"{args.name} is owned by its dedicated pipeline command")
    if args.status == "PASS":
        require_prerequisites(state, args.name)
        require_capabilities(state, args.name)
        if args.name in {"seed", "author", "regional-review", "global-review"} and not args.scene:
            raise WorkflowError(f"stage {args.name} PASS requires --scene")
        if args.name in {"author", "regional-review", "global-review"} and open_issues(state):
            raise WorkflowError(f"stage {args.name} cannot pass with unresolved issues")
        if args.name in {"regional-review", "global-review"}:
            author_actor = state["stages"]["author"].get("actor")
            if author_actor and actor_key(args.actor) == actor_key(author_actor):
                raise WorkflowError(f"stage {args.name} reviewer must differ from the author stage actor")
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
            "actor": args.actor,
            "note": args.note,
            "artifacts": artifacts,
            "sceneSha256": scene_sha,
        }
    )
    if args.status != "PASS":
        invalidate_after(state, args.name, f"{args.name} became {args.status}")
    event(state, args.actor, "stage", {"name": args.name, "status": args.status, "sceneSha256": scene_sha})
    save_state(args.state, state)


def command_open_issue(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    actor_key(args.actor)
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
        "openedBy": args.actor,
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
    actor_key(args.actor)
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
                "author": args.actor,
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
    actor_key(args.actor)
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
    if actor_key(args.actor) == actor_key(str(patch_author)):
        raise WorkflowError("patch author cannot review the same issue")
    if args.verdict == "PASS" and args.score < 85:
        raise WorkflowError("PASS review score must be at least 85")
    previous_scores = [attempt.get("score") for attempt in issue.get("attempts", [])]
    previous_score = previous_scores[-1] if previous_scores else None
    attempt = {
        "at": now_iso(),
        "reviewer": args.actor,
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
                "resolvedBy": args.actor,
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
    actor_key(args.actor)
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
    event(state, args.actor, "restore", {"scene": str(destination), "sha256": checkpoint["sha256"]})
    save_state(args.state, state)


def command_publish(args: argparse.Namespace) -> None:
    state = read_state(args.state)
    actor_key(args.actor)
    require_prerequisites(state, "publish")
    require_capabilities(state, "publish")
    if open_issues(state):
        raise WorkflowError("cannot publish with unresolved issues")
    scene = scene_artifact(args.scene)
    review = load_json(args.review)
    score = load_json(args.score)
    if scene["sha256"] != state.get("currentSceneSha256"):
        raise WorkflowError("publish scene is not the latest checkpoint")
    if state["stages"]["global-review"].get("sceneSha256") != scene["sha256"]:
        raise WorkflowError("global review is stale for this scene")
    if review.get("sceneSha256") != scene["sha256"]:
        raise WorkflowError("review receipt is stale for this scene")
    if score.get("status") != "PASS" or score.get("sceneSha256") != scene["sha256"]:
        raise WorkflowError("score report is not PASS for this scene")
    scorer = Path(__file__).with_name("score_scene.py")
    with tempfile.TemporaryDirectory(prefix="reconstruction-publish-score-") as temp_root:
        recomputed_path = Path(temp_root) / "score.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(scorer),
                "--scene",
                str(args.scene.resolve()),
                "--visual-review",
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
            raise WorkflowError("independent score recomputation failed; publication remains blocked")
        recomputed = load_json(recomputed_path)
    if recomputed != score:
        raise WorkflowError("supplied score report differs from independent recomputation")
    job = load_json(Path(state["job"]))
    blocked = set(job.get("blockedCapabilities", []))
    if blocked.intersection({"whole-scene-acceptance", "material-acceptance", "posed-photo-association"}):
        raise WorkflowError("job remains geometry-only or lacks whole-scene evidence capabilities")
    publish_dir = args.output.resolve() / scene["sha256"][:16]
    if publish_dir.exists():
        raise WorkflowError(f"immutable publish directory already exists: {publish_dir}")
    publish_dir.mkdir(parents=True)
    files = {}
    for name, source in (("scene.json", args.scene), ("review-receipt.json", args.review), ("scene-score.json", args.score)):
        destination = publish_dir / name
        shutil.copyfile(source.resolve(), destination)
        files[name] = artifact(destination)
    manifest = {
        "schemaVersion": 1,
        "publishedAt": now_iso(),
        "publisher": args.actor,
        "jobId": state.get("jobId"),
        "captureFingerprint": state.get("captureFingerprint"),
        "sceneSha256": scene["sha256"],
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
            "actor": args.actor,
            "note": args.note,
            "artifacts": [artifact(publish_dir / "publish-manifest.json")],
            "sceneSha256": scene["sha256"],
        }
    )
    event(state, args.actor, "publish", {"directory": str(publish_dir), "sceneSha256": scene["sha256"]})
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
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--job", required=True, type=Path)
    init.add_argument("--state", required=True, type=Path)

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
    stage.add_argument("--name", required=True, choices=STAGE_ORDER)
    stage.add_argument("--status", required=True, choices=sorted(STAGE_STATES))
    stage.add_argument("--artifact", action="append", default=[])
    stage.add_argument("--scene", type=Path)
    stage.add_argument("--note", default="")

    issue = subparsers.add_parser("open-issue")
    issue.add_argument("--state", required=True, type=Path)
    issue.add_argument("--actor", required=True)
    issue.add_argument("--area", required=True)
    issue.add_argument("--severity", required=True, choices=("P0", "P1", "P2"))
    issue.add_argument("--kind", required=True)
    issue.add_argument("--target", action="append", default=[])
    issue.add_argument("--summary", required=True)
    issue.add_argument("--evidence", action="append", default=[])

    patch = subparsers.add_parser("patch")
    patch.add_argument("--state", required=True, type=Path)
    patch.add_argument("--actor", required=True)
    patch.add_argument("--issue", required=True)
    patch.add_argument("--scene", required=True, type=Path)
    patch.add_argument("--checkpoint-dir", type=Path)
    patch.add_argument("--note", required=True)
    patch.add_argument("--strategy-change")

    review = subparsers.add_parser("review")
    review.add_argument("--state", required=True, type=Path)
    review.add_argument("--actor", required=True)
    review.add_argument("--issue", required=True)
    review.add_argument("--scene", required=True, type=Path)
    review.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    review.add_argument("--score", required=True, type=int, choices=range(0, 101))
    review.add_argument("--evidence", action="append", default=[])
    review.add_argument("--note", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--state", required=True, type=Path)
    restore.add_argument("--actor", required=True)
    restore.add_argument("--scene", required=True, type=Path)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--state", required=True, type=Path)
    publish.add_argument("--actor", required=True)
    publish.add_argument("--scene", required=True, type=Path)
    publish.add_argument("--review", required=True, type=Path)
    publish.add_argument("--score", required=True, type=Path)
    publish.add_argument("--output", required=True, type=Path)
    publish.add_argument("--note", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            initialize_workflow(args.job, args.state)
        elif args.command == "status":
            command_status(args)
        elif args.command == "capability":
            command_capability(args)
        elif args.command == "stage":
            command_stage(args)
        elif args.command == "open-issue":
            command_open_issue(args)
        elif args.command == "patch":
            command_patch(args)
        elif args.command == "review":
            command_review(args)
        elif args.command == "restore":
            command_restore(args)
        elif args.command == "publish":
            command_publish(args)
        else:
            raise WorkflowError(f"unknown command: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError, WorkflowError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
