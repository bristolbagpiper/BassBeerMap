"""Small cross-platform advisory locks for persistent state files."""

import os
import time
from pathlib import Path


def acquire_file_lock(path, timeout=0):
    """Acquire an OS-owned lock; the persistent file itself is never the lock state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    start = time.monotonic()
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'0')
            handle.flush()
        while not locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.monotonic() - start >= timeout:
                    break
                time.sleep(min(0.2, timeout))
        if locked:
            return handle
    finally:
        if not locked:
            handle.close()
    return None


def release_file_lock(handle):
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()
