"""Single fail-closed command surface for the reconstruction workflow."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .repository import RepositoryLayoutError, repository_root


def _work_file(work: Path, name: str) -> Path:
    return work.resolve() / name


def _run_python(relative_script: str, arguments: Sequence[str]) -> int:
    command = [sys.executable, str(repository_root() / relative_script), *arguments]
    return subprocess.run(command, check=False).returncode


def _add_work(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--work", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indoor-recon")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Discover one capture and initialize its nine-stage job")
    init.add_argument("--capture", required=True, type=Path)
    init.add_argument("--work", required=True, type=Path)
    init.add_argument("--profile", default="default-office-v1")
    init.add_argument("--point-cloud")
    init.add_argument("--length-unit", choices=("metre", "foot", "us-survey-foot"), required=True)
    init.add_argument("--up-axis", choices=("Z",), default="Z")
    init.add_argument("--coordinate-reference", required=True)
    init.add_argument("--scene-domain", choices=("indoor", "outdoor", "mixed", "unknown"), default="indoor")
    init.add_argument("--scaffold", type=Path)

    for name in ("status", "next"):
        command = commands.add_parser(name)
        _add_work(command)
        command.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="Return the next typed action; never auto-accept a stage")
    _add_work(run)
    run.add_argument("--until", default="macro-hypothesis")
    run.add_argument("--json", action="store_true")

    patch = commands.add_parser("patch")
    _add_work(patch)
    patch.add_argument("--patch", required=True, type=Path)
    patch.add_argument("--dry-run", action="store_true")
    patch.add_argument("--scene", type=Path)
    patch.add_argument("--identity", type=Path, required=True)
    patch.add_argument("--actor", required=True)

    review = commands.add_parser("review")
    _add_work(review)
    review.add_argument("--issue", required=True)
    review.add_argument("--scene", type=Path)
    review.add_argument("--identity", type=Path, required=True)
    review.add_argument("--actor", required=True)
    review.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    review.add_argument("--score", required=True, type=int)
    review.add_argument("--evidence", action="append", default=[])
    review.add_argument("--note", required=True)

    evaluate = commands.add_parser("evaluate")
    _add_work(evaluate)
    evaluate.add_argument("--scene", type=Path)
    evaluate.add_argument("--review", type=Path)
    evaluate.add_argument("--output", type=Path)

    publish = commands.add_parser("publish")
    _add_work(publish)
    publish.add_argument("--output", required=True, type=Path)
    publish.add_argument("--scene", type=Path)
    publish.add_argument("--review", type=Path)
    publish.add_argument("--quality-report", type=Path)
    publish.add_argument("--identity", type=Path, required=True)
    publish.add_argument("--actor", required=True)
    publish.add_argument("--bundle-file", action="append", default=[])
    publish.add_argument("--note", default="")

    mcp = commands.add_parser("mcp", help="Run the official MCP SDK stdio server")
    mcp.add_argument("--scene", required=True, type=Path)
    mcp.add_argument("--identity", required=True, type=Path)

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--output", required=True, type=Path)
    benchmark.add_argument("--baseline", type=Path)
    return parser


def _command_init(args: argparse.Namespace) -> int:
    scaffold = args.scaffold or (repository_root() / "templates" / "minimal-viewer")
    forwarded = [
        "--data", str(args.capture), "--work", str(args.work), "--scaffold", str(scaffold),
        "--profile", args.profile,
        "--length-unit", args.length_unit, "--up-axis", args.up_axis,
        "--coordinate-reference", args.coordinate_reference, "--scene-domain", args.scene_domain,
    ]
    if args.point_cloud:
        forwarded.extend(("--point-cloud", args.point_cloud))
    return _run_python(
        ".codex/skills/reconstruct-indoor-scene/scripts/init_reconstruction_job.py", forwarded
    )


def _workflow(args: Sequence[str]) -> int:
    return _run_python(
        ".codex/skills/reconstruct-indoor-scene/scripts/reconstruction_loop.py", args
    )


def _workflow_status(state_path: Path) -> tuple[dict[str, object] | None, int]:
    command = [
        sys.executable,
        str(repository_root() / ".codex/skills/reconstruct-indoor-scene/scripts/reconstruction_loop.py"),
        "status",
        "--state",
        str(state_path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(json.dumps({
            "ok": False,
            "error": {
                "code": "WORKFLOW_STATUS_FAILED",
                "message": "workflow status command returned non-zero",
                "retryable": False,
                "nextActions": [],
            },
        }, ensure_ascii=False))
        return None, result.returncode
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("nextActions"), list):
        raise ValueError("WORKFLOW_STATUS_INVALID: missing nextActions")
    return payload, 0


def _command_next(args: argparse.Namespace) -> int:
    state_path = _work_file(args.work, "pipeline-state.json")
    status, returncode = _workflow_status(state_path)
    if status is None:
        return returncode
    stages = status.get("stages", {})
    current_stage = next(
        (
            name
            for name, value in stages.items()
            if isinstance(value, dict) and value.get("status") != "PASS"
        ),
        None,
    ) if isinstance(stages, dict) else None
    response = {
        "ok": True,
        "state": str(state_path),
        "currentStage": current_stage,
        "nextActions": status["nextActions"],
        "executed": False,
    }
    if args.command == "run":
        response["requestedUntil"] = args.until
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


def _command_patch(args: argparse.Namespace) -> int:
    payload = json.loads(args.patch.read_text(encoding="utf-8"))
    operations = payload.get("ops") if isinstance(payload, dict) else payload
    if not isinstance(operations, list):
        raise ValueError("PATCH_INVALID: expected an operation list or an object with ops")
    forbidden = {"accept", "reject", "transition_issue"}.intersection(
        {str(row.get("op")) for row in operations if isinstance(row, dict)}
    )
    if forbidden:
        raise ValueError("REVIEW_VERDICT_BATCH_FORBIDDEN: use dedicated reviewer commands")
    scene = (args.scene or _work_file(args.work, "scene-authority.json")).resolve()
    from .repository import load_repo_module

    api = load_repo_module("scene_api", "scene-core/scene_api.py")
    current = api.load_scene(scene)
    expected = payload.get("baseGeometryDigest") if isinstance(payload, dict) else None
    actual = api.geometry_digest(current)
    if expected and expected != actual:
        raise ValueError("PATCH_BASE_STALE: baseGeometryDigest does not match current authority")
    scene_api = repository_root() / "scene-core" / "scene_api.py"
    command = [
        sys.executable, str(scene_api), "--scene", str(scene), "--actor", args.actor,
        "--identity", str(args.identity), "apply-patch", "--patch", str(args.patch),
    ]
    if args.dry_run:
        from copy import deepcopy

        identity = api.execution_identity_api.load_identity(args.identity.resolve())
        preview = deepcopy(current)
        api.apply_ops(preview, scene, operations, args.actor, identity)
        api.validate_scene(preview)
        print(json.dumps({"ok": True, "dryRun": True, "baseGeometryDigest": actual,
                          "newGeometryDigest": api.geometry_digest(preview)}, ensure_ascii=False))
        return 0
    if isinstance(payload, dict):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".ops.json", delete=False
        ) as stream:
            json.dump(operations, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            normalized = Path(stream.name)
        command[-1] = str(normalized)
        try:
            return subprocess.run(command, check=False).returncode
        finally:
            normalized.unlink(missing_ok=True)
    return subprocess.run(command, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return _command_init(args)
        if args.command == "status":
            return _workflow(["status", "--state", str(_work_file(args.work, "pipeline-state.json"))])
        if args.command in {"next", "run"}:
            # `run` intentionally stops at the typed next action; the Agent owns
            # execution and no stage can become PASS through this convenience command.
            return _command_next(args)
        if args.command == "patch":
            return _command_patch(args)
        if args.command == "review":
            forwarded = [
                "review", "--state", str(_work_file(args.work, "pipeline-state.json")),
                "--actor", args.actor, "--execution", str(args.identity), "--issue", args.issue,
                "--scene", str(args.scene or _work_file(args.work, "scene-authority.json")),
                "--verdict", args.verdict, "--score", str(args.score), "--note", args.note,
            ]
            for evidence in args.evidence:
                forwarded.extend(("--evidence", evidence))
            return _workflow(forwarded)
        if args.command == "evaluate":
            return _run_python("scene-core/quality_report_v2.py", [
                "--scene", str(args.scene or _work_file(args.work, "scene-authority.json")),
                "--review", str(args.review or _work_file(args.work, "review-receipt.json")),
                "--output", str(args.output or _work_file(args.work, "quality-report.json")),
            ])
        if args.command == "publish":
            forwarded = [
                "publish", "--state", str(_work_file(args.work, "pipeline-state.json")),
                "--actor", args.actor, "--execution", str(args.identity),
                "--scene", str(args.scene or _work_file(args.work, "scene-authority.json")),
                "--review", str(args.review or _work_file(args.work, "review-receipt.json")),
                "--quality-report", str(args.quality_report or _work_file(args.work, "quality-report.json")),
                "--output", str(args.output), "--note", args.note,
            ]
            for item in args.bundle_file:
                forwarded.extend(("--bundle-file", item))
            return _workflow(forwarded)
        if args.command == "mcp":
            from .mcp.server import main as mcp_main

            return mcp_main(["--scene", str(args.scene), "--identity", str(args.identity)])
        if args.command == "benchmark":
            from .benchmark import benchmark_main

            return benchmark_main(args.output, args.baseline)
    except OSError as error:
        message = f"IO_FAILED:{error.strerror or error.__class__.__name__}"
        print(json.dumps({"ok": False, "error": {"code": "IO_FAILED",
                                                   "message": message, "retryable": False,
                                                   "nextActions": []}}, ensure_ascii=False))
        return 2
    except (ValueError, json.JSONDecodeError, RepositoryLayoutError) as error:
        print(json.dumps({"ok": False, "error": {"code": str(error).split(":", 1)[0],
                                                   "message": str(error), "retryable": False,
                                                   "nextActions": []}}, ensure_ascii=False))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
