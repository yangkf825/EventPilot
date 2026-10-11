"""Bounded recovery for transient filesystem failures in run metadata.

Only opening an append stream is retried. Once writing starts its outcome may
be uncertain, so a failed write is never replayed and cannot duplicate a log
record. Atomic JSON replacement can safely retry the same complete value.
"""
from __future__ import annotations

import errno
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time

RETRY_DELAYS = (0.25, 0.75, 1.5)
TRANSIENT_ERRNOS = {errno.EIO, errno.ESTALE}


class StorageIOError(OSError):
    """Run persistence failed; this must not count as an Agent failure."""


def retry_io(operation, path):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return operation()
        except OSError as error:
            if error.errno not in TRANSIENT_ERRNOS:
                raise StorageIOError(error.errno,
                    'Run persistence failed; this error is not retriable. '
                    'Check storage before resuming.', os.fspath(path)) from error
            if attempt == len(RETRY_DELAYS):
                raise StorageIOError(error.errno,
                    'Storage I/O failed after 4 attempts; run stopped. '
                    'Check the drive before resuming the same run directory.',
                    os.fspath(path)) from error
            delay = RETRY_DELAYS[attempt]
            print(f'[STORAGE RETRY {attempt + 1}/3] errno={error.errno} '
                  f'file={path} wait={delay}s', file=sys.stderr, flush=True)
            time.sleep(delay)


@contextmanager
def open_append(path):
    path = Path(path)
    stream = retry_io(lambda: path.open('a', encoding='utf-8'), path)
    try:
        with stream:
            yield stream
    except StorageIOError:
        raise
    except OSError as error:
        # Writing/closing may have committed a partial record. Do not replay.
        raise StorageIOError(error.errno,
            'Append write failed; log tail may be partial. Run stopped '
            'without replaying this record.', os.fspath(path)) from error


def atomic_write_json(path, value):
    path = Path(path)
    # Serialize before touching the file; serialization errors are not I/O.
    text = json.dumps(value, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(path.suffix + '.tmp')

    def commit():
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    retry_io(commit, path)
