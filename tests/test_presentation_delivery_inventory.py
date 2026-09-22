"""Synthetic static bundles exercise release boundaries without a server."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".agents/skills/reconstruct-indoor-scene/scripts/delivery_inventory.py"
spec = importlib.util.spec_from_file_location("delivery_inventory", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def bundle(tmp_path):
    root = tmp_path / "site"
    (root / "ag").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "ag/index.html").write_text("<p>AG993</p>", encoding="utf-8")
    (root / "other/index.html").write_text("<p>其他模型</p>", encoding="utf-8")
    return root


def test_reordering_does_not_change_identity(tmp_path):
    before = tool.snapshot(bundle(tmp_path))
    after = {"schema": tool.SCHEMA, "files": dict(reversed(list(before["files"].items())))}
    assert tool.compare(before, after, []) == {
        "added": [], "changed": [], "removed": [], "unexpected": [], "matchesAllowedChanges": True,
    }


def test_local_change_preserves_other_models_and_detects_accidental_removal(tmp_path):
    root = bundle(tmp_path)
    before = tool.snapshot(root)
    (root / "ag/index.html").write_text("<p>MVP S1</p>", encoding="utf-8")
    (root / "ag/preview.svg").write_text("<svg/>", encoding="utf-8")
    allowed = ["ag/index.html", "ag/preview.svg"]
    result = tool.compare(before, tool.snapshot(root), allowed)
    assert result["matchesAllowedChanges"]
    assert result["changed"] == ["ag/index.html"]
    assert result["added"] == ["ag/preview.svg"]
    (root / "other/index.html").unlink()
    result = tool.compare(before, tool.snapshot(root), allowed)
    assert not result["matchesAllowedChanges"]
    assert result["removed"] == result["unexpected"] == ["other/index.html"]


def test_cli_detects_stale_or_extra_release_file(tmp_path, capsys):
    root = bundle(tmp_path)
    manifest = tmp_path / "inventory.json"
    assert tool.main(["snapshot", "--root", str(root), "--output", str(manifest)]) == 0
    args = ["verify", "--root", str(root), "--manifest", str(manifest)]
    assert tool.main(args) == 0
    (root / "ag/index.html").write_text("changed", encoding="utf-8")
    assert tool.main(args) == 1
    (root / "unexpected.txt").write_text("extra", encoding="utf-8")
    assert tool.main(args) == 1
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["changed"] == ["ag/index.html"]
    assert report["added"] == ["unexpected.txt"]


@pytest.mark.parametrize("path", [".", "", "../secret", "/absolute", "drive:/key", "ag\\file", "ag//file", "./file", "CON.txt", "ag/name. ", "a\nkey"])
def test_nonportable_paths_are_rejected(path):
    with pytest.raises(ValueError, match="INVALID_RELATIVE_PATH"):
        tool.portable_path(path)


def test_duplicate_case_malformed_records_and_duplicate_json_keys(tmp_path):
    data = tool.snapshot(bundle(tmp_path))
    data["files"]["AG/index.html"] = data["files"]["ag/index.html"]
    with pytest.raises(ValueError, match="CASE_COLLISION"):
        tool.validate_manifest(data)
    del data["files"]["AG/index.html"]
    data["files"]["ag/index.html"]["bytes"] = True
    with pytest.raises(ValueError, match="INVALID_FILE_RECORD"):
        tool.validate_manifest(data)
    manifest = tmp_path / "bad.json"
    manifest.write_text('{"schema": "a", "schema": "b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        tool.read_manifest(manifest)


def test_self_inclusion_is_rejected_without_writing(tmp_path):
    root = bundle(tmp_path)
    report = root / "inventory.json"
    with pytest.raises(SystemExit) as error:
        tool.main(["snapshot", "--root", str(root), "--output", str(report)])
    assert error.value.code == 2 and not report.exists()


def test_reparse_point_is_rejected():
    # A synthetic lstat covers Windows junction policy without admin symlink rights.
    from types import SimpleNamespace
    import stat

    class Junction:
        name = "linked-capture"

        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)

    with pytest.raises(ValueError, match="LINK_ENTRY"):
        tool.reject_link(Junction())


def test_compare_cli_requires_exact_allowlist(tmp_path, capsys):
    root = bundle(tmp_path)
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    tool.atomic_write(before, tool.snapshot(root))
    (root / "ag/index.html").write_text("new", encoding="utf-8")
    tool.atomic_write(after, tool.snapshot(root))
    args = ["compare", "--before", str(before), "--after", str(after)]
    assert tool.main(args) == 1
    assert tool.main(args + ["--allow", "ag/index.html"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["matchesAllowedChanges"]
