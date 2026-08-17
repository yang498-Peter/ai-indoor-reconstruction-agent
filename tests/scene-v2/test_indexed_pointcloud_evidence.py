from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def load_support():
    path = Path(__file__).with_name("test_geometry_pipeline.py")
    spec = importlib.util.spec_from_file_location("geometry_pipeline_test_support", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


support = load_support()
indexed_evidence = support.load("indexed_pointcloud_evidence")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class IndexedPointcloudEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.las = self.root / "room.las"
        self.origin_x, self.origin_y = support.synthetic_room(self.las)
        self.source_hash = sha256(self.las)
        self.index_root = self.root / "capture-index"
        support.capture_index.build_index(self.las, self.index_root, tile_size_m=2.0)
        self.index = support.capture_index.CaptureIndex.open(self.index_root, validate_source=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_overview_is_bound_to_index_and_does_not_reopen_source_for_writes(self):
        output = self.root / "overview"
        result = indexed_evidence.render_overview(
            self.index,
            output,
            cell=0.10,
            ground_cell=0.8,
            bands_text="low=0.20:1.00,walls=1.00:2.60",
        )
        self.assertEqual(sha256(self.las), self.source_hash)
        self.assertEqual(result["indexFingerprint"], self.index.manifest["indexFingerprint"])
        self.assertEqual(result["queryStats"]["indexPasses"], 2)
        self.assertTrue((output / "ortho-top.png").is_file())
        self.assertTrue((output / "band-walls.png").is_file())
        self.assertTrue((output / "evidence-manifest.json").is_file())

    def test_local_elevation_reads_only_intersecting_tiles(self):
        output = self.root / "elevation"
        result = indexed_evidence.render_elevation(
            self.index,
            output,
            [
                (self.origin_x + 0.5, self.origin_y),
                (self.origin_x + 1.5, self.origin_y),
            ],
            corridor_width=0.40,
            zrange=(0.2, 2.7),
            cell=0.04,
        )
        self.assertGreater(result["queryStats"]["pointsReturned"], 0)
        self.assertLess(
            result["queryStats"]["pointsRead"],
            self.index.manifest["indexedPointCount"],
        )
        self.assertEqual(result["indexFingerprint"], self.index.manifest["indexFingerprint"])
        self.assertTrue((output / "elevation.png").is_file())
        self.assertTrue((output / "elevation.json").is_file())


if __name__ == "__main__":
    unittest.main()
