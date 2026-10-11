"""Atomic byte artifacts without changing the frozen legacy code fingerprint."""
import os
from pathlib import Path
from eventarena.storage_io import retry_io


def atomic_write_bytes(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')

    def commit():
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    retry_io(commit, path)
