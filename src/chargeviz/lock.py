from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ConcurrentCollectorError(RuntimeError):
    """Another collector already owns this database."""


class RunLock:
    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        self.path = path.with_name(f"{path.name}.collector.lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        try:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock_file.close()
            raise ConcurrentCollectorError(
                f"a collector is already running for {self.path.parent}"
            ) from error
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
