from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path


def payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def archive_payload(payload: bytes, directory: str | Path, digest: str) -> Path:
    archive_dir = Path(directory)
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / f"{digest}.json.gz"
    if destination.exists():
        return destination

    temporary = archive_dir / f".{digest}.{os.getpid()}.tmp"
    temporary.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
    try:
        os.replace(temporary, destination)
    except OSError:
        if not destination.exists():
            raise
        temporary.unlink(missing_ok=True)
    return destination
