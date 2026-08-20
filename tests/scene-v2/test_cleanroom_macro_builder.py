import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

import cleanroom_macro_builder as macro  # noqa: E402
import cleanroom_scene_builder as base  # noqa: E402
import scene_api  # noqa: E402


class PanoramaCompletionTest(unittest.TestCase):
    def test_coherent_completion_extends_dividers_and_connects_rear_envelope(self):
        authored = [{
            "start": np.asarray([0.0, 0.0]), "end": np.asarray([10.0, 0.0]),
            "role": "north-room-glass-spine", "residualP90M": 0.06,
            "supportPointCount": 100, "sourceProposalIds": ["front"],
        }]
        proposals = {"wallCandidates": [
            {"id": "divider-a", "suggestedCenterline": {"start": [3.0, 0.0], "end": [3.0, 2.0]},
             "confidence": 0.7, "fitResidualP90M": 0.05, "supportPointCount": 40},
            {"id": "rear-a", "suggestedCenterline": {"start": [0.0, 3.0], "end": [4.0, 3.0]},
             "confidence": 0.7, "fitResidualP90M": 0.05, "supportPointCount": 40},
            {"id": "rear-b", "suggestedCenterline": {"start": [6.0, 3.2], "end": [10.0, 3.2]},
             "confidence": 0.7, "fitResidualP90M": 0.05, "supportPointCount": 40},
        ]}
        payload = {"panoramaRoomBand": {
            "enabled": True, "sourceRole": "north-room-glass-spine", "roomSide": "left",
            "dividerProposalIds": ["divider-a"], "rearProposalIds": ["rear-a", "rear-b"],
            "pierWidthM": 0.2, "coherentCompletion": {
                "enabled": True, "closeBandEnds": True,
                "rearEnvelopeStatus": "accepted-inferred", "sidewallStatus": "accepted-inferred",
                "rawRearStatus": "rejected",
            },
            "evidence": {"path": "panorama/test.jpg", "sha256": "a" * 64, "observation": "room band"},
        }}

        result, enabled = macro._panorama_room_band(payload, authored, proposals)

        self.assertTrue(enabled)
        rear = next(wall for wall in result if wall["role"] == "panorama-inferred-rear-envelope")
        self.assertEqual(rear["presentationStatus"], "accepted-inferred")
        self.assertEqual(rear["authorityStatus"], "candidate")
        self.assertAlmostEqual(rear["start"][1], 3.1, places=6)
        divider = next(wall for wall in result if wall["role"] == "panorama-inferred-full-divider-1")
        self.assertAlmostEqual(divider["end"][1], 3.1, places=6)
        self.assertEqual(sum("sidewall" in wall["role"] for wall in result), 2)
        raw_rear = [wall for wall in result if wall["role"].startswith("panorama-rear-glazing-")]
        self.assertTrue(raw_rear)
        self.assertTrue(all(wall["presentationStatus"] == "rejected" for wall in raw_rear))
        spaces = macro._north_room_topology(result)
        self.assertEqual(len(spaces), 2)
        self.assertTrue(all(len(space["boundaryNodeIds"]) == 4 for space in spaces))


class _StubQuery:
    def __init__(self):
        empty = np.zeros(0, dtype=np.float64)
        self.x, self.y, self.z = empty, empty, empty
        self.rgb = np.zeros((0, 3), dtype=np.uint8)
        self.point_count = 0
        self.stats = {}


class _RecordingIndex:
    """Records query_bbox z windows; returns no points."""

    def __init__(self):
        self.manifest = {"indexedPointCount": 1_000}
        self.calls = []

    def query_bbox(self, min_x, min_y, max_x, max_y, z_min, z_max, every):
        self.calls.append({
            "min_x": float(min_x), "min_y": float(min_y),
            "max_x": float(max_x), "max_y": float(max_y),
            "z_min": float(z_min), "z_max": float(z_max),
        })
        return _StubQuery()


class PlaneRelativeWindowTest(unittest.TestCase):
    """The raw z windows must follow the floor plane, not an absolute datum."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.u = np.asarray([1.0, 0.0])
        self.v = np.asarray([0.0, 1.0])
        self.plane = {"a": 0.01, "b": -0.02, "c": 5.0}

    def tearDown(self):
        self.temp.cleanup()

    def _corner_z(self, call):
        return [
            macro._plane_z(self.plane, x, y)
            for x in (call["min_x"], call["max_x"])
            for y in (call["min_y"], call["max_y"])
        ]

    def test_furniture_band_window_is_derived_from_floor_plane(self):
        index = _RecordingIndex()
        envelope = {"uMin": 0.0, "uMax": 10.0, "vMin": 0.0, "vMax": 8.0}
        items = macro._furniture_items(index, self.plane, self.u, self.v, envelope, self.root)
        self.assertEqual(items, [])
        call = index.calls[0]
        corner_z = self._corner_z(call)
        self.assertAlmostEqual(call["z_min"], min(corner_z) + 0.66 - 0.05, places=6)
        self.assertAlmostEqual(call["z_max"], max(corner_z) + 0.88 + 0.05, places=6)
        # With floor.c = 5.0 an absolute window near zero would be empty.
        self.assertGreater(call["z_min"], 4.0)

    def test_lrpc_export_window_is_derived_from_floor_plane(self):
        index = _RecordingIndex()
        polygon = [[0.0, 0.0], [10.0, 0.0], [10.0, 8.0], [0.0, 8.0]]
        envelope = {"uMin": 0.0, "uMax": 10.0, "vMin": 0.0, "vMax": 8.0}
        meta = macro._export_lrpc(
            index, self.root / "cloud.lrpc", polygon, envelope,
            self.u, self.v, self.plane, np.asarray([5.0, 4.0]), 1_000,
        )
        self.assertEqual(meta["pointCount"], 0)
        call = index.calls[0]
        corner_z = self._corner_z(call)
        self.assertAlmostEqual(call["z_min"], min(corner_z) - 0.18 - 0.10, places=6)
        self.assertAlmostEqual(call["z_max"], max(corner_z) + 3.45 + 0.10, places=6)
        self.assertGreater(call["z_min"], 4.0)


class FurnitureEvidenceTest(unittest.TestCase):
    def test_missing_author_photo_is_omitted_not_fabricated(self):
        sources = macro._furniture_evidence_sources("f" * 64, None, "c" * 64)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["type"], "tabletop-height-support")
        self.assertNotIn("undistort", str(sources))
        self.assertEqual(sources[0]["rootContentSha256s"], ["c" * 64])
        self.assertEqual(sources[0]["contentSha256"], "f" * 64)

    def test_author_supplied_photo_is_included_with_its_own_root(self):
        picks = {"furnitureEvidence": {"path": "photos/desk-family.jpg", "sha256": "9" * 64}}
        sources = macro._furniture_evidence_sources("f" * 64, picks, "c" * 64)
        self.assertEqual(len(sources), 2)
        photo = sources[1]
        self.assertEqual(photo["type"], "posed-photo-family")
        self.assertEqual(photo["producer"], "author-picks")
        self.assertEqual(photo["rootContentSha256s"], ["9" * 64])
        self.assertNotEqual(sources[0]["lineageId"], photo["lineageId"])


def _line_candidate(cid, start, end, thickness=0.12, support=40):
    return {
        "id": cid, "suggestedCenterline": {"start": list(start), "end": list(end)},
        "lengthM": float(np.hypot(end[0] - start[0], end[1] - start[1])),
        "confidence": 0.7, "fitResidualP90M": 0.05,
        "supportPointCount": support, "thicknessM": thickness,
    }


class WallConsolidationTest(unittest.TestCase):
    BOUNDS = {"minX": -10.0, "maxX": 10.0, "minY": -10.0, "maxY": 10.0}

    def _consolidate(self, candidates):
        return base._consolidate_walls(
            {"axisFamilies": [{"angleDeg": 0.0}], "wallCandidates": candidates},
            self.BOUNDS,
        )

    def test_parallel_walls_one_partition_apart_stay_separate(self):
        walls = self._consolidate([
            _line_candidate("near", (0.0, 0.0), (4.0, 0.0), support=500),
            _line_candidate("far", (0.0, 0.12), (4.0, 0.12), support=20),
        ])
        self.assertEqual(len(walls), 2)
        offsets = sorted(round(float(wall["start"][1]), 4) for wall in walls)
        # A weld would have dragged the shared offset toward the heavy wall.
        self.assertEqual(offsets, [0.0, 0.12])

    def test_doorway_sized_gap_is_bridged_but_recorded(self):
        walls = self._consolidate([
            _line_candidate("left", (0.0, 0.0), (2.0, 0.0)),
            _line_candidate("right", (2.9, 0.0), (5.0, 0.0)),
        ])
        self.assertEqual(len(walls), 1)
        self.assertAlmostEqual(float(walls[0]["start"][0]), 0.0, places=6)
        self.assertAlmostEqual(float(walls[0]["end"][0]), 5.0, places=6)
        self.assertEqual(walls[0]["bridgedGaps"], [
            {"alongStartM": 2.0, "alongEndM": 2.9, "widthM": 0.9},
        ])

    def test_scan_breakage_gap_bridges_silently(self):
        walls = self._consolidate([
            _line_candidate("left", (0.0, 0.0), (2.0, 0.0)),
            _line_candidate("right", (2.15, 0.0), (4.0, 0.0)),
        ])
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0]["bridgedGaps"], [])
        self.assertNotIn("mergeSpreadM", walls[0])

    def test_offset_spread_above_review_threshold_is_recorded(self):
        walls = self._consolidate([
            _line_candidate("a", (0.0, 0.0), (4.0, 0.0)),
            _line_candidate("b", (0.0, 0.07), (4.0, 0.07)),
        ])
        self.assertEqual(len(walls), 1)
        self.assertAlmostEqual(walls[0]["mergeSpreadM"], 0.07, places=4)


class EvidenceProvenanceTest(unittest.TestCase):
    def test_relative_scene_path_survives_bundle_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "geometry-workspace" / "evidence" / "band-walls.png"
            output = root / "scene-out"
            self.assertEqual(
                base._relative_scene_path(target, output),
                "../geometry-workspace/evidence/band-walls.png",
            )

    def test_scene_layers_sources_are_provenance_complete(self):
        wall = {
            "start": np.asarray([0.0, 0.0]), "end": np.asarray([4.0, 0.0]),
            "thickness": 0.12, "confidence": 0.8, "residualP90M": 0.05,
            "supportPointCount": 500, "sourceProposalIds": ["p1"],
            "pairedFaceSupport": True,
        }
        authority, hypothesis, presentation = base._scene_layers(
            "test-set", [wall], [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]], [],
            0.0, 3.0, {},
            "../ws/evidence/band-walls.png", "a" * 64, "b" * 64, [],
            proposal_path="../ws/structural-proposals.json", root_sha256="c" * 64,
        )
        for scene in (authority, hypothesis, presentation):
            sources = scene["evidence"]["wall_clean001"]["sources"]
            self.assertEqual(len(sources), 2)
            for source in sources:
                # quality_report_v2 provenance contract: both hash keys plus
                # lineage, roots and producer must be present.
                self.assertEqual(source["sha256"], source["contentSha256"])
                self.assertEqual(source["rootContentSha256s"], ["c" * 64])
                self.assertTrue(source["producer"])
                self.assertEqual(source["lineageId"], scene_api.evidence_lineage_id(
                    source["rootContentSha256s"], source["producer"],
                    source.get("generatorParameters"),
                ))
            self.assertNotEqual(sources[0]["lineageId"], sources[1]["lineageId"])
            self.assertTrue(scene_api.has_two_independent_sources(sources))


if __name__ == "__main__":
    unittest.main()
