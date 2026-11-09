"""Deterministic synthetic benchmark with explicit real-data limitations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from jsonschema import Draft202012Validator

from . import __version__
from .repository import load_repo_module, repository_root

KEY_LIMIT = 0.02
PERFORMANCE_LIMIT = 0.15


def _canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _peak_rss_bytes() -> int | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_current_process = kernel32.GetCurrentProcess
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = kernel32.K32GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(Counters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            ok = get_process_memory_info(
                get_current_process(), ctypes.byref(counters), counters.cb
            )
            return int(counters.PeakWorkingSetSize) if ok else None
        except (AttributeError, OSError):
            return None
    try:
        import importlib

        resource_module = importlib.import_module("resource")
        usage = resource_module.getrusage(resource_module.RUSAGE_SELF)
        value = int(usage.ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError):
        return None


@dataclass
class _Query:
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    stats: dict[str, Any]

    @property
    def point_count(self) -> int:
        return int(len(self.x))


class _SyntheticIndex:
    def __init__(self, points: np.ndarray) -> None:
        self.points = points

    def query_bbox(self, x_min: float, y_min: float, x_max: float, y_max: float,
                   *, z_min: float | None = None, z_max: float | None = None) -> _Query:
        selected = (
            (self.points[:, 0] >= x_min) & (self.points[:, 0] <= x_max)
            & (self.points[:, 1] >= y_min) & (self.points[:, 1] <= y_max)
        )
        if z_min is not None:
            selected &= self.points[:, 2] >= z_min
        if z_max is not None:
            selected &= self.points[:, 2] <= z_max
        rows = self.points[selected]
        return _Query(rows[:, 0], rows[:, 1], rows[:, 2], {
            "tileQueryCount": 1, "pointsRead": int(len(rows)), "cacheHitRatio": 0.0,
        })


def _fixture() -> tuple[dict[str, Any], dict[str, Any], _SyntheticIndex, str]:
    x = np.arange(0.025, 6.0, 0.05, dtype=np.float64)
    z = np.arange(0.05, 2.8, 0.10, dtype=np.float64)
    points = np.asarray([(a, face, height) for a in x for height in z for face in (-0.1, 0.1)])
    level = {"id": "level_0", "type": "level", "elevation": 0.0}
    wall = {
        "id": "wall_complete", "type": "wall", "parentId": "level_0", "children": [],
        "start": [0.0, 0.0], "end": [6.0, 0.0], "height": 2.8, "thickness": 0.2,
        "baseHeight": 0.0, "wallKind": "solid",
    }
    scene = {"nodes": {"level_0": level, "wall_complete": wall}}
    fixture_digest = _canonical_digest({"scene": scene, "pointCount": int(len(points)), "stepM": 0.05})
    return scene, wall, _SyntheticIndex(points), fixture_digest


def _load_baseline(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("suite") != "synthetic-wall-v1":
        raise ValueError("BENCHMARK_BASELINE_INVALID: suite")
    payload = {
        name: value.get(name)
        for name in (
            "suite", "fixtureDigest", "metrics", "deltas", "gates",
            "thresholds", "limitations", "configuration",
        )
    }
    if value.get("payloadDigest") != _canonical_digest(payload):
        raise ValueError("BENCHMARK_BASELINE_INVALID: payloadDigest")
    return value


def run_quick_benchmark(baseline_path: Path | None = None) -> dict[str, Any]:
    root = repository_root()
    metrics_module = load_repo_module("pointcloud_scene_metrics", "scene-core/pointcloud_scene_metrics.py")
    scene, wall, index, fixture_digest = _fixture()
    configuration = {
        "wallEvaluation": {
            "residualP90MaxM": 0.08,
            "supportRatioMin": 0.80,
            "supportToleranceM": 0.04,
            "binSizeM": 0.10,
        },
        "regressionThresholds": {
            "maxKeyMetricRegressionRatio": KEY_LIMIT,
            "maxPerformanceRegressionRatio": PERFORMANCE_LIMIT,
        },
    }
    wall_config = configuration["wallEvaluation"]
    started = time.perf_counter()
    result = metrics_module.evaluate_wall(
        scene, wall, index,
        residual_p90_max_m=wall_config["residualP90MaxM"],
        support_ratio_min=wall_config["supportRatioMin"],
        support_tolerance_m=wall_config["supportToleranceM"],
        bin_size_m=wall_config["binSizeM"],
    )
    wall_clock = time.perf_counter() - started
    peak_rss = _peak_rss_bytes()
    metrics = {
        "coverageAreaRatio": result.get("coverageAreaRatio"),
        "verticalCoverageRatio": result.get("verticalCoverageRatio"),
        "surfaceResidualP90M": result.get("surfaceResidualP90M"),
        "maxUnsupportedRunM": result.get("maxUnsupportedRunM"),
        "wallStatus": result.get("status"),
        "wallClockSeconds": round(wall_clock, 6),
        "peakRssBytes": peak_rss,
        "pointsRead": result.get("queryStats", {}).get("pointsRead"),
    }
    environment_digest = _canonical_digest({
        "python": platform.python_version(), "platform": platform.platform(), "numpy": np.__version__,
    })
    baseline = _load_baseline(baseline_path)
    coverage = metrics["coverageAreaRatio"]
    residual_p90 = metrics["surfaceResidualP90M"]
    unsupported_run = metrics["maxUnsupportedRunM"]
    key_gate = "PASS" if (
        metrics["wallStatus"] == "PASS"
        and isinstance(coverage, (int, float)) and float(coverage) >= 0.98
        and isinstance(residual_p90, (int, float)) and float(residual_p90) <= 0.001
        and isinstance(unsupported_run, (int, float)) and float(unsupported_run) <= 0.001
    ) else "FAIL"
    performance_gate = "NOT_RUN"
    deltas: dict[str, float] = {}
    limitations = ["REAL_CAPTURE_NOT_PROVIDED"]
    if baseline is not None:
        baseline_metrics = baseline.get("metrics", {})
        key_specs = {
            "coverageAreaRatio": -1.0,
            "verticalCoverageRatio": -1.0,
            "surfaceResidualP90M": 1.0,
            "maxUnsupportedRunM": 1.0,
        }
        key_regressions = []
        for name, direction in key_specs.items():
            old, new = baseline_metrics.get(name), metrics.get(name)
            if isinstance(old, (int, float)) and old > 0 and isinstance(new, (int, float)):
                denominator = max(abs(float(old)), 0.001)
                regression = direction * (float(new) - float(old)) / denominator
                deltas[f"key:{name}"] = round(regression, 6)
                key_regressions.append(regression)
            elif isinstance(old, (int, float)) and isinstance(new, (int, float)):
                regression = direction * (float(new) - float(old)) / 0.001
                deltas[f"key:{name}"] = round(regression, 6)
                key_regressions.append(regression)
        if key_regressions and any(value > KEY_LIMIT for value in key_regressions):
            key_gate = "FAIL"

        baseline_environment = baseline.get("producer", {}).get("environmentDigest")
        if baseline_environment == environment_digest:
            performance_deltas = []
            for name in ("wallClockSeconds", "peakRssBytes"):
                old, new = baseline_metrics.get(name), metrics.get(name)
                if isinstance(old, (int, float)) and old > 0 and isinstance(new, (int, float)):
                    regression = (float(new) - float(old)) / float(old)
                    deltas[f"performance:{name}"] = round(regression, 6)
                    performance_deltas.append(regression)
            performance_gate = "PASS" if len(performance_deltas) == 2 and all(
                value <= PERFORMANCE_LIMIT for value in performance_deltas
            ) else "FAIL"
        else:
            limitations.append("PERFORMANCE_BASELINE_ENVIRONMENT_MISMATCH")
    else:
        limitations.append("PERFORMANCE_BASELINE_NOT_PROVIDED")
    gates = {
        "keyMetrics": key_gate,
        "performanceRegression": performance_gate,
        "realCaptureRegression": "NOT_RUN",
    }
    status = "FAIL" if "FAIL" in gates.values() else ("REVIEW" if "NOT_RUN" in gates.values() else "PASS")
    payload = {
        "suite": "synthetic-wall-v1", "fixtureDigest": fixture_digest,
        "metrics": metrics, "deltas": deltas, "gates": gates, "limitations": limitations,
        "thresholds": configuration["regressionThresholds"],
        "configuration": configuration,
    }
    code_digest = _canonical_digest({
        "benchmark.py": _file_sha(Path(__file__)),
        "pointcloud_scene_metrics.py": _file_sha(root / "scene-core" / "pointcloud_scene_metrics.py"),
    })
    report = {
        "schemaVersion": "1.0", "artifactType": "benchmark-report",
        "suite": payload["suite"], "status": status,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "producer": {
            "name": "indoor-recon", "version": __version__, "gitSha": _git_sha(root),
            "codeSha256": code_digest, "command": ["indoor-recon", "benchmark"],
            "configDigest": _canonical_digest(configuration),
            "environmentDigest": environment_digest,
            "randomSeed": 0,
        },
        "inputs": [{"artifactType": "synthetic-fixture", "payloadDigest": fixture_digest}],
        **payload,
    }
    report["payloadDigest"] = _canonical_digest(payload)
    schema = json.loads((root / "schemas" / "benchmark-report-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)
    return report


def benchmark_main(output: Path, baseline: Path | None = None) -> int:
    report = run_quick_benchmark(baseline)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"ok": report["status"] != "FAIL", "status": report["status"],
                      "output": str(output)}, ensure_ascii=False))
    return 1 if report["status"] == "FAIL" else 0
