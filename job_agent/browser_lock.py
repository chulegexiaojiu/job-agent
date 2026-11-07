"""Non-blocking OS lock shared by capture and delivery CLI workers."""
from contextlib import contextmanager
import os
from pathlib import Path


class BrowserSessionBusy(ValueError):
    pass


@contextmanager
def browser_session_lock(private: Path):
    private.mkdir(parents=True, exist_ok=True)
    # Keep the original filename so old delivery workers contend on the same lock.
    with (private / 'browser-delivery.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BrowserSessionBusy('另一个浏览器采集或投递会话正在运行。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
