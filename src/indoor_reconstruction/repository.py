"""Resolve package-owned runtime resources without consulting the caller CWD."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


class RepositoryLayoutError(RuntimeError):
    """A required runtime resource is absent, conflicting, or corrupt."""


def repository_root() -> Path:
    package = Path(__file__).resolve().parent
    bundled = package / "_runtime"
    if bundled.is_dir():
        _verify_runtime(bundled)
        return bundled
    # Editable/source execution is bound to this module's checkout, never CWD.
    source = package.parent.parent
    if (source / "runtime-files.json").is_file() and (
        source / "scene-core/scene_api.py"
    ).is_file():
        return source
    raise RepositoryLayoutError(
        "RUNTIME_RESOURCES_MISSING: reinstall the indoor-reconstruction-agent distribution"
    )


def _verify_runtime(root: Path) -> None:
    try:
        manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        if not {"schemas/pipeline-contract-v2.json", "scene-core/scene_api.py"}.issubset(files):
            raise ValueError("required runtime resources missing")
        calculated = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if manifest["format"] != "indoor-recon-runtime-v1" or calculated != manifest["bundleDigest"]:
            raise ValueError("invalid manifest")
        for relative, expected in files.items():
            path = root / relative
            if (
                not isinstance(relative, str) or "\\" in relative or ":" in relative
                or Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("invalid resource path")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RepositoryLayoutError("RUNTIME_RESOURCE_HASH_MISMATCH:" + relative)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RepositoryLayoutError("RUNTIME_MANIFEST_INVALID: reinstall the distribution") from error


def load_repo_module(name: str, relative_path: str) -> ModuleType:
    root = repository_root()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RepositoryLayoutError("RUNTIME_RESOURCE_UNAVAILABLE: required module is not in this runtime")
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(str(getattr(existing, "__file__", ""))).resolve() != path:
            raise RepositoryLayoutError("RUNTIME_MODULE_CONFLICT:" + name)
        return existing
    module_directory = str(path.parent)
    if module_directory not in sys.path:
        # The legacy scene-core modules still use sibling imports while they
        # migrate behind the packaged CLI/MCP boundary.
        sys.path.insert(0, module_directory)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RepositoryLayoutError(f"REPOSITORY_MODULE_MISSING:{relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
