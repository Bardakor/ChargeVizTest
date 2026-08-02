from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from chargeviz.collector import Collector, CollectorConfig
from chargeviz.database import Database
from chargeviz.http import HTTPClient
from chargeviz.lock import ConcurrentCollectorError
from chargeviz.models import AnalysisReport
from chargeviz.sessions import analyze

DEFAULT_URL = "https://opendata.motorfuelgroup.net/locations"
_DURATION_PATTERN = re.compile(r"^(?P<number>(?:\d+(?:\.\d*)?|\.\d+))(?P<unit>[smh])$")


def parse_duration(value: str) -> float:
    match = _DURATION_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("duration must look like 30s, 120m, or 2h")
    factors = {"s": 1.0, "m": 60.0, "h": 3600.0}
    result = float(match.group("number")) * factors[match.group("unit")]
    if result <= 0:
        raise ValueError("duration must be positive")
    return result


def _duration_argument(value: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _minutes(seconds: float | None) -> str:
    return "not estimable" if seconds is None else f"{seconds / 60:.2f} min"


def _seconds(seconds: float | None) -> str:
    """Seconds at millisecond precision, with a minutes hint once they stop being readable."""
    if seconds is None:
        return "—"
    # Cadence figures sit around 120 s and their millisecond tail is the interesting part,
    # so only spell out minutes once the value is long enough to be hard to read.
    if seconds < 300:
        return f"{seconds:.3f} s"
    return f"{seconds:.1f} s ({int(seconds // 60)} m {int(seconds % 60):02d} s)"


def _milliseconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.1f} ms"


def _megabytes(value: int) -> str:
    return f"{value / 1_000_000:.1f} MB"


def _clock(timestamp: str | None) -> str:
    """Render a stored UTC timestamp as `YYYY-MM-DD HH:MM:SS`, dropping microseconds."""
    if timestamp is None:
        return "—"
    return timestamp.replace("T", " ")[:19] + "Z"


def _window(report: AnalysisReport) -> str:
    first, last = report.first_poll_started_at, report.last_poll_completed_at
    if first is None or last is None:
        return "no successful poll recorded"
    span = (
        datetime.fromisoformat(last.replace("Z", "+00:00"))
        - datetime.fromisoformat(first.replace("Z", "+00:00"))
    ).total_seconds()
    return (
        f"{_clock(first)} → {_clock(last)} ({int(span // 3600)} h {int(span % 3600 // 60):02d} m)"
    )


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    alignment = ["---"] + ["---:"] * (len(header) - 1)
    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(alignment) + "|",
        *["| " + " | ".join(row) + " |" for row in rows],
        "",
    ]


def render_markdown(report: AnalysisReport) -> str:
    complete = report.completed_session_count
    headline = (
        f"**Average session duration: {_minutes(report.average_duration_seconds)}** "
        f"across {complete} complete charging episodes."
    )
    terminal_total = sum(report.terminal_status_counts.values()) or 1

    def share(count: int, total: int) -> str:
        return f"{count / total * 100:.1f} %"

    lines = [
        "# ChargeViz run report",
        "",
        headline,
        "",
        f"- Observation window (UTC): {_window(report)}",
        f"- Fleet observed: {report.distinct_evse_count:,} EVSEs, "
        f"{report.status_change_count:,} status changes recorded",
        "",
        "## Session duration",
        "",
        "Complete episodes only — both the start and the end were observed.",
        "",
        *_table(
            ("Metric", "Value"),
            [
                ("Mean", _minutes(report.average_duration_seconds)),
                ("Median", _minutes(report.median_duration_seconds)),
                ("P90 (nearest rank)", _minutes(report.p90_duration_seconds)),
                ("Shortest", _minutes(report.minimum_duration_seconds)),
                ("Longest", _minutes(report.maximum_duration_seconds)),
                ("**Complete episodes (the denominator)**", f"**{complete}**"),
            ],
        ),
        "## Episodes excluded from the mean",
        "",
        "Counted and published rather than guessed at.",
        "",
        *_table(
            ("Reason", "Episodes"),
            [
                (
                    "Left-censored — already charging when first seen",
                    str(report.left_censored_session_count),
                ),
                (
                    "Right-censored — still charging when the run ended",
                    str(report.right_censored_session_count),
                ),
                (
                    "Ambiguous — ended in `UNKNOWN` or an unmapped status",
                    str(report.ambiguous_session_count),
                ),
                ("Invalid — non-positive duration", str(report.invalid_duration_count)),
            ],
        ),
        "## How complete episodes ended",
        "",
        *_table(
            ("Terminal status", "Count", "Share"),
            [
                (f"`{status}`", str(count), share(count, terminal_total))
                for status, count in sorted(
                    report.terminal_status_counts.items(), key=lambda item: -item[1]
                )
            ],
        ),
        "## Sensitivity checks",
        "",
        *_table(
            ("Check", "Value"),
            [
                (
                    "Mean using poll observation time only",
                    _minutes(report.observation_time_average_duration_seconds),
                ),
                (
                    "Episodes that needed the observation-time fallback",
                    f"{report.observation_time_fallback_count} of {complete}",
                ),
                (
                    "Episodes spanning a 429 or a collection pause",
                    f"{report.gap_affected_session_count} of {complete}",
                ),
                ("Episodes spanning no gap at all", str(report.gap_free_session_count)),
                (
                    "Mean over gap-free episodes *(a short-session bound, not a cleaner estimate)*",
                    _minutes(report.gap_free_average_duration_seconds),
                ),
            ],
        ),
        "## Run quality",
        "",
        *_table(
            ("Metric", "Value"),
            [
                ("Poll attempts", str(report.poll_attempt_count)),
                ("Successful", str(report.successful_poll_count)),
                ("Rate-limited (HTTP 429)", str(report.rate_limited_poll_count)),
                ("Other failures", str(report.failed_poll_count)),
                ("Left incomplete (`RUNNING`)", str(report.running_poll_count)),
                (
                    "Start-to-start gap, median / minimum",
                    f"{_seconds(report.median_poll_start_gap_seconds)} / "
                    f"{_seconds(report.minimum_poll_start_gap_seconds)}",
                ),
                (
                    "Longest gap between successful polls",
                    _seconds(report.maximum_success_gap_seconds),
                ),
                ("Mean fetch time", _milliseconds(report.average_fetch_ms)),
                ("Mean parse time", _milliseconds(report.average_parse_ms)),
                ("Mean persist time", _milliseconds(report.average_persist_ms)),
                ("Total bytes fetched", _megabytes(report.total_response_bytes)),
            ],
        ),
        "Session rules and their interpretation: `RESULTS.md`. "
        "Machine-readable form: `--format json`.",
    ]
    return "\n".join(lines)


def _emit_json(event: dict[str, object]) -> None:
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chargeviz",
        description="Collect MFG EVSE status snapshots and estimate charging sessions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="run the polite sequential collector")
    collect.add_argument("--db", type=Path, default=Path("data/chargeviz.sqlite3"))
    collect.add_argument("--archive-dir", type=Path, default=Path("data/raw"))
    collect.add_argument("--no-archive", action="store_true")
    collect.add_argument("--duration", type=_duration_argument, default=7200.0)
    collect.add_argument("--interval", type=float, default=120.0)
    collect.add_argument("--timeout", type=float, default=30.0)
    collect.add_argument("--url", default=DEFAULT_URL)

    report = subparsers.add_parser("report", help="recompute session and run metrics")
    report.add_argument("--db", type=Path, default=Path("data/chargeviz.sqlite3"))
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "collect":
        try:
            config = CollectorConfig(
                interval_seconds=args.interval,
                duration_seconds=args.duration,
            )
            summary = Collector(
                database=Database(args.db),
                client=HTTPClient(timeout_seconds=args.timeout),
                source_url=args.url,
                config=config,
                archive_dir=None if args.no_archive else args.archive_dir,
                emit=_emit_json,
            ).run()
        except ConcurrentCollectorError as error:
            print(f"error: {error}", file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            print(
                json.dumps({"event": "collection_interrupted"}, separators=(",", ":")),
                file=sys.stderr,
            )
            return 130
        except ValueError as error:
            parser.error(str(error))
        return 0 if summary.success_count > 0 else 2

    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")
    report = analyze(args.db)
    content = (
        json.dumps(asdict(report), indent=2, sort_keys=True)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
