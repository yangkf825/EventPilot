"""Keep one writer per registered run; kernel releases the lock after a crash."""
from contextlib import contextmanager
from pathlib import Path
import os


class RunAlreadyActiveError(RuntimeError):
    pass


@contextmanager
def exclusive_run(root):
    lock_path = Path(root) / 'logs' / 'runner.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as stream:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RunAlreadyActiveError('Another process is writing this run. '
                    'Stop it before resuming the same run directory.') from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        else:
            import msvcrt
            # Windows locks a byte range, so the file needs one byte.
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RunAlreadyActiveError('Another process is writing this run. '
                    'Stop it before resuming the same run directory.') from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
