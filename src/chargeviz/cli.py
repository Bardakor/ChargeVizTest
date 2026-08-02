from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True, slots=True)
class _Section:
    """One block of the report. The header row is only shown when it carries information."""

    title: str
    header: tuple[str, ...]
    rows: list[tuple[str, ...]]
    note: str | None = None

    @property
    def labelled(self) -> bool:
        return len(self.header) > 2


@dataclass(frozen=True, slots=True)
class _ReportView:
    headline: str
    meta: list[str]
    sections: list[_Section]
    footer: str


def _view(report: AnalysisReport) -> _ReportView:
    """The single content model behind all three renderers, so they cannot drift apart."""
    complete = report.completed_session_count
    terminal_total = sum(report.terminal_status_counts.values()) or 1
    return _ReportView(
        headline=(
            f"Average session duration: {_minutes(report.average_duration_seconds)} "
            f"across {complete} complete charging episodes."
        ),
        meta=[
            f"Observation window (UTC): {_window(report)}",
            f"Fleet observed: {report.distinct_evse_count:,} EVSEs, "
            f"{report.status_change_count:,} status changes recorded",
        ],
        sections=[
            _Section(
                title="Session duration",
                note="Complete episodes only — both the start and the end were observed.",
                header=("Metric", "Value"),
                rows=[
                    ("Mean", _minutes(report.average_duration_seconds)),
                    ("Median", _minutes(report.median_duration_seconds)),
                    ("P90 (nearest rank)", _minutes(report.p90_duration_seconds)),
                    ("Shortest", _minutes(report.minimum_duration_seconds)),
                    ("Longest", _minutes(report.maximum_duration_seconds)),
                    ("Complete episodes (the denominator)", f"{complete}"),
                ],
            ),
            _Section(
                title="Episodes excluded from the mean",
                note="Counted and published rather than guessed at.",
                header=("Reason", "Episodes"),
                rows=[
                    (
                        "Left-censored — already charging when first seen",
                        str(report.left_censored_session_count),
                    ),
                    (
                        "Right-censored — still charging when the run ended",
                        str(report.right_censored_session_count),
                    ),
                    (
                        "Ambiguous — ended in UNKNOWN or an unmapped status",
                        str(report.ambiguous_session_count),
                    ),
                    ("Invalid — non-positive duration", str(report.invalid_duration_count)),
                ],
            ),
            _Section(
                title="How complete episodes ended",
                header=("Terminal status", "Count", "Share"),
                rows=[
                    (status, str(count), f"{count / terminal_total * 100:.1f} %")
                    for status, count in sorted(
                        report.terminal_status_counts.items(), key=lambda item: -item[1]
                    )
                ],
            ),
            _Section(
                title="Sensitivity checks",
                header=("Check", "Value"),
                rows=[
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
                        "Mean over gap-free episodes (a short-session bound, not a cleaner one)",
                        _minutes(report.gap_free_average_duration_seconds),
                    ),
                ],
            ),
            _Section(
                title="Run quality",
                header=("Metric", "Value"),
                rows=[
                    ("Poll attempts", str(report.poll_attempt_count)),
                    ("Successful", str(report.successful_poll_count)),
                    ("Rate-limited (HTTP 429)", str(report.rate_limited_poll_count)),
                    ("Other failures", str(report.failed_poll_count)),
                    ("Left incomplete (RUNNING)", str(report.running_poll_count)),
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
        ],
        footer=(
            "Session rules and their interpretation: RESULTS.md. "
            "Machine-readable form: --format json."
        ),
    )


def render_markdown(report: AnalysisReport) -> str:
    view = _view(report)
    lines = ["# ChargeViz run report", "", f"**{view.headline}**", ""]
    lines += [f"- {item}" for item in view.meta]
    for section in view.sections:
        lines += ["", f"## {section.title}", ""]
        if section.note:
            lines += [section.note, ""]
        alignment = ["---"] + ["---:"] * (len(section.header) - 1)
        lines.append("| " + " | ".join(section.header) + " |")
        lines.append("|" + "|".join(alignment) + "|")
        lines += ["| " + " | ".join(row) + " |" for row in section.rows]
    lines += ["", view.footer]
    return "\n".join(lines)


def _plain_table(section: _Section) -> list[str]:
    grid = ([section.header] if section.labelled else []) + section.rows
    if not grid:
        return ["  (nothing recorded)"]
    widths = [max(len(row[index]) for row in grid) for index in range(len(section.header))]
    lines: list[str] = []
    for position, row in enumerate(grid):
        cells = [row[0].ljust(widths[0])]
        cells += [row[index].rjust(widths[index]) for index in range(1, len(widths))]
        lines.append(("  " + "  ".join(cells)).rstrip())
        if section.labelled and position == 0:
            lines.append("  " + "  ".join("─" * width for width in widths))
    return lines


def render_text(report: AnalysisReport) -> str:
    """Aligned plain-text tables, standard library only. Used when rich is not installed."""
    view = _view(report)
    lines = ["ChargeViz run report", "", view.headline, ""]
    lines += [f"  {item}" for item in view.meta]
    for section in view.sections:
        lines += ["", section.title, "─" * len(section.title)]
        if section.note:
            lines += [section.note, ""]
        lines += _plain_table(section)
    lines += ["", view.footer]
    return "\n".join(lines)


def print_rich(report: AnalysisReport) -> bool:
    """Print with rich if it is installed. Returns False so callers can fall back."""
    try:
        from rich import box
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        return False

    view = _view(report)
    console = Console()
    console.print(f"\n[bold]{view.headline}[/bold]")
    for item in view.meta:
        console.print(f"[dim]{item}[/dim]")
    for section in view.sections:
        # Title and note are printed above the box rather than passed to Table, whose
        # caption renders underneath — the note introduces the table, so it goes first.
        console.print(f"\n[bold]{section.title}[/bold]")
        if section.note:
            console.print(f"[dim]{section.note}[/dim]")
        table = Table(box=box.ROUNDED, show_header=section.labelled, header_style="bold")
        table.add_column(section.header[0], justify="left", overflow="fold")
        for name in section.header[1:]:
            table.add_column(name, justify="right", no_wrap=True)
        for row in section.rows:
            table.add_row(*row)
        console.print(table)
    console.print(f"\n[dim]{view.footer}[/dim]\n")
    return True


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
    report.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="text for the terminal (default), markdown to paste, json for machines",
    )
    report.add_argument("--output", type=Path)
    return parser


def _run_collect(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        summary = Collector(
            database=Database(args.db),
            client=HTTPClient(timeout_seconds=args.timeout),
            source_url=args.url,
            config=CollectorConfig(
                interval_seconds=args.interval,
                duration_seconds=args.duration,
            ),
            archive_dir=None if args.no_archive else args.archive_dir,
            emit=_emit_json,
        ).run()
    except ConcurrentCollectorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(
            json.dumps({"event": "collection_interrupted"}, separators=(",", ":")), file=sys.stderr
        )
        return 130
    except ValueError as error:
        parser.error(str(error))
    return 0 if summary.success_count > 0 else 2


def _run_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")
    report = analyze(args.db)

    # rich draws straight to the terminal so it can use colour and the real width. A file
    # must never receive escape codes, so writing to --output always takes the plain path.
    if args.format == "text" and args.output is None and print_rich(report):
        return 0

    if args.format == "json":
        content = json.dumps(asdict(report), indent=2, sort_keys=True)
    elif args.format == "markdown":
        content = render_markdown(report)
    else:
        content = render_text(report)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "collect":
        return _run_collect(args, parser)
    return _run_report(args, parser)


if __name__ == "__main__":
    sys.exit(main())
