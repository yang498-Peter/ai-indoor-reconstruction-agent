#!/usr/bin/env python3
"""Build and verify a self-contained, content-addressed Scene V2 publish bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Iterable


SCHEMA_VERSION = "2.0"
ARTIFACT_TYPE = "scene-v2-publish-bundle"
REQUIRED_ROLES = {"authority", "review", "quality", "evidence", "render", "provenance"}
EXTRA_ROLES = {"evidence", "render", "provenance"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PublishBundleError(RuntimeError):
    def __init__(self, message: str, code: str = "PUBLISH_BUNDLE_INVALID") -> None:
        super().__init__(f"{code}:{message}")
        self.code = code
        self.message = message


class BundleSource:
    __slots__ = ("role", "bundle_path", "sha256", "size", "source_path", "content")

    def __init__(
        self,
        role: str,
        bundle_path: str,
        sha256: str,
        size: int,
        source_path: Path | None = None,
        content: bytes | None = None,
    ) -> None:
        self.role = role
        self.bundle_path = bundle_path
        self.sha256 = sha256
        self.size = size
        self.source_path = source_path
        self.content = content

    def descriptor(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.bundle_path,
            "sha256": self.sha256,
            "size": self.size,
        }


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path, code: str = "PUBLISH_INPUT_INVALID") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishBundleError(str(path), code) from error
    if not isinstance(value, dict):
        raise PublishBundleError(str(path), code)
    return value


def _safe_name(path: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip(".-")
    return value or "artifact.bin"


def _resolve_scene_reference(scene_path: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PublishBundleError("scene evidence path is missing", "PUBLISH_EVIDENCE_PATH_INVALID")
    normalized = value.replace("\\", "/")
    relative = Path(*PurePosixPath(normalized).parts)
    if (
        relative.is_absolute()
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise PublishBundleError(value, "PUBLISH_EVIDENCE_PATH_ESCAPE")
    for base in (scene_path.parent, scene_path.parent.parent):
        candidate = (base / relative).resolve()
        if candidate == base.resolve() or base.resolve() in candidate.parents:
            if candidate.is_file():
                return candidate
    raise PublishBundleError(value, "PUBLISH_EVIDENCE_MISSING")


def parse_bundle_specs(values: Iterable[str]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise PublishBundleError(value, "PUBLISH_BUNDLE_SPEC_INVALID")
        role, raw_path = value.split("=", 1)
        role = role.strip().lower()
        if role not in EXTRA_ROLES or not raw_path.strip():
            raise PublishBundleError(value, "PUBLISH_BUNDLE_SPEC_INVALID")
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise PublishBundleError(str(path), "PUBLISH_BUNDLE_FILE_MISSING")
        result.append((role, path))
    return result


def _file_source(role: str, bundle_path: str, path: Path, expected_sha256: str | None = None) -> BundleSource:
    resolved = path.resolve()
    if not resolved.is_file():
        raise PublishBundleError(str(resolved), "PUBLISH_BUNDLE_FILE_MISSING")
    digest = _sha256_file(resolved)
    if expected_sha256 is not None and digest != expected_sha256:
        raise PublishBundleError(bundle_path, "PUBLISH_BUNDLE_SOURCE_HASH_MISMATCH")
    return BundleSource(role, bundle_path, digest, resolved.stat().st_size, source_path=resolved)


def _bytes_source(role: str, bundle_path: str, content: bytes) -> BundleSource:
    return BundleSource(role, bundle_path, _sha256_bytes(content), len(content), content=content)


def _provenance_payload(state: dict[str, Any]) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for name in state.get("stageOrder", []):
        stage = state.get("stages", {}).get(name, {})
        stages[name] = {
            "status": stage.get("status"),
            "sceneSha256": stage.get("sceneSha256"),
            "executionRunId": stage.get("executionRunId"),
            "identityDigest": stage.get("identityDigest"),
            "evaluation": stage.get("evaluation"),
            "artifacts": [
                {
                    key: item.get(key)
                    for key in ("artifactType", "sha256", "role")
                    if item.get(key) is not None
                }
                for item in stage.get("artifacts", [])
                if isinstance(item, dict)
            ],
        }
    return {
        "schemaVersion": "1.0",
        "artifactType": "publish-provenance-index",
        "jobId": state.get("jobId"),
        "captureFingerprint": state.get("captureFingerprint"),
        "pipelineContractDigest": state.get("pipelineContractDigest"),
        "currentSceneSha256": state.get("currentSceneSha256"),
        "stages": stages,
        "capabilities": {
            name: {
                "status": value.get("status"),
                "evidenceSha256s": sorted(
                    item.get("sha256")
                    for item in value.get("evidence", [])
                    if isinstance(item, dict) and isinstance(item.get("sha256"), str)
                ),
            }
            for name, value in sorted(state.get("capabilities", {}).items())
            if isinstance(value, dict)
        },
        "executions": {
            run_id: {
                "identityDigest": value.get("identityDigest"),
                "identity": value.get("identity"),
            }
            for run_id, value in sorted(state.get("executions", {}).items())
            if isinstance(value, dict)
        },
    }


def _bundle_sources(
    scene_path: Path,
    review_path: Path,
    quality_path: Path,
    state: dict[str, Any],
    extras: list[tuple[str, Path]],
) -> tuple[list[BundleSource], dict[str, Any], dict[str, Any]]:
    scene_path, review_path, quality_path = scene_path.resolve(), review_path.resolve(), quality_path.resolve()
    scene = _load_object(scene_path)
    quality = _load_object(quality_path)
    sources = [
        _file_source("authority", "scene-authority.json", scene_path),
        _file_source("review", "review-receipt.json", review_path),
        _file_source("quality", "quality-report.json", quality_path),
    ]
    seen_evidence: set[tuple[str, str]] = set()
    for entry in scene.get("evidence", {}).values():
        if not isinstance(entry, dict):
            continue
        for source in entry.get("sources", []):
            if not isinstance(source, dict) or not source.get("path"):
                continue
            evidence_path = _resolve_scene_reference(scene_path, source.get("path"))
            expected = source.get("contentSha256") or source.get("sha256")
            if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
                raise PublishBundleError(str(source.get("path")), "PUBLISH_EVIDENCE_HASH_MISSING")
            key = ("evidence", expected)
            if key not in seen_evidence:
                sources.append(_file_source(
                    "evidence",
                    f"evidence/{expected[:16]}-{_safe_name(evidence_path)}",
                    evidence_path,
                    expected,
                ))
                seen_evidence.add(key)
            receipt = source.get("provenanceReceipt")
            if isinstance(receipt, dict) and receipt.get("path"):
                receipt_path = _resolve_scene_reference(scene_path, receipt.get("path"))
                receipt_sha = receipt.get("sha256")
                if not isinstance(receipt_sha, str) or not SHA256_RE.fullmatch(receipt_sha):
                    raise PublishBundleError(str(receipt.get("path")), "PUBLISH_PROVENANCE_HASH_MISSING")
                key = ("provenance", receipt_sha)
                if key not in seen_evidence:
                    sources.append(_file_source(
                        "provenance",
                        f"provenance/{receipt_sha[:16]}-{_safe_name(receipt_path)}",
                        receipt_path,
                        receipt_sha,
                    ))
                    seen_evidence.add(key)
    state_extras: list[tuple[str, Path]] = []
    for issue in state.get("issues", []):
        if not isinstance(issue, dict):
            continue
        for attempt in issue.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            for item in attempt.get("evidence", []):
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                role = "render" if item.get("role") == "render" else "evidence"
                path = Path(str(item["path"])).resolve()
                if not path.is_file():
                    raise PublishBundleError(str(path), "PUBLISH_REVIEW_EVIDENCE_MISSING")
                expected = item.get("sha256")
                if not isinstance(expected, str) or _sha256_file(path) != expected:
                    raise PublishBundleError(str(path), "PUBLISH_REVIEW_EVIDENCE_HASH_STALE")
                state_extras.append((role, path))
    for role, path in [*state_extras, *extras]:
        digest = _sha256_file(path)
        key = (role, digest)
        if key in seen_evidence:
            continue
        folder = "renders" if role == "render" else role
        sources.append(_file_source(role, f"{folder}/{digest[:16]}-{_safe_name(path)}", path, digest))
        seen_evidence.add(key)
    provenance = json.dumps(
        _provenance_payload(state), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8") + b"\n"
    sources.append(_bytes_source("provenance", "provenance/pipeline-provenance.json", provenance))
    roles = {source.role for source in sources}
    missing = REQUIRED_ROLES - roles
    if missing:
        code = "PUBLISH_RENDER_REQUIRED" if missing == {"render"} else "PUBLISH_BUNDLE_ROLE_MISSING"
        raise PublishBundleError(",".join(sorted(missing)), code)
    destinations = [source.bundle_path for source in sources]
    if len(destinations) != len(set(destinations)):
        raise PublishBundleError("duplicate bundle destination", "PUBLISH_BUNDLE_PATH_COLLISION")
    sources.sort(key=lambda source: source.bundle_path)
    return sources, scene, quality


def _write_source(root: Path, source: BundleSource) -> None:
    destination = root / PurePosixPath(source.bundle_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.source_path is not None:
        with source.source_path.open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    elif source.content is not None:
        with destination.open("xb") as output_stream:
            output_stream.write(source.content)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    else:
        raise PublishBundleError(source.bundle_path, "PUBLISH_BUNDLE_SOURCE_INVALID")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _relative_file(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise PublishBundleError("manifest file path", "PUBLISH_MANIFEST_PATH_INVALID")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in pure.parts
        or "." in pure.parts
    ):
        raise PublishBundleError(value, "PUBLISH_MANIFEST_PATH_ESCAPE")
    resolved = (root / Path(*pure.parts)).resolve()
    if root.resolve() not in resolved.parents:
        raise PublishBundleError(value, "PUBLISH_MANIFEST_PATH_ESCAPE")
    return resolved


def verify_bundle(root: Path, *, require_directory_name: bool = True) -> dict[str, Any]:
    root = root.resolve()
    manifest = _load_object(root / "publish-manifest.json", "PUBLISH_MANIFEST_INVALID")
    if manifest.get("schemaVersion") != SCHEMA_VERSION or manifest.get("artifactType") != ARTIFACT_TYPE:
        raise PublishBundleError("manifest identity", "PUBLISH_MANIFEST_INVALID")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PublishBundleError("manifest files", "PUBLISH_MANIFEST_INVALID")
    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise PublishBundleError("manifest file entry", "PUBLISH_MANIFEST_INVALID")
        role, relative, digest, size = item.get("role"), item.get("path"), item.get("sha256"), item.get("size")
        if role not in REQUIRED_ROLES or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise PublishBundleError(str(relative), "PUBLISH_MANIFEST_INVALID")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or relative in seen:
            raise PublishBundleError(str(relative), "PUBLISH_MANIFEST_INVALID")
        seen.add(relative)
        path = _relative_file(root, relative)
        if not path.is_file() or path.stat().st_size != size or _sha256_file(path) != digest:
            raise PublishBundleError(str(relative), "PUBLISH_BUNDLE_REVALIDATION_FAILED")
        descriptors.append({"role": role, "path": relative, "sha256": digest, "size": size})
    roles = {item["role"] for item in descriptors}
    if not REQUIRED_ROLES.issubset(roles):
        raise PublishBundleError(",".join(sorted(REQUIRED_ROLES - roles)), "PUBLISH_BUNDLE_ROLE_MISSING")
    for singleton in ("authority", "review", "quality"):
        if sum(item["role"] == singleton for item in descriptors) != 1:
            raise PublishBundleError(singleton, "PUBLISH_BUNDLE_ROLE_CARDINALITY_INVALID")
    digest_payload = {key: value for key, value in manifest.items() if key != "bundleDigest"}
    digest_payload["files"] = sorted(descriptors, key=lambda item: item["path"])
    bundle_digest = _canonical_digest(digest_payload)
    if manifest.get("bundleDigest") != bundle_digest:
        raise PublishBundleError("bundle digest", "PUBLISH_BUNDLE_DIGEST_MISMATCH")
    if require_directory_name and root.name != bundle_digest:
        raise PublishBundleError(root.name, "PUBLISH_CONTENT_ADDRESS_MISMATCH")
    by_role = {item["role"]: item for item in descriptors if item["role"] in {"authority", "review", "quality"}}
    scene = _load_object(_relative_file(root, by_role["authority"]["path"]))
    review = _load_object(_relative_file(root, by_role["review"]["path"]))
    quality = _load_object(_relative_file(root, by_role["quality"]["path"]))
    blocking_checks = quality.get("blockingChecks")
    if (
        quality.get("status") != "PASS"
        or not isinstance(blocking_checks, list)
        or not blocking_checks
        or any(not isinstance(item, dict) or item.get("status") != "PASS" for item in blocking_checks)
    ):
        raise PublishBundleError("quality report", "PUBLISH_QUALITY_NOT_PASS")
    authority_sha = by_role["authority"]["sha256"]
    if quality.get("artifactSha256") != authority_sha or manifest.get("artifactSha256") != authority_sha:
        raise PublishBundleError("authority binding", "PUBLISH_AUTHORITY_BINDING_STALE")
    if review.get("artifactSha256") != authority_sha:
        raise PublishBundleError("review binding", "PUBLISH_REVIEW_BINDING_STALE")
    if (
        review.get("geometryDigest") != quality.get("geometryDigest")
        or review.get("evidenceSetDigest") != quality.get("evidenceSetDigest")
    ):
        raise PublishBundleError("review digest binding", "PUBLISH_REVIEW_BINDING_STALE")
    if manifest.get("geometryDigest") != quality.get("geometryDigest"):
        raise PublishBundleError("geometry binding", "PUBLISH_QUALITY_BINDING_STALE")
    if scene.get("schemaVersion") != "2.0":
        raise PublishBundleError("authority schema", "PUBLISH_AUTHORITY_SCHEMA_INVALID")
    return manifest


def create_bundle(
    output_root: Path,
    scene_path: Path,
    review_path: Path,
    quality_path: Path,
    state: dict[str, Any],
    publisher: dict[str, Any],
    extras: list[tuple[str, Path]],
    publish_scope: str,
    scope_blocked_capabilities: list[str],
    capability_degradations: list[dict[str, Any]],
    published_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    sources, _scene, quality = _bundle_sources(scene_path, review_path, quality_path, state, extras)
    descriptors = [source.descriptor() for source in sources]
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "artifactType": ARTIFACT_TYPE,
        "publishedAt": published_at or datetime.now(timezone.utc).isoformat(),
        "publisher": publisher,
        "jobId": state.get("jobId"),
        "captureFingerprint": state.get("captureFingerprint"),
        "geometryDigest": quality.get("geometryDigest"),
        "artifactSha256": quality.get("artifactSha256"),
        "evidenceSetDigest": quality.get("evidenceSetDigest"),
        "configDigest": quality.get("configDigest"),
        "publishScope": publish_scope,
        "scopeBlockedCapabilities": scope_blocked_capabilities,
        "capabilityDegradations": capability_degradations,
        "verification": {
            "algorithm": "sha256-canonical-manifest-core-v1",
            "status": "PASS",
        },
        "files": descriptors,
    }
    bundle_digest = _canonical_digest(manifest)
    manifest["bundleDigest"] = bundle_digest
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / bundle_digest
    if final.exists():
        raise PublishBundleError(str(final), "PUBLISH_IMMUTABLE_DESTINATION_EXISTS")
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle_digest}.", dir=output_root))
    try:
        for source in sources:
            _write_source(staging, source)
        _write_manifest(staging / "publish-manifest.json", manifest)
        verify_bundle(staging, require_directory_name=False)
        os.replace(staging, final)
        verified = verify_bundle(final)
        for path in sorted(final.rglob("*"), reverse=True):
            if path.is_file():
                os.chmod(path, 0o444)
        return final, verified
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = verify_bundle(args.bundle)
    except PublishBundleError as error:
        print(json.dumps({"ok": False, "code": error.code, "error": error.message}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "bundleDigest": manifest["bundleDigest"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
