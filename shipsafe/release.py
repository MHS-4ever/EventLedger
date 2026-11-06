"""Deterministic, allowlisted source bundle. Never package the working directory."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

PATHS = ['app', 'migrations', 'alembic.ini', 'pyproject.toml', 'README.md',
         'shipsafe/controller.py', 'shipsafe/compose.yaml', 'shipsafe/compose.production.yaml']


def package(root: Path, sha: str, image: str, output: Path) -> dict:
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('A full source commit SHA is required')
    if not re.fullmatch(r'542468167362\.dkr\.ecr\.us-east-1\.amazonaws\.com/shipsafe/eventledger@sha256:[0-9a-f]{64}', image):
        raise ValueError('An approved ECR digest is required')
    source = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', sha, '--', *PATHS],
                            capture_output=True, check=True).stdout
    files = {}
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Release source must contain regular files only')
            name = member.name
            if Path(name).name.startswith('.') or name.endswith(('.env', '.tfvars', '.tfstate')):
                raise ValueError('Disallowed release file')
            files[name] = archive.extractfile(member).read()
    required = {'shipsafe/controller.py', 'shipsafe/compose.production.yaml', 'alembic.ini'}
    if not required <= files.keys() or not any(n.startswith('migrations/versions/') for n in files):
        raise ValueError('Release is missing a controller, configuration or migrations')
    manifest = {'manifest_version': 1, 'git_sha': sha, 'image_ref': image,
                'schema_revision': 'd4e5f6a7b8c9', 'layout': 'repository-root',
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files['deployment.json'] = (json.dumps(manifest, sort_keys=True, indent=2)+'\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'\n', encoding='ascii')
    return {'git_sha': sha, 'image_ref': image, 'artifact_sha256': digest, 'file_count': len(files)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(Path.cwd(), args.sha, args.image, args.output), sort_keys=True))
