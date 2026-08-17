import sys
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

import render_scene_evidence_overlay as overlay  # noqa: E402


class ProposalExplanationTest(unittest.TestCase):
    def test_aligned_covered_candidate_is_explained(self):
        candidate = {
            "id": "proposal-a",
            "suggestedCenterline": {"start": [0.0, 0.03], "end": [4.0, 0.03]},
        }
        result = overlay._proposal_explanation(
            candidate,
            [(np.asarray([0.0, 0.0]), np.asarray([4.0, 0.0]))],
            set(),
        )
        self.assertTrue(result["explained"])
        self.assertEqual(result["coverageRatio"], 1.0)

    def test_perpendicular_or_distant_candidate_is_not_explained(self):
        candidate = {
            "id": "proposal-b",
            "suggestedCenterline": {"start": [0.0, 1.0], "end": [0.0, 5.0]},
        }
        result = overlay._proposal_explanation(
            candidate,
            [(np.asarray([0.0, 0.0]), np.asarray([4.0, 0.0]))],
            set(),
        )
        self.assertFalse(result["explained"])
        self.assertLess(result["coverageRatio"], 0.60)

    def test_source_binding_records_deliberate_partial_use(self):
        candidate = {
            "id": "proposal-c",
            "suggestedCenterline": {"start": [0.0, 0.0], "end": [4.0, 0.0]},
        }
        result = overlay._proposal_explanation(candidate, [], {"proposal-c"})
        self.assertTrue(result["explained"])
        self.assertTrue(result["sourceBound"])


if __name__ == "__main__":
    unittest.main()
