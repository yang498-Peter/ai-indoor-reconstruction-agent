"""End-to-end gate test for the Semantic Scene V2 MCP stdio server.

The server is driven exactly like a real MCP client would: a child process
speaking newline-delimited JSON-RPC 2.0 over stdin/stdout. Reads go through a
background thread and a queue because select() cannot poll pipes on Windows,
and a blocking readline on a crashed server would hang the whole suite.
"""

from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "scene-core" / "mcp_server.py"

RESPONSE_TIMEOUT_S = 20.0

EXPECTED_TOOLS = {
    "init_scene", "create_wall", "add_door", "add_window", "add_opening", "add_item",
    "add_slab", "add_ceiling", "add_zone", "add_column", "update_node", "delete_node",
    "attach_evidence", "accept_node", "reject_node", "open_issue", "transition_issue", "apply_patch", "find_nodes",
    "measure", "get_scene_summary", "get_node", "validate_scene", "undo", "get_agent_guide",
}


class McpServerTestCase(unittest.TestCase):
    """Spawns one server per test against a throwaway scene directory."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene_path = self.root / "scene.json"
        self.next_id = 0
        self.stderr_lines: list[str] = []

        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--scene", str(self.scene_path), "--actor", "mcp-test"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.responses: queue.Queue[str] = queue.Queue()
        self.readers = [
            threading.Thread(target=self._pump, args=(self.process.stdout, self.responses.put), daemon=True),
            threading.Thread(target=self._pump, args=(self.process.stderr, self.stderr_lines.append), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def tearDown(self) -> None:
        # Closing stdin is the documented shutdown for an MCP stdio server; kill
        # only if it refuses, so a hung server surfaces as a slow test not a leak.
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()
            self.process.wait(timeout=10)
        for reader in self.readers:
            reader.join(timeout=5)
        # Popen leaves the pipe wrappers open; close them once the readers hit
        # EOF so the suite does not accumulate ResourceWarnings per test.
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self.temp.cleanup()

    @staticmethod
    def _pump(stream, sink) -> None:
        if stream is None:
            return
        for line in stream:
            sink(line.rstrip("\r\n"))

    # -- transport helpers -------------------------------------------------

    def _send(self, payload: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_response(self) -> dict:
        try:
            line = self.responses.get(timeout=RESPONSE_TIMEOUT_S)
        except queue.Empty:
            self.fail(
                f"no response within {RESPONSE_TIMEOUT_S}s; "
                f"exit={self.process.poll()} stderr={self.stderr_lines}"
            )
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message_id = self.next_id
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        response = self._read_response()
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), message_id)
        return response

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[dict, bool]:
        """Return (decoded tool payload, isError). Errors carry a raw text message."""
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", response, f"tools/call {name} failed at the protocol layer")
        result = response["result"]
        self.assertEqual(result["content"][0]["type"], "text")
        text = result["content"][0]["text"]
        is_error = bool(result["isError"])
        return ({"message": text} if is_error else json.loads(text)), is_error

    def call_ok(self, name: str, arguments: dict | None = None) -> dict:
        payload, is_error = self.call_tool(name, arguments)
        self.assertFalse(is_error, f"tool {name} unexpectedly failed: {payload}")
        return payload["result"]

    def handshake(self) -> dict:
        response = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0"},
        })
        self.notify("notifications/initialized")
        return response["result"]

    def disk_scene(self) -> dict:
        return json.loads(self.scene_path.read_text(encoding="utf-8"))


class HandshakeTest(McpServerTestCase):
    def test_initialize_advertises_tools_capability(self):
        result = self.handshake()
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(result["serverInfo"]["name"])
        self.assertTrue(result["serverInfo"]["version"])

    def test_tools_list_matches_expected_surface(self):
        self.handshake()
        tools = self.request("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(len(tools), len(EXPECTED_TOOLS))
        self.assertEqual(names, EXPECTED_TOOLS)
        for tool in tools:
            self.assertTrue(tool["description"], f"{tool['name']} has no description")
            self.assertEqual(tool["inputSchema"]["type"], "object")
        by_name = {tool["name"]: tool for tool in tools}
        # The offset semantics are the single most misread argument; keep them documented.
        self.assertIn("offset", by_name["add_door"]["inputSchema"]["properties"])
        self.assertIn("CENTER", by_name["add_door"]["inputSchema"]["properties"]["offset"]["description"])
        self.assertIn("meters", by_name["create_wall"]["description"])

    def test_ping_and_unknown_method(self):
        self.handshake()
        self.assertEqual(self.request("ping")["result"], {})
        response = self.request("scene/teleport")
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_reported_as_tool_error(self):
        self.handshake()
        payload, is_error = self.call_tool("demolish_building")
        self.assertTrue(is_error)
        self.assertIn("UNKNOWN_TOOL", payload["message"])

    def test_agent_guide_documents_the_invariants(self):
        self.handshake()
        guide = self.call_ok("get_agent_guide")["guide"]
        self.assertIn("meters", guide)
        self.assertIn("attach_evidence", guide)
        self.assertIn("reviewer", guide)


class AuthoringWorkflowTest(McpServerTestCase):
    def test_full_workflow_with_failed_gate_and_undo(self):
        self.handshake()

        init = self.call_ok("init_scene", {"dataset": "mcp-capture-001", "level_height": 3.0})
        self.assertTrue(init["levelId"].startswith("level_"))
        self.assertTrue(self.scene_path.is_file())

        wall = self.call_ok("create_wall", {
            "id": "wall_south", "start": [0, 0], "end": [8, 0], "thickness": 0.2, "height": 3.0,
        })
        self.assertEqual(wall["id"], "wall_south")
        self.assertAlmostEqual(self.call_ok("measure", {"id": "wall_south"})["lengthM"], 8.0)

        door = self.call_ok("add_door", {
            "id": "door_main", "wall": "wall_south", "offset": 2.0, "width": 0.95, "height": 2.05,
        })
        self.assertEqual(door["parentId"], "wall_south")
        self.assertAlmostEqual(door["hostOffsetM"], 2.0)

        before = self.disk_scene()
        self.assertIn("door_main", before["nodes"])

        # Gate: a door centered past the wall end must be refused with the scene intact.
        payload, is_error = self.call_tool("add_door", {
            "id": "door_offwall", "wall": "wall_south", "offset": 20.0, "width": 1.0, "height": 2.0,
        })
        self.assertTrue(is_error)
        self.assertIn("OPENING_OUTSIDE_WALL", payload["message"])
        after = self.disk_scene()
        self.assertNotIn("door_offwall", after["nodes"])
        self.assertEqual(after["nodes"].keys(), before["nodes"].keys())
        self.assertEqual(after["revision"]["counter"], before["revision"]["counter"])

        summary = self.call_ok("get_scene_summary")
        self.assertEqual(summary["nodeCounts"], {"level": 1, "wall": 1, "door": 1})
        self.assertEqual(summary["evidenceStatuses"], {"candidate": 2})
        self.assertEqual(summary["dataset"], "mcp-capture-001")

        node = self.call_ok("get_node", {"id": "door_main"})
        self.assertEqual(node["node"]["type"], "door")
        self.assertEqual(node["evidence"]["status"], "candidate")
        self.assertTrue(self.call_ok("validate_scene")["valid"])

        walls = self.call_ok("find_nodes", {"type": "wall"})
        self.assertEqual([row["id"] for row in walls], ["wall_south"])

        # Undo drops the door write and restores the wall-only revision.
        restored = self.call_ok("undo")
        self.assertEqual(restored["restoredRevision"], before["revision"]["counter"] - 1)
        rolled_back = self.disk_scene()
        self.assertNotIn("door_main", rolled_back["nodes"])
        self.assertIn("wall_south", rolled_back["nodes"])
        self.assertEqual(self.call_ok("get_scene_summary")["nodeCounts"], {"level": 1, "wall": 1})

    def test_init_scene_refuses_to_overwrite(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-capture-002"})
        payload, is_error = self.call_tool("init_scene", {"dataset": "mcp-capture-002"})
        self.assertTrue(is_error)
        self.assertIn("SCENE_EXISTS", payload["message"])

    def test_apply_patch_is_atomic_over_the_wire(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-capture-003"})
        payload, is_error = self.call_tool("apply_patch", {"ops": [
            {"op": "create_wall", "id": "wall_ok", "start": [0, 0], "end": [5, 0], "thickness": 0.12},
            {"op": "add_door", "id": "door_bad", "wall": "wall_ok", "offset": 9.0, "width": 1.0, "height": 2.0},
        ]})
        self.assertTrue(is_error)
        self.assertIn("OPENING_OUTSIDE_WALL", payload["message"])
        self.assertNotIn("wall_ok", self.disk_scene()["nodes"])

        applied = self.call_ok("apply_patch", {"ops": [
            {"op": "create_wall", "id": "wall_ok", "start": [0, 0], "end": [5, 0], "thickness": 0.12},
            {"op": "add_door", "id": "door_ok", "wall": "wall_ok", "offset": 2.5, "width": 1.0, "height": 2.0},
        ]})
        self.assertEqual(applied["applied"], 2)
        self.assertIn("door_ok", self.disk_scene()["nodes"])

    def test_evidence_and_acceptance_gates_survive_the_wrapper(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-capture-004"})
        self.call_ok("create_wall", {"id": "wall_west", "start": [0, 0], "end": [0, 6], "thickness": 0.12})

        payload, is_error = self.call_tool("accept_node", {
            "id": "wall_west", "mode": "measured", "reviewer": "reviewer-east",
        })
        self.assertTrue(is_error)
        self.assertIn("MEASURED_NEEDS_VERIFIED_SOURCE", payload["message"])

        (self.root / "west-slice.json").write_text(json.dumps({"kind": "slice"}), encoding="utf-8")
        self.call_ok("attach_evidence", {
            "id": "wall_west", "type": "high-structure-slice", "path": "west-slice.json",
        })

        # The server actor authored the wall, so it may not review its own work.
        payload, is_error = self.call_tool("accept_node", {
            "id": "wall_west", "mode": "measured", "reviewer": "mcp-test",
        })
        self.assertTrue(is_error)
        self.assertIn("SELF_REVIEW_FORBIDDEN", payload["message"])

        entry = self.call_ok("accept_node", {
            "id": "wall_west", "mode": "measured", "reviewer": "reviewer-east",
        })
        self.assertEqual(entry["status"], "accepted-measured")

        # Editing accepted geometry demotes it back to candidate.
        self.call_ok("update_node", {"id": "wall_west", "updates": {"height": 2.85}})
        self.assertEqual(self.call_ok("get_node", {"id": "wall_west"})["evidence"]["status"], "candidate")

    def test_delete_cascades_to_hosted_openings(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-capture-005"})
        self.call_ok("create_wall", {"id": "wall_north", "start": [0, 4], "end": [6, 4], "thickness": 0.12})
        self.call_ok("add_window", {
            "id": "window_n1", "wall": "wall_north", "offset": 3.0, "width": 1.2, "height": 1.4,
        })
        deleted = self.call_ok("delete_node", {"id": "wall_north"})["deleted"]
        self.assertIn("wall_north", deleted)
        self.assertIn("window_n1", deleted)
        self.assertEqual(self.call_ok("get_scene_summary")["nodeCounts"], {"level": 1})


if __name__ == "__main__":
    unittest.main()
