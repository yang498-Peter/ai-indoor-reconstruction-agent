"""Brand framing preserves source pixels and rejects unusable artwork."""
import hashlib
import importlib.util
from pathlib import Path

from PIL import Image
import pytest

SCRIPT = Path(__file__).parents[1] / ".agents/skills/reconstruct-indoor-scene/scripts/inspect_brand_asset.py"
spec = importlib.util.spec_from_file_location("brand_asset", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_padded_artwork_bounds_and_source_immutability(tmp_path):
    path = tmp_path / "padded.png"
    picture = Image.new("RGBA", (100, 80))
    picture.paste((240, 0, 0, 255), (20, 10, 80, 50))
    picture.save(path)
    original = path.read_bytes()
    result = tool.inspect_asset(path)
    assert result["alphaBoundsPixels"] == [20, 10, 80, 50]
    assert result["artworkAspectRatio"] == 1.5
    assert result["uvBoundsBottomLeftOrigin"] == [0.2, 0.375, 0.8, 0.875]
    assert result["sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_empty_alpha_is_rejected(tmp_path):
    path = tmp_path / "empty.png"
    Image.new("RGBA", (8, 8)).save(path)
    with pytest.raises(ValueError, match="fully transparent"):
        tool.inspect_asset(path)


def test_opaque_background_is_not_silently_removed(tmp_path):
    path = tmp_path / "opaque.png"
    Image.new("RGB", (20, 10), "white").save(path)
    result = tool.inspect_asset(path)
    assert result["fullyOpaque"]
    assert result["alphaBoundsPixels"] == [0, 0, 20, 10]
