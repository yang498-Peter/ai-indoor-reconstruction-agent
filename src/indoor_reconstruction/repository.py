"""Locate and load the checked-in domain implementation during migration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


class RepositoryLayoutError(RuntimeError):
    """Raised when a command requiring the portable checkout cannot find it."""


def repository_root() -> Path:
    candidates = [Path.cwd(), Path(__file__).resolve()]
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / "schemas" / "pipeline-contract-v2.json").is_file() and (
                parent / "scene-core" / "scene_api.py"
            ).is_file():
                return parent
    raise RepositoryLayoutError(
        "REPOSITORY_LAYOUT_REQUIRED: run this command from a portable checkout containing schemas/ and scene-core/"
    )


def load_repo_module(name: str, relative_path: str) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = repository_root() / relative_path
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
