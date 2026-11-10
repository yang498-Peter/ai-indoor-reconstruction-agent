"""Resource identity is tied to the installed package, never to the caller CWD."""
import hashlib
import json
from pathlib import Path

import pytest

from indoor_reconstruction import repository
from indoor_reconstruction.cli import DELEGATED_TOOLS

ROOT = Path(__file__).resolve().parents[1]


def test_checkout_selection_ignores_a_different_repository_in_cwd(tmp_path, monkeypatch):
    fake = tmp_path / 'other-checkout'
    (fake / 'schemas').mkdir(parents=True)
    (fake / 'scene-core').mkdir()
    (fake / 'schemas/pipeline-contract-v2.json').write_text('{}', encoding='utf-8')
    (fake / 'scene-core/scene_api.py').write_text('raise RuntimeError("WRONG_CHECKOUT")', encoding='utf-8')
    monkeypatch.chdir(fake)
    assert repository.repository_root() == ROOT


def test_bundled_resources_are_preferred_and_tampering_is_rejected(tmp_path, monkeypatch):
    package = tmp_path / 'site-packages/indoor_reconstruction'
    runtime = package / '_runtime'
    hashes = {}
    for name in ['schemas/pipeline-contract-v2.json', 'scene-core/scene_api.py']:
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (runtime / 'runtime-manifest.json').write_text(json.dumps({
        'format': 'indoor-recon-runtime-v1', 'files': hashes, 'bundleDigest': digest,
    }), encoding='utf-8')
    monkeypatch.setattr(repository, '__file__', str(package / 'repository.py'))
    assert repository.repository_root() == runtime
    (runtime / 'scene-core/scene_api.py').write_text('changed', encoding='utf-8')
    with pytest.raises(repository.RepositoryLayoutError, match='RUNTIME_RESOURCE_HASH_MISMATCH'):
        repository.repository_root()


def test_missing_installed_resources_never_fall_back_to_checkout_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, '__file__', str(tmp_path/'site-packages/indoor_reconstruction/repository.py'))
    monkeypatch.chdir(ROOT)
    with pytest.raises(repository.RepositoryLayoutError, match='RUNTIME_RESOURCES_MISSING'):
        repository.repository_root()


def test_runtime_loader_rejects_escape_without_exposing_path(tmp_path):
    with pytest.raises(repository.RepositoryLayoutError, match='RUNTIME_RESOURCE_UNAVAILABLE') as error:
        repository.load_repo_module('not_a_runtime_module', str(tmp_path/'outside.py'))
    assert str(tmp_path) not in str(error.value)


def test_distribution_allowlist_contains_all_public_tool_entrypoints():
    files = json.loads((ROOT/'runtime-files.json').read_text(encoding='utf-8'))['files']
    assert len(files) == len(set(files))
    assert set(DELEGATED_TOOLS.values()).issubset(files)
    assert {'schemas/tool-policies-v1.json', 'templates/minimal-viewer/index.html',
            'scene-core/scene_api.py', '.codex/skills/reconstruct-indoor-scene/scripts/reconstruction_loop.py'}.issubset(files)
    for relative in files:
        assert (ROOT/relative).is_file()
        assert not relative.startswith(('work/', 'outputs/', 'data/', 'services/'))
