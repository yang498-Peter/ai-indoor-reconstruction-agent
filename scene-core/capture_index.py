#!/usr/bin/env python3
"""Build and query a read-only, source-metre tiled point-cloud cache.

The source LAS/LAZ is streamed exactly once while building the index.  Later
global passes read the compact tile store and local sections touch only tiles
whose plan bounds intersect the requested region.  Coordinates are stored as
float32 offsets from a float64 source origin so large projected coordinates do
not lose centimetre precision.

The cache is a derivative artifact.  It never changes the capture and it is
published atomically only after every tile and the manifest are complete.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Iterator

import laspy
import numpy as np


FORMAT = "capture-index-v1"
CHUNK_POINTS = 1_000_000
RECORD_DTYPE = np.dtype(
    [
        ("x", "<f4"),
        ("y", "<f4"),
        ("z", "<f4"),
        ("r", "u1"),
        ("g", "u1"),
        ("b", "u1"),
    ],
    align=False,
)


class CaptureIndexError(RuntimeError):
    pass


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _header_identity(source: Path) -> dict[str, object]:
    stat = source.stat()
    with laspy.open(source) as reader:
        header = reader.header
        bounds = {
            "minX": float(header.mins[0]),
            "minY": float(header.mins[1]),
            "minZ": float(header.mins[2]),
            "maxX": float(header.maxs[0]),
            "maxY": float(header.maxs[1]),
            "maxZ": float(header.maxs[2]),
        }
        point_count = int(header.point_count)
        point_format = int(header.point_format.id)
    identity: dict[str, object] = {
        "path": str(source.resolve()),
        "bytes": int(stat.st_size),
        "modifiedNs": int(stat.st_mtime_ns),
        "pointCount": point_count,
        "pointFormat": point_format,
        "bounds": bounds,
    }
    identity["fingerprint"] = _canonical_hash(identity)
    return identity


def _capture_binding(source: Path, manifest_path: Path | None, output: Path) -> dict[str, object] | None:
    if manifest_path is None:
        return None
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capture_root = Path(str(manifest.get("captureRoot", ""))).resolve()
    recommended = manifest.get("recommended")
    point_cloud = recommended.get("pointCloud") if isinstance(recommended, dict) else None
    relative_path = point_cloud.get("path") if isinstance(point_cloud, dict) else None
    if not isinstance(relative_path, str) or not relative_path:
        raise CaptureIndexError("capture manifest has no unambiguous recommended point cloud")
    expected = (capture_root / Path(relative_path)).resolve()
    if expected != source.resolve():
        raise CaptureIndexError("source LAS/LAZ does not match the capture manifest recommendation")
    source_stat = source.stat()
    if int(point_cloud.get("bytes", -1)) != source_stat.st_size or int(point_cloud.get("modifiedNs", -1)) != source_stat.st_mtime_ns:
        raise CaptureIndexError("capture manifest point-cloud identity is stale")
    resolved_output = output.resolve()
    if resolved_output == capture_root or capture_root in resolved_output.parents:
        raise CaptureIndexError("capture index output must stay outside the read-only capture root")
    fingerprint = manifest.get("captureFingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise CaptureIndexError("capture manifest fingerprint is missing or invalid")
    return {
        "captureFingerprint": fingerprint,
        "captureManifest": str(manifest_path),
        "captureManifestSha256": _sha256_file(manifest_path),
        "relativePointCloud": relative_path,
    }


def _rgb(chunk: "laspy.ScaleAwarePointRecord") -> np.ndarray:
    names = set(chunk.point_format.dimension_names)
    if not {"red", "green", "blue"}.issubset(names):
        return np.zeros((len(chunk), 3), dtype=np.uint8)
    values = np.column_stack(
        (
            np.asarray(chunk.red, dtype=np.uint16),
            np.asarray(chunk.green, dtype=np.uint16),
            np.asarray(chunk.blue, dtype=np.uint16),
        )
    )
    if values.max(initial=0) > 255:
        values = np.rint(values.astype(np.float64) / 257.0)
    return np.clip(values, 0, 255).astype(np.uint8)


def build_index(
    source: Path,
    output: Path,
    *,
    tile_size_m: float = 8.0,
    every: int = 1,
    capture_manifest: Path | None = None,
) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_file() or source.suffix.casefold() not in {".las", ".laz"}:
        raise CaptureIndexError("capture index currently requires one existing LAS or LAZ source")
    if output.exists():
        raise CaptureIndexError("capture index output already exists; use a fresh derivative directory")
    if not math.isfinite(tile_size_m) or tile_size_m <= 0:
        raise CaptureIndexError("tile_size_m must be a finite positive number")
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        raise CaptureIndexError("every must be a positive integer")

    before = _header_identity(source)
    binding = _capture_binding(source, capture_manifest, output)
    bounds = before["bounds"]
    assert isinstance(bounds, dict)
    origin = {
        "x": float(bounds["minX"]),
        "y": float(bounds["minY"]),
        "z": float(bounds["minZ"]),
    }
    tile_columns = max(1, int(math.ceil((float(bounds["maxX"]) - origin["x"]) / tile_size_m)))
    tile_rows = max(1, int(math.ceil((float(bounds["maxY"]) - origin["y"]) / tile_size_m)))

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    tiles_root = temporary / "tiles"
    tiles_root.mkdir()
    tile_counts: dict[str, int] = {}
    indexed_count = 0
    has_color = False
    try:
        with laspy.open(source) as reader:
            has_color = {"red", "green", "blue"}.issubset(set(reader.header.point_format.dimension_names))
            for chunk in reader.chunk_iterator(CHUNK_POINTS):
                if every > 1:
                    chunk = chunk[::every]
                if len(chunk) == 0:
                    continue
                x = np.asarray(chunk.x, dtype=np.float64)
                y = np.asarray(chunk.y, dtype=np.float64)
                z = np.asarray(chunk.z, dtype=np.float64)
                colors = _rgb(chunk)
                tile_x = np.floor((x - origin["x"]) / tile_size_m).astype(np.int64)
                tile_y = np.floor((y - origin["y"]) / tile_size_m).astype(np.int64)
                tile_x = np.clip(tile_x, 0, tile_columns - 1)
                tile_y = np.clip(tile_y, 0, tile_rows - 1)
                flat = tile_y * tile_columns + tile_x
                order = np.argsort(flat, kind="stable")
                ordered_flat = flat[order]
                boundaries = np.r_[0, np.flatnonzero(ordered_flat[1:] != ordered_flat[:-1]) + 1, len(order)]
                for start, end in zip(boundaries[:-1], boundaries[1:]):
                    selected = order[start:end]
                    flat_key = int(ordered_flat[start])
                    ix = flat_key % tile_columns
                    iy = flat_key // tile_columns
                    key = f"{ix}_{iy}"
                    records = np.empty(len(selected), dtype=RECORD_DTYPE)
                    records["x"] = (x[selected] - origin["x"]).astype(np.float32)
                    records["y"] = (y[selected] - origin["y"]).astype(np.float32)
                    records["z"] = (z[selected] - origin["z"]).astype(np.float32)
                    records["r"] = colors[selected, 0]
                    records["g"] = colors[selected, 1]
                    records["b"] = colors[selected, 2]
                    with (tiles_root / f"tile-{key}.bin").open("ab") as stream:
                        stream.write(records.tobytes(order="C"))
                    tile_counts[key] = tile_counts.get(key, 0) + len(records)
                    indexed_count += len(records)

        after = _header_identity(source)
        if after != before:
            raise CaptureIndexError("source point cloud changed while the index was being built")
        tiles = {
            key: {
                "path": f"tiles/tile-{key}.bin",
                "pointCount": count,
                "bytes": count * RECORD_DTYPE.itemsize,
            }
            for key, count in sorted(
                tile_counts.items(),
                key=lambda item: tuple(int(value) for value in reversed(item[0].split("_"))),
            )
        }
        fingerprint_basis = {
            "format": FORMAT,
            "sourceFingerprint": before["fingerprint"],
            "captureFingerprint": binding.get("captureFingerprint") if binding else None,
            "tileSizeM": tile_size_m,
            "decimation": every,
            "indexedPointCount": indexed_count,
            "tiles": {key: value["pointCount"] for key, value in tiles.items()},
        }
        manifest: dict[str, object] = {
            "format": FORMAT,
            "sourceIdentity": before,
            "captureBinding": binding,
            "origin": origin,
            "bounds": bounds,
            "tileSizeM": tile_size_m,
            "tileColumns": tile_columns,
            "tileRows": tile_rows,
            "recordDtype": RECORD_DTYPE.descr,
            "recordBytes": RECORD_DTYPE.itemsize,
            "decimation": every,
            "inputPointCount": before["pointCount"],
            "indexedPointCount": indexed_count,
            "hasColor": has_color,
            "tiles": tiles,
            "indexFingerprint": _canonical_hash(fingerprint_basis),
        }
        (temporary / "capture-index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
        return manifest
    except Exception:
        if temporary.exists():
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            temporary.rmdir()
        raise


@dataclass
class PointQuery:
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    rgb: np.ndarray
    stats: dict[str, int]

    @property
    def point_count(self) -> int:
        return int(len(self.x))


class CaptureIndex:
    def __init__(self, root: Path, manifest: dict[str, object]):
        self.root = root
        self.manifest = manifest
        origin = manifest["origin"]
        assert isinstance(origin, dict)
        self.origin_x = float(origin["x"])
        self.origin_y = float(origin["y"])
        self.origin_z = float(origin["z"])
        self.tile_size_m = float(manifest["tileSizeM"])

    @classmethod
    def open(cls, root: Path, *, validate_source: bool = False) -> "CaptureIndex":
        root = root.resolve()
        manifest_path = root / "capture-index.json"
        if not manifest_path.is_file():
            raise CaptureIndexError(f"capture index manifest is missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise CaptureIndexError("unsupported capture index format")
        if manifest.get("recordBytes") != RECORD_DTYPE.itemsize:
            raise CaptureIndexError("capture index record layout is incompatible")
        tiles = manifest.get("tiles")
        if not isinstance(tiles, dict):
            raise CaptureIndexError("capture index tile table is invalid")
        for key, item in tiles.items():
            if not isinstance(item, dict):
                raise CaptureIndexError(f"capture index tile entry is invalid: {key}")
            path = (root / str(item.get("path", ""))).resolve()
            if root not in path.parents or not path.is_file():
                raise CaptureIndexError(f"capture index tile is missing or escapes its root: {key}")
            expected_bytes = int(item.get("pointCount", -1)) * RECORD_DTYPE.itemsize
            if expected_bytes < 0 or path.stat().st_size != expected_bytes:
                raise CaptureIndexError(f"capture index tile size is stale: {key}")
        index = cls(root, manifest)
        if validate_source:
            index.validate_source()
        return index

    def validate_source(self) -> None:
        identity = self.manifest.get("sourceIdentity")
        if not isinstance(identity, dict):
            raise CaptureIndexError("capture index source identity is missing")
        source = Path(str(identity.get("path", "")))
        if not source.is_file() or _header_identity(source) != identity:
            raise CaptureIndexError("capture index source identity is stale")

    def _tile_keys(self, min_x: float, min_y: float, max_x: float, max_y: float) -> list[str]:
        if not all(math.isfinite(value) for value in (min_x, min_y, max_x, max_y)) or min_x > max_x or min_y > max_y:
            raise CaptureIndexError("query bounds must be finite and ordered")
        columns = int(self.manifest["tileColumns"])
        rows = int(self.manifest["tileRows"])
        x0 = max(0, min(columns - 1, int(math.floor((min_x - self.origin_x) / self.tile_size_m))))
        x1 = max(0, min(columns - 1, int(math.floor((max_x - self.origin_x) / self.tile_size_m))))
        y0 = max(0, min(rows - 1, int(math.floor((min_y - self.origin_y) / self.tile_size_m))))
        y1 = max(0, min(rows - 1, int(math.floor((max_y - self.origin_y) / self.tile_size_m))))
        table = self.manifest["tiles"]
        assert isinstance(table, dict)
        return [f"{ix}_{iy}" for iy in range(y0, y1 + 1) for ix in range(x0, x1 + 1) if f"{ix}_{iy}" in table]

    def iter_bbox(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        *,
        z_min: float | None = None,
        z_max: float | None = None,
        every: int = 1,
    ) -> Iterator[PointQuery]:
        if isinstance(every, bool) or not isinstance(every, int) or every < 1:
            raise CaptureIndexError("query every must be a positive integer")
        table = self.manifest["tiles"]
        assert isinstance(table, dict)
        for key in self._tile_keys(min_x, min_y, max_x, max_y):
            item = table[key]
            assert isinstance(item, dict)
            records = np.fromfile(self.root / str(item["path"]), dtype=RECORD_DTYPE)
            x = records["x"].astype(np.float64) + self.origin_x
            y = records["y"].astype(np.float64) + self.origin_y
            z = records["z"].astype(np.float64) + self.origin_z
            keep = (x >= min_x) & (x <= max_x) & (y >= min_y) & (y <= max_y)
            if z_min is not None:
                keep &= z >= z_min
            if z_max is not None:
                keep &= z <= z_max
            selected = np.flatnonzero(keep)
            if every > 1:
                selected = selected[::every]
            if len(selected) == 0:
                continue
            rgb = np.column_stack((records["r"][selected], records["g"][selected], records["b"][selected]))
            yield PointQuery(
                x=x[selected],
                y=y[selected],
                z=z[selected],
                rgb=rgb.astype(np.uint8, copy=False),
                stats={"tilesRead": 1, "pointsRead": len(records), "pointsReturned": len(selected)},
            )

    def query_bbox(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        *,
        z_min: float | None = None,
        z_max: float | None = None,
        every: int = 1,
    ) -> PointQuery:
        batches = list(
            self.iter_bbox(
                min_x,
                min_y,
                max_x,
                max_y,
                z_min=z_min,
                z_max=z_max,
                every=every,
            )
        )
        stats = {
            "tilesRead": sum(batch.stats["tilesRead"] for batch in batches),
            "pointsRead": sum(batch.stats["pointsRead"] for batch in batches),
            "pointsReturned": sum(batch.stats["pointsReturned"] for batch in batches),
        }
        if not batches:
            empty = np.empty(0, dtype=np.float64)
            return PointQuery(empty, empty.copy(), empty.copy(), np.empty((0, 3), dtype=np.uint8), stats)
        return PointQuery(
            x=np.concatenate([batch.x for batch in batches]),
            y=np.concatenate([batch.y for batch in batches]),
            z=np.concatenate([batch.z for batch in batches]),
            rgb=np.concatenate([batch.rgb for batch in batches]),
            stats=stats,
        )

    def query_all(self, *, z_min: float | None = None, z_max: float | None = None, every: int = 1) -> PointQuery:
        bounds = self.manifest["bounds"]
        assert isinstance(bounds, dict)
        return self.query_bbox(
            float(bounds["minX"]),
            float(bounds["minY"]),
            float(bounds["maxX"]),
            float(bounds["maxY"]),
            z_min=z_min,
            z_max=z_max,
            every=every,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="stream LAS/LAZ once and atomically publish a tiled cache")
    build.add_argument("--las", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--tile-size", type=float, default=8.0)
    build.add_argument("--every", type=int, default=1)
    build.add_argument("--capture-manifest", type=Path)
    query = sub.add_parser("query", help="query one source-metre bounding box")
    query.add_argument("--index", required=True, type=Path)
    query.add_argument("--bbox", required=True, help="minX,minY,maxX,maxY")
    query.add_argument("--zrange", help="zMin,zMax")
    query.add_argument("--every", type=int, default=1)
    query.add_argument("--output", type=Path, help="optional compressed NPZ with x/y/z/rgb")
    query.add_argument("--validate-source", action="store_true")
    args = parser.parse_args()

    if args.command == "build":
        manifest = build_index(
            args.las,
            args.output,
            tile_size_m=args.tile_size,
            every=args.every,
            capture_manifest=args.capture_manifest,
        )
        print(json.dumps({"ok": True, "index": str(args.output.resolve()), "manifest": manifest}, ensure_ascii=False))
        return 0

    bbox = [float(value) for value in args.bbox.split(",")]
    if len(bbox) != 4:
        parser.error("--bbox expects minX,minY,maxX,maxY")
    zrange = [float(value) for value in args.zrange.split(",")] if args.zrange else [None, None]
    if len(zrange) != 2:
        parser.error("--zrange expects zMin,zMax")
    index = CaptureIndex.open(args.index, validate_source=args.validate_source)
    result = index.query_bbox(*bbox, z_min=zrange[0], z_max=zrange[1], every=args.every)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.output, x=result.x, y=result.y, z=result.z, rgb=result.rgb)
    print(json.dumps({"ok": True, "pointCount": result.point_count, "stats": result.stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
