import sys
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

import audit_structural_omissions as audit  # noqa: E402


class HeightSupportClassificationTest(unittest.TestCase):
    def test_full_height_run_advances_to_local_review(self):
        result = audit._classify_height_support(
            np.asarray([4] * 12), np.asarray([5] * 12), np.asarray([3] * 12), 0.12,
        )
        self.assertEqual(result["disposition"], "LOCAL_ELEVATION_REVIEW")
        self.assertAlmostEqual(result["longestFullHeightRunM"], 1.44)

    def test_middle_only_furniture_return_is_withheld(self):
        result = audit._classify_height_support(
            np.asarray([0] * 20), np.asarray([8] * 20), np.asarray([0] * 20), 0.12,
        )
        self.assertEqual(result["disposition"], "WITHHOLD_NON_FULL_HEIGHT")
        self.assertEqual(result["fullHeightCoverageRatio"], 0.0)


if __name__ == "__main__":
    unittest.main()
