#!/usr/bin/env python3
"""Inventory prepared static files; detect stale or unplanned release changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

SCHEMA = "presentation-file-inventory-v1"
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def portable_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("INVALID_RELATIVE_PATH")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or path.as_posix() != value:
        raise ValueError(f"INVALID_RELATIVE_PATH:{value}")
    for part in path.parts:
        if (part in {".", ".."} or part.endswith((".", " "))
                or RESERVED.match(part)
                or any(ord(char) < 32 or char in '\\:*?"<>|' for char in part)):
            raise ValueError(f"INVALID_RELATIVE_PATH:{value}")
    return value


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("files"), dict):
        raise ValueError("INVALID_INVENTORY_SCHEMA")
    seen = set()
    for name, entry in data["files"].items():
        portable_path(name)
        if name.casefold() in seen:
            raise ValueError(f"CASE_COLLISION:{name}")
        seen.add(name.casefold())
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"}
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry["bytes"]) is not int or entry["bytes"] < 0):
            raise ValueError(f"INVALID_FILE_RECORD:{name}")
    return data


def unique_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"DUPLICATE_JSON_KEY:{key}")
        data[key] = value
    return data


def read_manifest(path: Path) -> dict:
    return validate_manifest(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys))


def reject_link(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError(f"LINK_ENTRY:{path.name}")
    return info


def snapshot(root: Path) -> dict:
    reject_link(root)
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("ROOT_NOT_DIRECTORY")
    files = {}

    def walk(directory):
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            info = reject_link(path)
            if stat.S_ISDIR(info.st_mode):
                walk(path)
            elif stat.S_ISREG(info.st_mode):
                name = portable_path(path.relative_to(root).as_posix())
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                after = reject_link(path)
                if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError(f"FILE_CHANGED_DURING_HASH:{name}")
                files[name] = {"sha256": digest.hexdigest(), "bytes": info.st_size}
            else:
                raise ValueError(f"UNSUPPORTED_FILE_TYPE:{path.name}")

    walk(root)
    return validate_manifest({"schema": SCHEMA, "files": files})


def differences(before: dict, after: dict) -> dict:
    left = validate_manifest(before)["files"]
    right = validate_manifest(after)["files"]
    return {
        "added": sorted(right.keys() - left.keys()),
        "removed": sorted(left.keys() - right.keys()),
        "changed": sorted(name for name in left.keys() & right.keys() if left[name] != right[name]),
    }


def compare(before: dict, after: dict, allowed: list[str]) -> dict:
    permit = {portable_path(name) for name in allowed}
    result = differences(before, after)
    affected = set().union(*result.values())
    result["unexpected"] = sorted(affected - permit)
    result["matchesAllowedChanges"] = not result["unexpected"]
    return result


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("snapshot")
    take.add_argument("--root", type=Path, required=True)
    take.add_argument("--output", type=Path, required=True)
    diff = commands.add_parser("compare")
    diff.add_argument("--before", type=Path, required=True)
    diff.add_argument("--after", type=Path, required=True)
    diff.add_argument("--allow", action="append", default=[])
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            if args.output.resolve().is_relative_to(args.root.resolve()):
                raise ValueError("REPORT_INSIDE_INVENTORIED_TREE")
            report = snapshot(args.root)
            atomic_write(args.output, report)
            result = {"files": len(report["files"]), "bytes": sum(record["bytes"] for record in report["files"].values())}
            ok = True
        elif args.command == "compare":
            result = compare(read_manifest(args.before), read_manifest(args.after), args.allow)
            ok = result["matchesAllowedChanges"]
        else:
            result = differences(read_manifest(args.manifest), snapshot(args.root))
            result["matchesInventory"] = not any(result.values())
            ok = result["matchesInventory"]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(2, json.dumps({"error": str(error)}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
