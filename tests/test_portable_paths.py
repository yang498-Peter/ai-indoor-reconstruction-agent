"""A URL route must not be confused with a private absolute machine path."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_path_guard", ROOT / "scripts/validate_portable_repo.py")
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


@pytest.mark.parametrize("prefix", ["", "'", '"', " ", "source=", "file://"])
@pytest.mark.parametrize("directory", ["home", "Users"])
def test_private_posix_roots_remain_rejected(prefix, directory):
    assert GUARD.ABSOLUTE_PATH.search(prefix + "/" + directory + "/private-capture")


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_private_windows_roots_remain_rejected(separator):
    assert GUARD.ABSOLUTE_PATH.search("C:" + separator + "Users" + separator + "private-capture")


@pytest.mark.parametrize("route", [
    "admin/users/uid/reset-password", "https://example.invalid/users/uid",
    "api/home/status", "docs/Users/README.md",
])
def test_relative_routes_are_not_private_machine_roots(route):
    assert not GUARD.ABSOLUTE_PATH.search(route)
