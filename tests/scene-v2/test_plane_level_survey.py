import sys
import unittest
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scene-core"))

from plane_level_survey import cell_low_surface, robust_plane  # noqa: E402


class PlaneLevelSurveyTest(unittest.TestCase):
    def test_recovers_tilted_floor_with_furniture_and_low_outliers(self):
        rng = np.random.default_rng(42)
        x = rng.uniform(-4.0, 10.0, 80_000)
        y = rng.uniform(-5.0, 9.5, 80_000)
        floor = 0.028 * x - 0.017 * y - 2.61
        z = floor + rng.normal(0.0, 0.006, len(x))
        furniture = rng.random(len(x)) < 0.32
        z[furniture] += rng.uniform(0.25, 1.2, int(furniture.sum()))
        low_outlier = rng.random(len(x)) < 0.002
        z[low_outlier] -= rng.uniform(0.08, 0.35, int(low_outlier.sum()))

        cell_x, cell_y, cell_z = cell_low_surface(
            x, y, z, cell_size_m=0.16, percentile=10.0,
        )
        coefficients, mask, residual = robust_plane(cell_x, cell_y, cell_z, seed=7)

        self.assertAlmostEqual(coefficients[0], 0.028, delta=0.003)
        self.assertAlmostEqual(coefficients[1], -0.017, delta=0.003)
        self.assertAlmostEqual(coefficients[2], -2.61, delta=0.02)
        self.assertGreater(float(mask.mean()), 0.80)
        self.assertLess(float(np.percentile(np.abs(residual[mask]), 90)), 0.025)


if __name__ == "__main__":
    unittest.main()
