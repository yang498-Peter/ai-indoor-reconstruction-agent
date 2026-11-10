"""Bundle only explicitly listed, public runtime resources in wheel and sdist."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        bundled = root / 'src/indoor_reconstruction/_runtime'
        if bundled.is_dir():
            manifest = json.loads((bundled / 'runtime-manifest.json').read_text(encoding='utf-8'))
            paths = sorted(manifest['files'])
            source = bundled
            if (digest(manifest['files']) != manifest['bundleDigest']
                    or manifest.get('format') != 'indoor-recon-runtime-v1'
                    or manifest.get('packageVersion') != self.metadata.version):
                raise ValueError('RUNTIME_MANIFEST_INVALID')
        else:
            paths = json.loads((root / 'runtime-files.json').read_text(encoding='utf-8'))['files']
            source = root
            manifest = None
        hashes = {}
        if len(paths) != len(set(paths)):
            raise ValueError('RUNTIME_RESOURCE_DUPLICATE')
        forced = build_data.setdefault('force_include', {})
        prefix = ('src/' if self.target_name == 'sdist' else '') + 'indoor_reconstruction/_runtime/'
        for relative in paths:
            path = PurePosixPath(relative)
            if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
                raise ValueError('RUNTIME_RESOURCE_PATH_INVALID')
            allowed = (
                (relative.startswith('scene-core/') and path.suffix in {'.py', '.js', '.json', '.md'})
                or (relative.startswith(('schemas/', 'profiles/')) and path.suffix == '.json')
                or (relative.startswith(('.agents/skills/', '.codex/skills/')) and path.suffix in {'.py', '.md', '.json'})
                or (relative.startswith('templates/minimal-viewer/') and path.suffix in {'.html', '.js', '.css'})
                or (relative.startswith('docs/') and path.suffix == '.md')
                or relative in {'scripts/multiview_refinement.py', 'scripts/camera_control_refinement.py',
                                'scripts/multiview_batch.py', 'scripts/indoor_presentation_evidence.py',
                                'scripts/presentation_alignment.py', 'scripts/validate_presentation_ifc.py',
                                'scripts/smoke_presentation.py'}
            )
            if not allowed or file_size_exceeded(source / relative):
                raise ValueError('RUNTIME_RESOURCE_NOT_PUBLIC_RUNTIME:' + relative)
            file = source / relative
            if file.is_symlink() or not file.resolve().is_relative_to(source.resolve()) or not file.is_file():
                raise ValueError('RUNTIME_RESOURCE_MISSING:' + relative)
            hashes[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
            if manifest is not None and hashes[relative] != manifest['files'][relative]:
                raise ValueError('RUNTIME_RESOURCE_HASH_MISMATCH:' + relative)
            forced[str(file)] = prefix + relative
        if manifest is None:
            try:
                result = subprocess.run(['git', '-c', f'safe.directory={root.as_posix()}',
                                         'rev-parse', 'HEAD'], cwd=root, capture_output=True, check=False)
                revision = result.stdout.decode().strip() if result.returncode == 0 else None
            except OSError:
                revision = None
            manifest = {'format': 'indoor-recon-runtime-v1', 'packageVersion': self.metadata.version,
                        'files': hashes, 'bundleDigest': digest(hashes),
                        'sourceRevision': revision}
        self._runtime_temp = tempfile.TemporaryDirectory(prefix='indoor-recon-build-')
        generated = Path(self._runtime_temp.name) / 'runtime-manifest.json'
        generated.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        forced[str(generated)] = prefix + generated.name

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        self._runtime_temp.cleanup()


def file_size_exceeded(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 5 * 1024 * 1024
