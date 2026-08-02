from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from chargeviz.cli import main, parse_duration, print_rich, render_markdown, render_text
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


SECTION_TITLES = (
    "Session duration",
    "Episodes excluded from the mean",
    "How complete episodes ended",
    "Sensitivity checks",
    "Run quality",
)


def test_markdown_report_states_the_headline_and_every_section(tmp_path: Path) -> None:
    report = render_markdown(analyze(_one_complete_session(tmp_path)))

    assert "**Average session duration: 2.00 min across 1 complete charging episodes.**" in report
    for title in SECTION_TITLES:
        assert f"## {title}" in report
    # Censoring counts must be visible even when they are zero, never silently dropped.
    assert "| Right-censored — still charging when the run ended | 0 |" in report
    assert "| AVAILABLE | 1 | 100.0 % |" in report


def test_text_report_carries_the_same_content_and_aligns_columns(tmp_path: Path) -> None:
    report = render_text(analyze(_one_complete_session(tmp_path)))

    assert "Average session duration: 2.00 min across 1 complete charging episodes." in report
    for title in SECTION_TITLES:
        assert title in report
    assert "Right-censored — still charging when the run ended  0" in report

    # Within a section every row is padded to one width, so the values line up.
    rows = report.split("Session duration\n", 1)[1].split("\n\n")[1].splitlines()
    assert {line.split()[0] for line in rows} == {
        "Mean",
        "Median",
        "P90",
        "Shortest",
        "Longest",
        "Complete",
    }
    assert len({len(line) for line in rows}) == 1
    assert rows[0].endswith("2.00 min")
    assert "|" not in report and "**" not in report


def test_report_renders_an_empty_database_without_crashing(tmp_path: Path) -> None:
    empty = Database(tmp_path / "empty.sqlite3").path

    for render in (render_markdown, render_text):
        report = render(analyze(empty))
        assert "not estimable" in report
        assert "no successful poll recorded" in report


def test_rich_output_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without rich installed the CLI must still print, via the plain renderer."""
    monkeypatch.setitem(sys.modules, "rich", None)

    assert print_rich(analyze(_one_complete_session(tmp_path))) is False


def test_text_written_to_a_file_never_contains_escape_codes(tmp_path: Path) -> None:
    database_path = _one_complete_session(tmp_path)
    destination = tmp_path / "out" / "report.txt"

    assert main(["report", "--db", str(database_path), "--output", str(destination)]) == 0

    written = destination.read_text(encoding="utf-8")
    assert "\x1b[" not in written
    assert "Average session duration: 2.00 min" in written


def test_report_json_output_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _one_complete_session(tmp_path)

    assert main(["report", "--db", str(database_path), "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["completed_session_count"] == 1
    assert payload["average_duration_seconds"] == pytest.approx(120.0)
