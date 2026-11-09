from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class PackagingAndCliContractTest(unittest.TestCase):
    def test_project_is_locked_and_exposes_the_single_cli(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["name"], "indoor-reconstruction-agent")
        self.assertEqual(
            pyproject["project"]["scripts"]["indoor-recon"],
            "indoor_reconstruction.cli:main",
        )
        self.assertTrue(any(value.startswith("mcp==2.") for value in pyproject["project"]["dependencies"]))
        lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
        self.assertIn('name = "mcp"', lock)
        self.assertIn('name = "indoor-reconstruction-agent"', lock)

    def test_cli_exposes_the_documented_workflow(self) -> None:
        from indoor_reconstruction.cli import build_parser

        parser = build_parser()
        subcommands = next(
            action.choices
            for action in parser._actions
            if getattr(action, "choices", None)
        )
        self.assertEqual(
            set(subcommands),
            {"init", "status", "next", "run", "patch", "review", "evaluate", "publish", "mcp", "benchmark"},
        )

    def test_next_returns_one_typed_action_without_execution(self) -> None:
        from indoor_reconstruction import cli

        status = {
            "stages": {"intake": {"status": "PASS"}, "macro-hypothesis": {"status": "REVIEW"}},
            "nextActions": ["measure the macro hypothesis"],
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            cli, "_workflow_status", return_value=(status, 0)
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = cli.main(["next", "--work", temp, "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["currentStage"], "macro-hypothesis")
        self.assertEqual(payload["nextActions"], ["measure the macro hypothesis"])
        self.assertFalse(payload["executed"])

    def test_next_failure_does_not_expose_the_work_path(self) -> None:
        from indoor_reconstruction import cli

        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = cli.main(["next", "--work", temp, "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(returncode, 2)
        self.assertEqual(payload["error"]["code"], "WORKFLOW_STATUS_FAILED")
        self.assertNotIn(temp, output.getvalue())

    def test_init_forwards_the_bound_topology_profile(self) -> None:
        from indoor_reconstruction import cli

        with mock.patch.object(cli, "_run_python", return_value=0) as runner:
            returncode = cli.main([
                "init", "--capture", "capture", "--work", "work",
                "--profile", "cleanroom-v1", "--length-unit", "metre",
                "--coordinate-reference", "local-test-frame",
            ])
        self.assertEqual(returncode, 0)
        forwarded = runner.call_args.args[1]
        self.assertEqual(forwarded[forwarded.index("--profile") + 1], "cleanroom-v1")

    def test_patch_cannot_batch_a_reviewer_verdict(self) -> None:
        from indoor_reconstruction import cli

        with tempfile.TemporaryDirectory() as temp:
            patch = Path(temp) / "patch.json"
            patch.write_text(json.dumps({"ops": [{"op": "accept", "id": "wall_1"}]}), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                returncode = cli.main([
                    "patch", "--work", temp, "--patch", str(patch),
                    "--identity", str(Path(temp) / "identity.json"), "--actor", "author",
                ])
        payload = json.loads(output.getvalue())
        self.assertEqual(returncode, 2)
        self.assertEqual(payload["error"]["code"], "REVIEW_VERDICT_BATCH_FORBIDDEN")


class CodexDiscoveryContractTest(unittest.TestCase):
    def test_repo_skill_and_layered_agents_are_discoverable(self) -> None:
        skill = ROOT / ".agents" / "skills" / "reconstruct-indoor-scene" / "SKILL.md"
        root_agents = ROOT / "AGENTS.md"
        core_agents = ROOT / "scene-core" / "AGENTS.md"
        for path in (skill, root_agents, core_agents):
            self.assertTrue(path.is_file(), path)
            self.assertNotIn("\ufffd", path.read_text(encoding="utf-8"))
        text = skill.read_text(encoding="utf-8")
        self.assertIn("indoor-recon status", text)
        self.assertIn("indoor-recon next", text)
        self.assertLess(len(text.encode("utf-8")), 8 * 1024)

    def test_portability_checker_uses_the_canonical_skill_path(self) -> None:
        text = (ROOT / "scripts" / "validate_portable_repo.py").read_text(encoding="utf-8")
        self.assertIn('.agents/skills/reconstruct-indoor-scene/SKILL.md', text)
        self.assertNotIn('ROOT / ".codex/skills/reconstruct-indoor-scene/SKILL.md"', text)


class OfficialMcpContractTest(unittest.TestCase):
    def test_server_uses_official_sdk_with_structured_results(self) -> None:
        from mcp import Client

        from indoor_reconstruction.mcp.server import create_server

        identity = {
            "schemaVersion": "1.0",
            "actorId": "wp8-author",
            "runId": "11111111-1111-4111-8111-111111111111",
            "role": "author",
            "provider": "deterministic-checker",
            "model": "none",
            "startedAt": "2026-08-21T00:00:00+00:00",
            "toolPolicyHash": "0" * 64,
            "policyId": "author-v1",
        }

        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as temp:
                server = create_server(Path(temp) / "scene.json", identity)
                async with Client(server) as client:
                    tools = await client.list_tools()
                    names = [tool.name for tool in tools.tools]
                    self.assertEqual(names, sorted(names))
                    result = await client.call_tool("get_agent_guide", {})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["ok"], True)
                    self.assertEqual(result.structured_content["tool"], "get_agent_guide")
                    error = await client.call_tool("does_not_exist", {})
                    self.assertTrue(error.is_error)

        asyncio.run(exercise())

    def test_legacy_entrypoint_no_longer_implements_protocol_by_hand(self) -> None:
        text = (ROOT / "scene-core" / "mcp_server.py").read_text(encoding="utf-8")
        self.assertNotIn("PROTOCOL_VERSION", text)
        self.assertNotIn("def handle_message(", text)
        self.assertIn("indoor_reconstruction.mcp.server", text)


class AutomationAndBenchmarkContractTest(unittest.TestCase):
    def test_ci_matrix_and_codex_review_are_separate_gates(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("matrix:", ci)
        self.assertIn("ubuntu-latest", ci)
        self.assertIn("windows-latest", ci)
        self.assertIn("3.11", ci)
        self.assertIn("3.12", ci)
        self.assertIn("deterministic-checks:", ci)
        self.assertIn("needs: [deterministic-python, node-contract, lint-type-package]", ci)
        self.assertNotIn("OPENAI_API_KEY", ci)
        self.assertNotIn("openai/codex-action", ci)
        # External paid review is an unconfigured capability, not a reason to
        # send code during ordinary CI or invent a successful reviewer receipt.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("NOT_RUN", readme)
        self.assertFalse((ROOT / ".github/workflows/codex-review.yml").exists())

    def test_quick_benchmark_emits_typed_fail_closed_report(self) -> None:
        from indoor_reconstruction.benchmark import run_quick_benchmark

        report = run_quick_benchmark()
        self.assertEqual(report["artifactType"], "benchmark-report")
        self.assertEqual(report["schemaVersion"], "1.0")
        self.assertEqual(report["suite"], "synthetic-wall-v1")
        self.assertIn(report["status"], {"PASS", "REVIEW", "FAIL"})
        self.assertEqual(report["thresholds"]["maxKeyMetricRegressionRatio"], 0.02)
        self.assertEqual(report["thresholds"]["maxPerformanceRegressionRatio"], 0.15)
        self.assertEqual(report["gates"]["keyMetrics"], "PASS")
        self.assertEqual(report["gates"]["performanceRegression"], "NOT_RUN")
        self.assertIn("PERFORMANCE_BASELINE_NOT_PROVIDED", report["limitations"])
        self.assertIn("REAL_CAPTURE_NOT_PROVIDED", report["limitations"])
        self.assertRegex(report["payloadDigest"], r"^[0-9a-f]{64}$")
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
