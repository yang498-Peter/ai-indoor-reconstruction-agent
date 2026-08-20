"""End-to-end gate test for the Semantic Scene V2 MCP stdio server.

The server is driven exactly like a real MCP client would: a child process
speaking newline-delimited JSON-RPC 2.0 over stdin/stdout. Reads go through a
background thread and a queue because select() cannot poll pipes on Windows,
and a blocking readline on a crashed server would hang the whole suite.
"""

from __future__ import annotations

import importlib.util
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

import laspy
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "scene-core" / "mcp_server.py"
POLICIES = json.loads((REPO_ROOT / "schemas" / "tool-policies-v1.json").read_text(encoding="utf-8"))["policies"]

RESPONSE_TIMEOUT_S = 20.0

EXPECTED_TOOLS = {
    "init_scene", "create_wall", "add_door", "add_window", "add_opening", "add_item",
    "add_slab", "add_ceiling", "add_zone", "add_column", "update_node", "delete_node",
    "attach_evidence", "open_issue", "apply_patch", "find_nodes",
    "measure", "get_scene_summary", "get_node", "validate_scene", "undo", "get_agent_guide",
    "propose_wall", "refine_wall_line", "submit_semantic_observations",
}


def policy_digest(policy_id: str) -> str:
    raw = json.dumps(POLICIES[policy_id], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def identity(actor, run_id, role, policy, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "mcp-test", "model": "fixture", "policyId": policy,
        "toolPolicyHash": policy_digest(policy),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "mcp-test", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR_IDENTITY = identity(
    "mcp-test", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
)
REVIEWER_IDENTITY = identity(
    "reviewer-east", "22222222-2222-4222-8222-222222222222",
    "reviewer", "reviewer-readonly-v1", "regional",
)


class McpServerTestCase(unittest.TestCase):
    """Spawns one server per test against a throwaway scene directory."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene_path = self.root / "scene.json"
        self.next_id = 0
        self.stderr_lines: list[str] = []

        self._start_server(AUTHOR_IDENTITY)

    def _start_server(self, identity_value: dict) -> None:
        self.identity_path = self.root / f"identity-{identity_value['actorId']}.json"
        self.identity_path.write_text(json.dumps(identity_value), encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--scene", str(self.scene_path), "--identity", str(self.identity_path)],
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

    def _stop_server(self) -> None:
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
    def restart_server(self, identity_value: dict) -> None:
        self._stop_server()
        self.responses = queue.Queue()
        self.stderr_lines = []
        self._start_server(identity_value)

    def tearDown(self) -> None:
        self._stop_server()
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

        self.restart_server(REVIEWER_IDENTITY)
        self.handshake()
        payload, is_error = self.call_tool("accept_node", {
            "id": "wall_west", "mode": "measured",
        })
        self.assertTrue(is_error)
        self.assertIn("MEASURED_NEEDS_VERIFIED_SOURCE", payload["message"])

        self.restart_server(AUTHOR_IDENTITY)
        self.handshake()
        (self.root / "west-slice.json").write_text(json.dumps({"kind": "slice"}), encoding="utf-8")
        self.call_ok("attach_evidence", {
            "id": "wall_west", "type": "high-structure-slice", "path": "west-slice.json",
            "producer": "mcp-test",
        })

        self.restart_server(REVIEWER_IDENTITY)
        self.handshake()
        entry = self.call_ok("accept_node", {
            "id": "wall_west", "mode": "measured",
        })
        self.assertEqual(entry["status"], "accepted-measured")

        # Editing accepted geometry demotes it back to candidate.
        self.restart_server(AUTHOR_IDENTITY)
        self.handshake()
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


def _load_core(name: str):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scene-core" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wall_capture_index(root: Path) -> Path:
    """One double-faced wall (faces y=2.0 / y=2.2, x 0..6) as a capture index."""
    rows: list[tuple[float, float, float]] = []
    z_levels = np.linspace(0.55, 2.35, 14)
    for x in np.arange(0.0, 6.0 + 1e-9, 0.05):
        for y in (2.0, 2.2):
            rows.extend((float(x), y, float(z)) for z in z_levels)
    xyz = np.asarray(rows, dtype=np.float64)
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.asarray([0.001, 0.001, 0.001])
    las_path = root / "wall.las"
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(las_path)
    index_root = root / "capture-index"
    _load_core("capture_index").build_index(las_path, index_root, tile_size_m=2.0)
    return index_root


class WallFitToolsTest(McpServerTestCase):
    """propose_wall / refine_wall_line measure against raw points, never write."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture_temp.cleanup)
        cls.index_root = _wall_capture_index(Path(cls.fixture_temp.name))

    def test_propose_wall_refines_rough_line_without_touching_the_scene(self):
        self.handshake()
        result = self.call_ok("propose_wall", {
            "index": str(self.index_root),
            "start": [0.4, 2.33], "end": [5.6, 2.33],
            "floor_z": 0.0,
        })
        self.assertFalse(result["written"])
        proposal = result["proposal"]
        self.assertEqual(proposal["status"], "FIT_OK", proposal.get("reason"))
        nearest_face = min(abs(proposal["start"][1] - 2.0), abs(proposal["start"][1] - 2.2))
        self.assertLess(nearest_face, 0.05)
        self.assertTrue(proposal["doubleSided"]["detected"])
        self.assertAlmostEqual(proposal["doubleSided"]["thicknessM"], 0.2, delta=0.04)
        # Measurement only: no scene file may appear as a side effect.
        self.assertFalse(self.scene_path.exists())

    def test_refine_wall_line_reports_deviation_and_leaves_disk_untouched(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-fit-001"})
        self.call_ok("create_wall", {
            "id": "wall_a", "start": [0.2, 2.1], "end": [5.8, 2.1], "thickness": 0.2,
        })
        before = self.scene_path.read_bytes()
        result = self.call_ok("refine_wall_line", {
            "id": "wall_a", "index": str(self.index_root), "floor_z": 0.0,
        })
        self.assertFalse(result["written"])
        self.assertEqual(result["wallId"], "wall_a")
        self.assertEqual(result["refinement"]["status"], "FIT_OK")
        # Stored centerline 2.1 sits between the faces: the paired-centerline
        # deviation must be near zero even though the face deviation is ~0.1.
        self.assertLess(abs(result["centerlineDeviation"]["midLateralM"]), 0.05)
        self.assertAlmostEqual(abs(result["faceDeviation"]["midLateralM"]), 0.1, delta=0.05)
        self.assertEqual(self.scene_path.read_bytes(), before)

    def test_refine_wall_line_rejects_a_missing_wall(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-fit-002"})
        payload, is_error = self.call_tool("refine_wall_line", {
            "id": "not_a_wall", "index": str(self.index_root), "floor_z": 0.0,
        })
        self.assertTrue(is_error)
        self.assertIn("HOST_NOT_A_WALL", payload["message"])

    def test_propose_wall_surfaces_capture_index_errors(self):
        self.handshake()
        payload, is_error = self.call_tool("propose_wall", {
            "index": str(self.root / "no-such-index"),
            "start": [0.0, 0.0], "end": [5.0, 0.0],
            "floor_z": 0.0,
        })
        self.assertTrue(is_error)
        self.assertIn("CAPTURE_INDEX_ERROR", payload["message"])
        self.assertFalse(self.scene_path.exists())


def _look_at_c2w(position, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """OpenGL camera-to-world: camera looks along -Z, +Y is up."""
    position = np.asarray(position, dtype=np.float64)
    forward = np.asarray(target, dtype=np.float64) - position
    forward = forward / np.linalg.norm(forward)
    z_axis = -forward
    x_axis = np.cross(forward, np.asarray(up, dtype=np.float64))
    x_axis = x_axis / np.linalg.norm(x_axis)
    c2w = np.eye(4)
    c2w[:3, 0] = x_axis
    c2w[:3, 1] = np.cross(z_axis, x_axis)
    c2w[:3, 2] = z_axis
    c2w[:3, 3] = position
    return c2w


class SemanticObservationToolsTest(McpServerTestCase):
    """submit_semantic_observations grounds pixel boxes, never writes."""

    DOOR = {"center": 2.0, "width": 0.9, "head": 2.05}
    FACE_Y = 1.95  # wall centerline y=2.0, thickness 0.1, camera on the -y side

    def _write_transforms(self) -> Path:
        path = self.root / "transforms.json"
        path.write_text(json.dumps({
            "undistort_camera_model": {
                "width": 400, "height": 400,
                "intrinsic": [[300, 0, 200], [0, 300, 200], [0, 0, 1]],
            },
            "frames": [{
                "file_path": "left\\1.jpg", "timestamp": 1,
                "transform_matrix": _look_at_c2w((3.0, -3.0, 1.3), (3.0, 2.0, 1.3)).tolist(),
            }],
        }), encoding="utf-8")
        return path

    def _door_bbox(self) -> list[float]:
        pp = _load_core("photo_projection")
        frame = pp.load_frames(self.root / "transforms.json")[0]
        x0 = self.DOOR["center"] - self.DOOR["width"] / 2
        x1 = self.DOOR["center"] + self.DOOR["width"] / 2
        corners = np.array([
            [x0, self.FACE_Y, 0.0], [x1, self.FACE_Y, 0.0],
            [x1, self.FACE_Y, self.DOOR["head"]], [x0, self.FACE_Y, self.DOOR["head"]],
        ])
        uv, _, in_front = pp.project_points(frame, corners)
        self.assertTrue(in_front.all())
        return [float(uv[:, 0].min()), float(uv[:, 1].min()),
                float(uv[:, 0].max()), float(uv[:, 1].max())]

    def _observations(self, bbox: list[float]) -> dict:
        return {
            "schemaVersion": "1.0",
            "captureFingerprint": "mcp-fixture-capture",
            "observations": [{
                "frameId": "transforms.json#0", "bbox": bbox, "label": "door",
                "labelConfidence": 0.9, "observer": "mcp-test-vlm",
            }],
        }

    def test_observation_grounds_to_a_candidate_without_writing(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-semantic-001"})
        self.call_ok("create_wall", {
            "id": "wall_face", "start": [0, 2], "end": [6, 2], "thickness": 0.1, "height": 2.7,
        })
        transforms = self._write_transforms()
        before = self.scene_path.read_bytes()
        result = self.call_ok("submit_semantic_observations", {
            "observations": self._observations(self._door_bbox()),
            "transforms": str(transforms),
            "ground_z": 0.0,
        })
        self.assertFalse(result["written"])
        report = result["report"]
        self.assertEqual(report["counts"], {
            "observations": 1, "candidates": 1, "corroborated": 0, "unresolved": 0,
        })
        candidate = report["candidates"][0]
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["coordinateSource"], "ray-cast-estimate")
        self.assertTrue(candidate["requiresGeometryConfirmation"])
        self.assertEqual(candidate["hostWallId"], "wall_face")
        self.assertLess(abs(candidate["hostOffsetM"] - self.DOOR["center"]), 0.15)
        self.assertLess(abs(candidate["widthM"] - self.DOOR["width"]), 0.15)
        # Limited permissions: semantic-only confidence stays low, the label
        # confidence never leaks through as a coordinate confidence.
        self.assertLessEqual(candidate["confidence"], 0.4)
        # Measurement only: the scene byte-for-byte unchanged, no node added.
        self.assertEqual(self.scene_path.read_bytes(), before)
        self.assertEqual(self.call_ok("get_scene_summary")["nodeCounts"], {"level": 1, "wall": 1})

    def test_invalid_payload_is_rejected_fail_closed(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-semantic-002"})
        transforms = self._write_transforms()
        bad = self._observations([10, 10, 20, 20])
        # World coordinates smuggled next to the pixel box must be refused.
        bad["observations"][0]["worldXYZ"] = [1.0, 2.0, 3.0]
        payload, is_error = self.call_tool("submit_semantic_observations", {
            "observations": bad, "transforms": str(transforms), "ground_z": 0.0,
        })
        self.assertTrue(is_error)
        self.assertIn("OBSERVATIONS_INVALID", payload["message"])

    def test_missing_transforms_is_a_tool_error(self):
        self.handshake()
        self.call_ok("init_scene", {"dataset": "mcp-semantic-003"})
        payload, is_error = self.call_tool("submit_semantic_observations", {
            "observations": self._observations([10, 10, 20, 20]),
            "transforms": str(self.root / "no-transforms.json"),
            "ground_z": 0.0,
        })
        self.assertTrue(is_error)
        self.assertIn("TRANSFORMS_MISSING", payload["message"])


if __name__ == "__main__":
    unittest.main()
