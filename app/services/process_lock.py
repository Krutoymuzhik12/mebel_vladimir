"""Эксклюзивная блокировка: один процесс слушает MAX /updates."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)


class ExclusiveLock:
    """Кросс-платформенный flock на файл. Второй экземпляр не войдёт в with."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                self._fh.write("0")
                self._fh.flush()
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            self.acquired = True
            return True
        except OSError:
            logger.warning(
                "лок занят (%s) — другой процесс уже слушает. "
                "Второй инстанс MAX inbox не запускаю.",
                self.path,
            )
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self.acquired = False
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
        self.acquired = False

    def __enter__(self) -> "ExclusiveLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
