from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
VIEWER = REPO / "prototypes" / "litereality-three-redraw-20260812" / "viewer.js"


def test_presentation_stats_separate_authority_from_display_geometry() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    assert "freshAuthorityReview?.acceptedMeasuredWallCount" in source
    assert "authorityDisplayStructures" in source
    assert "authorityCount} / ${displayCount" in source


def test_point_cloud_artifact_is_relative_to_scene_file() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    assert "const sceneUrl = new URL(scenePath, window.location.href)" in source
    assert "new URL(pointCloudArtifact, sceneUrl)" in source
