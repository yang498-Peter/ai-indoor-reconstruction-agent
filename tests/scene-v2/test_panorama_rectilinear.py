import importlib.util
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scene-core" / "panorama_rectilinear.py"
SPEC = importlib.util.spec_from_file_location("panorama_rectilinear", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_center_pixel_tracks_requested_yaw() -> None:
    width = 720
    panorama = np.zeros((360, width, 3), dtype=np.uint8)
    panorama[:, :, 0] = np.arange(width, dtype=np.uint16)[None, :] % 256
    view = MODULE.render_view(
        panorama,
        yaw_degrees=90.0,
        horizontal_fov_degrees=90.0,
        width=101,
        height=101,
    )
    expected_x = int(0.75 * width) % 256
    assert abs(int(view[50, 50, 0]) - expected_x) <= 2


def test_rejects_invalid_fov() -> None:
    panorama = np.zeros((20, 40, 3), dtype=np.uint8)
    try:
        MODULE.render_view(panorama, yaw_degrees=0.0, horizontal_fov_degrees=180.0)
    except ValueError as error:
        assert "field of view" in str(error)
    else:
        raise AssertionError("invalid field of view was accepted")


def test_pitch_direction_and_pole_do_not_wrap() -> None:
    panorama = np.zeros((180, 360, 3), dtype=np.uint8)
    panorama[:, :, 0] = np.arange(180, dtype=np.uint8)[:, None]
    up = MODULE.render_view(panorama, yaw_degrees=0, pitch_degrees=30, width=101, height=101)
    down = MODULE.render_view(panorama, yaw_degrees=0, pitch_degrees=-30, width=101, height=101)
    assert abs(int(up[50, 50, 0]) - 60) <= 1
    assert abs(int(down[50, 50, 0]) - 120) <= 1

    panorama[:, :, :] = 0
    panorama[-4:, :, 2] = 255
    zenith = MODULE.render_view(panorama, yaw_degrees=0, pitch_degrees=90, width=101, height=101)
    assert int(zenith[50, 50, 2]) == 0


def test_longitude_wraps_at_seam() -> None:
    panorama = np.zeros((90, 180, 3), dtype=np.uint8)
    panorama[:, :, 1] = np.arange(180, dtype=np.uint8)[None, :]
    a = MODULE.render_view(panorama, yaw_degrees=-180, width=101, height=101)
    b = MODULE.render_view(panorama, yaw_degrees=180, width=101, height=101)
    assert np.array_equal(a, b)


@pytest.mark.parametrize("kwargs", [
    {"yaw_degrees": float("nan")},
    {"yaw_degrees": 0, "pitch_degrees": 91},
    {"yaw_degrees": 0, "horizontal_fov_degrees": float("inf")},
])
def test_rejects_nonfinite_or_invalid_angles(kwargs) -> None:
    with pytest.raises(ValueError):
        MODULE.render_view(np.zeros((20, 40, 3), dtype=np.uint8), **kwargs)


def test_unicode_paths_and_existing_output_are_preserved(tmp_path: Path) -> None:
    source = tmp_path / "全景.jpg"
    output = tmp_path / "输出" / "视图.jpg"
    ok, encoded = cv2.imencode(".jpg", np.full((40, 80, 3), 127, dtype=np.uint8))
    assert ok
    source.write_bytes(encoded.tobytes())
    before = source.read_bytes()
    MODULE.make_contact_sheet(source, output, yaws=[0, 90, 180], pitch=0, fov=90, width=30, height=20)
    result = output.read_bytes()
    assert cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR).shape == (40, 60, 3)
    with pytest.raises(FileExistsError):
        MODULE.make_contact_sheet(source, output, yaws=[0], pitch=0, fov=90, width=30, height=20)
    assert output.read_bytes() == result
    assert source.read_bytes() == before


def test_cli_rejects_output_collision_before_writing(tmp_path: Path) -> None:
    first = tmp_path / "room.jpg"
    second = tmp_path / "room-rectilinear.jpg"
    first.write_bytes(b"first original")
    second.write_bytes(b"second original")
    result = subprocess.run([sys.executable, str(SCRIPT), str(first), str(second), "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode != 0
    assert first.read_bytes() == b"first original"
    assert second.read_bytes() == b"second original"
    assert not (tmp_path / "room-rectilinear-rectilinear.jpg").exists()


def test_cli_rejects_duplicate_names_across_directories(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    first, second = a / "room.jpg", b / "room.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    output = tmp_path / "out"
    result = subprocess.run([sys.executable, str(SCRIPT), str(first), str(second), "--output-dir", str(output)], capture_output=True)
    assert result.returncode != 0
    assert not output.exists()


def test_cli_writes_single_view_to_fresh_directory(tmp_path: Path) -> None:
    source = tmp_path / "输入.png"
    ok, encoded = cv2.imencode(".png", np.full((40, 80, 3), 80, dtype=np.uint8))
    assert ok
    source.write_bytes(encoded.tobytes())
    output = tmp_path / "views"
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), "--output-dir", str(output),
                             "--yaws", "0", "--width", "30", "--height", "20"], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output_image = output / "输入-rectilinear.jpg"
    image = cv2.imdecode(np.frombuffer(output_image.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == (20, 30, 3)
