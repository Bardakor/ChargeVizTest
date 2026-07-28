from __future__ import annotations

from pathlib import Path

import pytest

from chargeviz.lock import ConcurrentCollectorError, RunLock


def test_second_collector_for_the_same_database_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "collector.sqlite3"

    with (
        RunLock(database_path),
        pytest.raises(ConcurrentCollectorError, match="already running"),
        RunLock(database_path),
    ):
        pass


def test_lock_is_released_after_context_exit(tmp_path: Path) -> None:
    database_path = tmp_path / "collector.sqlite3"

    with RunLock(database_path):
        pass
    with RunLock(database_path):
        pass
