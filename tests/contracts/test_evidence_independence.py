from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, CORE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


identity_api = load("execution_identity")
api = load("scene_api")


def identity(actor: str, run_id: str, role: str, policy_id: str, reviewer_class: str | None = None):
    value = {
        "schemaVersion": "1.0",
        "actorId": actor,
        "runId": run_id,
        "role": role,
        "provider": "contract-test",
        "model": "deterministic-fixture",
        "policyId": policy_id,
        "toolPolicyHash": identity_api.policy_digest(policy_id),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "contract-test", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


AUTHOR = identity(
    "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
)
REVIEWER = identity(
    "reviewer-east", "22222222-2222-4222-8222-222222222222",
    "reviewer", "reviewer-readonly-v1", "regional",
)


class EvidenceIndependenceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scene_path = self.root / "scene-authority.json"
        self.scene = api.new_scene("pr-c", 3.0, 0.0, AUTHOR["actorId"], AUTHOR)
        self.level = api.default_level_id(self.scene)
        api.op_create_wall(
            self.scene,
            {
                "id": "wall_host", "level": self.level, "start": [0, 0], "end": [6, 0],
                "height": 3.0, "thickness": 0.12, "execution": AUTHOR,
            },
            AUTHOR["actorId"],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, name: str, content: bytes) -> str:
        path = self.root / name
        path.write_bytes(content)
        return name

    def _attach(self, node_id: str, path: str, role: str):
        return api.op_attach_evidence(
            self.scene,
            self.scene_path,
            {
                "id": node_id,
                "type": role,
                "sourceRole": role,
                "path": path,
                "producer": "contract-fixture",
            },
        )

    def test_same_bytes_under_two_paths_do_not_count_as_independent(self) -> None:
        first = self._write("first.bin", b"same evidence bytes")
        second = self._write("second.bin", b"same evidence bytes")
        self._attach("wall_host", first, "elevation")
        self._attach("wall_host", second, "photo")
        with self.assertRaisesRegex(api.SceneError, "INFERRED_NEEDS_TWO_DISTINCT_SOURCES"):
            api.op_accept(
                self.scene,
                self.scene_path,
                {
                    "id": "wall_host", "mode": "inferred",
                    "reviewerIdentity": REVIEWER, "reason": "two labels, one file",
                },
            )

    def test_python_claim_payload_matches_cross_runtime_fixture(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "scene-v2" / "claim-payload-fixture.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(api.claim_payload(fixture["scene"], "wall_a"), fixture["wallClaim"])

    def _attach_derived(
        self,
        name: str,
        content: bytes,
        roots: list[str],
        producer: str,
        generator_parameters=None,
    ) -> None:
        path = self._write(name, content)
        receipt_name = f"{name}.provenance.json"
        receipt = {
            "outputContentSha256": hashlib.sha256(content).hexdigest(),
            "rootContentSha256s": roots,
            "producer": producer,
        }
        if generator_parameters is not None:
            receipt["generatorParameters"] = generator_parameters
        (self.root / receipt_name).write_text(json.dumps(receipt), encoding="utf-8")
        api.op_attach_evidence(
            self.scene,
            self.scene_path,
            {
                "id": "wall_host", "type": "derived", "path": path,
                "provenanceReceipt": receipt_name,
            },
        )

    def test_same_root_distinct_generators_count_as_independent(self) -> None:
        # A pure point-cloud capture derives everything from one LAS root.
        # Different derivation pipelines over that root are legal independent
        # lineages (SKILL: missing evidence lowers confidence, never blocks).
        root_digest = "f" * 64
        self._attach_derived("crop-a.bin", b"derived crop a", [root_digest], "elevation-renderer")
        self._attach_derived("crop-b.bin", b"derived crop b", [root_digest], "ortho-renderer")
        entry = api.op_accept(
            self.scene,
            self.scene_path,
            {
                "id": "wall_host", "mode": "inferred",
                "reviewerIdentity": REVIEWER, "reason": "two derivation pipelines of one capture",
            },
        )
        self.assertEqual(entry["status"], "accepted-inferred")
        lineages = {source["lineageId"] for source in entry["sources"]}
        self.assertEqual(len(lineages), 2)

    def test_same_generator_different_parameters_count_as_independent(self) -> None:
        root_digest = "f" * 64
        self._attach_derived(
            "band-low.bin", b"low band slice", [root_digest],
            "pointcloud-evidence", {"band": [0.3, 0.9]},
        )
        self._attach_derived(
            "band-high.bin", b"high band slice", [root_digest],
            "pointcloud-evidence", {"band": [1.8, 2.4]},
        )
        entry = api.op_accept(
            self.scene,
            self.scene_path,
            {
                "id": "wall_host", "mode": "inferred",
                "reviewerIdentity": REVIEWER, "reason": "distinct height bands of one capture",
            },
        )
        self.assertEqual(entry["status"], "accepted-inferred")

    def test_overlapping_roots_alone_no_longer_reject(self) -> None:
        self._attach_derived("derived-a.bin", b"derived a", ["a" * 64, "b" * 64], "generator-a")
        self._attach_derived("derived-b.bin", b"derived b", ["a" * 64, "c" * 64], "generator-b")
        entry = api.op_accept(
            self.scene,
            self.scene_path,
            {
                "id": "wall_host", "mode": "inferred",
                "reviewerIdentity": REVIEWER, "reason": "distinct generators over overlapping roots",
            },
        )
        self.assertEqual(entry["status"], "accepted-inferred")

    def test_same_generator_same_parameters_rerun_is_rejected(self) -> None:
        # Anti-cheat baseline: re-running one generator with identical
        # parameters yields the same lineage fingerprint even when output
        # bytes differ (timestamps, compression), and must not count twice.
        root_digest = "f" * 64
        self._attach_derived(
            "run-1.bin", b"first run bytes", [root_digest],
            "pointcloud-evidence", {"band": [0.3, 0.9]},
        )
        self._attach_derived(
            "run-2.bin", b"second run bytes", [root_digest],
            "pointcloud-evidence", {"band": [0.3, 0.9]},
        )
        with self.assertRaisesRegex(api.SceneError, "INFERRED_NEEDS_TWO_DISTINCT_SOURCES"):
            api.op_accept(
                self.scene,
                self.scene_path,
                {
                    "id": "wall_host", "mode": "inferred",
                    "reviewerIdentity": REVIEWER, "reason": "same pipeline run twice",
                },
            )

    def test_copied_file_under_second_receipt_is_rejected(self) -> None:
        # Anti-cheat baseline: identical bytes stay one source no matter how
        # the receipts describe them.
        root_digest = "f" * 64
        self._attach_derived("copy-1.bin", b"identical payload", [root_digest], "generator-a")
        self._attach_derived("copy-2.bin", b"identical payload", [root_digest], "generator-b")
        with self.assertRaisesRegex(api.SceneError, "INFERRED_NEEDS_TWO_DISTINCT_SOURCES"):
            api.op_accept(
                self.scene,
                self.scene_path,
                {
                    "id": "wall_host", "mode": "inferred",
                    "reviewerIdentity": REVIEWER, "reason": "one file copied twice",
                },
            )

    def test_handwritten_source_without_hash_cannot_be_accepted_as_measured(self) -> None:
        path = self._write("manual-source.bin", b"manual source")
        self.scene["evidence"]["wall_host"]["sources"].append({
            "type": "elevation", "path": path,
        })
        with self.assertRaisesRegex(api.SceneError, "EVIDENCE_HASH_STALE"):
            api.op_accept(
                self.scene,
                self.scene_path,
                {"id": "wall_host", "mode": "measured", "reviewerIdentity": REVIEWER},
            )

    def test_distinct_content_and_lineage_accepts_and_binds_claim(self) -> None:
        first = self._write("elevation.bin", b"elevation evidence")
        second = self._write("photo.bin", b"photo evidence")
        self._attach("wall_host", first, "elevation")
        self._attach("wall_host", second, "photo")
        entry = api.op_accept(
            self.scene,
            self.scene_path,
            {
                "id": "wall_host", "mode": "inferred",
                "reviewerIdentity": REVIEWER, "reason": "independent geometry and photo lineages",
            },
        )
        self.assertEqual(entry["status"], "accepted-inferred")
        self.assertEqual(entry["claimHash"], api.claim_hash(self.scene, "wall_host"))
        self.assertEqual(entry["claimSnapshot"], api.claim_payload(self.scene, "wall_host"))
        self.assertEqual(entry["reviewer"]["runId"], REVIEWER["runId"])

    def test_direct_geometry_tamper_is_rejected_by_claim_snapshot_validation(self) -> None:
        receipt = self._write("wall-elevation.bin", b"wall elevation")
        self._attach("wall_host", receipt, "elevation")
        api.op_accept(
            self.scene,
            self.scene_path,
            {"id": "wall_host", "mode": "measured", "reviewerIdentity": REVIEWER},
        )
        self.scene["nodes"]["wall_host"]["end"] = [7, 0]
        with self.assertRaisesRegex(api.SceneError, "EVIDENCE_CLAIM_SNAPSHOT_STALE"):
            api.validate_scene(self.scene)

    def test_host_geometry_change_invalidates_accepted_opening(self) -> None:
        api.op_add_opening(
            self.scene,
            "door",
            {
                "id": "door_a", "wall": "wall_host", "offset": 2.0, "width": 0.9,
                "height": 2.1, "execution": AUTHOR,
            },
            AUTHOR["actorId"],
        )
        receipt = self._write("door-elevation.bin", b"door elevation")
        self._attach("door_a", receipt, "elevation")
        api.op_accept(
            self.scene,
            self.scene_path,
            {"id": "door_a", "mode": "measured", "reviewerIdentity": REVIEWER},
        )
        api.op_update_node(self.scene, {"id": "wall_host", "updates": {"height": 2.8}})
        self.assertEqual(self.scene["evidence"]["door_a"]["status"], "candidate")
        self.assertNotIn("claimHash", self.scene["evidence"]["door_a"])

    def test_non_geometry_meta_note_does_not_invalidate_claim(self) -> None:
        receipt = self._write("wall-elevation.bin", b"wall elevation")
        self._attach("wall_host", receipt, "elevation")
        entry = api.op_accept(
            self.scene,
            self.scene_path,
            {"id": "wall_host", "mode": "measured", "reviewerIdentity": REVIEWER},
        )
        old_claim = entry["claimHash"]
        api.op_update_node(self.scene, {"id": "wall_host", "updates": {"meta.note": "review note"}})
        self.assertEqual(self.scene["evidence"]["wall_host"]["status"], "accepted-measured")
        self.assertEqual(self.scene["evidence"]["wall_host"]["claimHash"], old_claim)

    def test_created_identity_fields_are_protected(self) -> None:
        with self.assertRaisesRegex(api.SceneError, "PROTECTED_FIELD"):
            api.op_update_node(
                self.scene, {"id": "wall_host", "updates": {"meta.createdBy": "attacker"}},
            )


if __name__ == "__main__":
    unittest.main()
