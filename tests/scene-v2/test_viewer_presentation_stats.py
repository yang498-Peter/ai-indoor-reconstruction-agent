from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PROTO = REPO / "prototypes" / "litereality-three-redraw-20260812"
VIEWER = PROTO / "viewer.js"
VIEWER_HTML = PROTO / "viewer.html"


def test_presentation_stats_separate_authority_from_display_geometry() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    assert "freshAuthorityReview?.acceptedMeasuredWallCount" in source
    assert "authorityDisplayStructures" in source
    assert "authorityCount} / ${displayCount" in source


def test_point_cloud_artifact_is_relative_to_scene_file() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    assert "const sceneUrl = new URL(scenePath, window.location.href)" in source
    assert "new URL(pointCloudArtifact, sceneUrl)" in source


def test_photo_align_mode_exists_with_pose_transform() -> None:
    """P2-2: photo/pano alignment view compiled into the viewer source."""
    source = VIEWER.read_text(encoding="utf-8")

    # entry/exit lifecycle and mode dataset used by browser checks
    assert "function enterPhotoAlign(" in source
    assert "function exitPhotoAlign(" in source
    assert "photoAlignMode" in source
    # scene-source -> display pose conversion (display = [x, elevation, -y])
    assert "SOURCE_TO_DISPLAY" in source
    assert "function photoPoseMatrix(" in source
    # pinhole FOV comes from intrinsics, not a hardcoded camera default
    assert "function photoVerticalFovDeg(" in source
    assert "intrinsics.height / 2) / intrinsics.flY" in source


def test_photo_align_supports_equirect_panoramas() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    # equirect branch: inside-out sphere plus levelled orientation default
    assert "'equirect'" in source
    assert "equirectangular" in source
    assert "geometry.scale(-1, 1, 1)" in source
    assert "applyPanoOrientation" in source
    assert "yawOffsetDeg" in source
    # frame markers with LOD budget for large photo sets
    assert "function buildPhotoMarkers(" in source
    assert "PHOTO_MARKER_BUDGET" in source


def test_photo_urls_resolve_relative_to_scene_json() -> None:
    """Photos follow the same URL rule as the LRPC point-cloud artifact."""
    source = VIEWER.read_text(encoding="utf-8")

    assert "function resolveSceneAsset(" in source
    assert "new URL(path, sceneBaseUrl)" in source
    assert "resolveSceneAsset(photo.path)" in source


def test_review_panel_mode_exists() -> None:
    """P2-5: unified review panel is a viewer mode, not a separate page."""
    source = VIEWER.read_text(encoding="utf-8")
    html = VIEWER_HTML.read_text(encoding="utf-8")

    assert "function renderReviewPanel(" in source
    assert "function reviewElements(" in source
    assert "function focusReviewElement(" in source
    assert "toggleReviewPanel" in source
    # status color contract for the four evidence states
    for status in ("accepted-measured", "accepted-inferred", "candidate", "rejected"):
        assert f"'{status}'" in source or f'"{status}"' in source
    # filters and evidence-source expansion
    assert "data-review-filter" in source
    assert "data-review-preview" in source
    assert 'id="review-panel"' in html
    assert 'id="review-mode-toggle"' in html


def test_new_ui_strings_are_bilingual() -> None:
    source = VIEWER.read_text(encoding="utf-8")

    for key in ("photoAlignEnter", "photoAlignExit", "photoOpacity", "reviewMode",
                "reviewOnlyCandidate", "reviewOnlyWarning", "reviewEvidenceSources"):
        assert source.count(f"{key}:") >= 2, f"i18n key {key} must exist in zh-CN and en"
