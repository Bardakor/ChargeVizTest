from __future__ import annotations

from pathlib import Path

import pytest

from chargeviz.cli import main, parse_duration
from chargeviz.lock import RunLock


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2h", 7200.0), ("120m", 7200.0), ("30s", 30.0), ("1.5h", 5400.0)],
)
def test_human_duration_is_parsed(value: str, expected: float) -> None:
    assert parse_duration(value) == expected


def test_invalid_human_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration("tomorrow")


def test_collect_command_reports_an_existing_collector(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "collector.sqlite3"

    with RunLock(database_path):
        exit_code = main(
            [
                "collect",
                "--db",
                str(database_path),
                "--duration",
                "1s",
                "--url",
                "https://example.invalid",
            ]
        )

    assert exit_code == 3
    assert "already running" in capsys.readouterr().err
