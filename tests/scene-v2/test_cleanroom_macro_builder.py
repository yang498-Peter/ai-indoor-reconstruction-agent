import sys
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

import cleanroom_macro_builder as macro  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
