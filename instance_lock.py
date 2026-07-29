"""Cross-platform single-process lock used by PythonAnywhere and local runs."""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunningError(f"Another Aegis process holds {self.path}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()

