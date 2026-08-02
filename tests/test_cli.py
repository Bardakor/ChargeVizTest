from __future__ import annotations

import json
from pathlib import Path

import pytest

from chargeviz.cli import main, parse_duration, render_markdown
from chargeviz.database import Database
from chargeviz.lock import RunLock
from chargeviz.models import EVSEObservation, PollTimings
from chargeviz.sessions import analyze


def _one_complete_session(directory: Path) -> Path:
    """Build a database holding exactly one two-minute CHARGING episode."""
    db = Database(directory / "report.sqlite3")
    timeline = [
        ("2026-07-27T09:00:30.000000Z", "AVAILABLE"),
        ("2026-07-27T09:02:30.000000Z", "CHARGING"),
        ("2026-07-27T09:04:30.000000Z", "AVAILABLE"),
    ]
    for observed_at, status in timeline:
        poll_id = db.start_poll(scheduled_at=observed_at, started_at=observed_at)
        db.complete_poll(
            poll_id=poll_id,
            observations=[
                EVSEObservation(
                    source="mfg",
                    location_id="LOC-1",
                    evse_uid="EVSE-1",
                    evse_id="GB*MFG*E1",
                    status=status,
                    source_last_updated=observed_at,
                )
            ],
            observed_at=observed_at,
            http_status=200,
            payload_sha256="c" * 64,
            response_bytes=100,
            location_count=1,
            connector_count=1,
            duplicate_count=0,
            unknown_status_count=0,
            timings=PollTimings(fetch_ms=1.0, parse_ms=1.0),
        )
    return db.path


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


def test_report_states_the_headline_and_every_section(tmp_path: Path) -> None:
    report = render_markdown(analyze(_one_complete_session(tmp_path)))

    assert "**Average session duration: 2.00 min** across 1 complete charging episodes." in report
    for heading in (
        "## Session duration",
        "## Episodes excluded from the mean",
        "## How complete episodes ended",
        "## Sensitivity checks",
        "## Run quality",
    ):
        assert heading in report
    # Censoring counts must be visible even when they are zero, never silently dropped.
    assert "| Right-censored — still charging when the run ended | 0 |" in report
    assert "| `AVAILABLE` | 1 | 100.0 % |" in report


def test_report_renders_an_empty_database_without_crashing(tmp_path: Path) -> None:
    empty = Database(tmp_path / "empty.sqlite3").path

    report = render_markdown(analyze(empty))

    assert "not estimable" in report
    assert "no successful poll recorded" in report


def test_report_json_output_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _one_complete_session(tmp_path)

    assert main(["report", "--db", str(database_path), "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["completed_session_count"] == 1
    assert payload["average_duration_seconds"] == pytest.approx(120.0)
