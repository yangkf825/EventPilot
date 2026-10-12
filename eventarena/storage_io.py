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
import hashlib
import tempfile
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
    def commit():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
        temporary=Path(name)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(text);stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass
    try:
        retry_io(commit, path)
    except StorageIOError as error:
        # A failed external write must stop execution. Preserve the exact value
        # locally, rather than dropping a paid response or silently scoring it.
        journal=os.environ.get('EP_STORAGE_JOURNAL')
        if journal:
          try:
            target=path.resolve();root=os.environ.get('EP_RUN_ROOT')
            if root and target.is_relative_to(Path(root).resolve()):
                spool=Path(journal);spool.mkdir(parents=True,exist_ok=True)
                key=hashlib.sha256(os.fsencode(target)).hexdigest()
                record={'target':str(target),'sha256':hashlib.sha256(text.encode()).hexdigest(),
                        'payload':text,'errno':error.errno,'format':'exact_pending_atomic_json_v1'}
                destination=spool/(key+'.pending.json')
                fd,name=tempfile.mkstemp(prefix=key+'.',dir=spool)
                with os.fdopen(fd,'w',encoding='utf-8') as stream:
                    json.dump(record,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
                Path(name).replace(destination)
                print('[STORAGE JOURNAL] Exact pending value retained locally; execution stopped.',file=sys.stderr,flush=True)
          except OSError:
            print('[STORAGE JOURNAL] Local retention also failed; original storage error preserved.',file=sys.stderr,flush=True)
        raise
