from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict
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


def _minutes(value: float | None) -> str:
    return "not estimable" if value is None else f"{value / 60:.2f}"


def render_markdown(report: AnalysisReport) -> str:
    return "\n".join(
        [
            "## Recomputed metrics",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Poll attempts | {report.poll_attempt_count} |",
            f"| Successful polls | {report.successful_poll_count} |",
            f"| Rate-limited polls | {report.rate_limited_poll_count} |",
            f"| Other failed polls | {report.failed_poll_count} |",
            f"| Incomplete (`RUNNING`) polls | {report.running_poll_count} |",
            f"| Distinct EVSEs | {report.distinct_evse_count} |",
            f"| Status changes | {report.status_change_count} |",
            f"| Complete sessions | {report.completed_session_count} |",
            f"| Average duration (minutes) | {_minutes(report.average_duration_seconds)} |",
            "| Observation-time average (minutes) | "
            f"{_minutes(report.observation_time_average_duration_seconds)} |",
            f"| Median duration (minutes) | {_minutes(report.median_duration_seconds)} |",
            f"| P90 duration (minutes) | {_minutes(report.p90_duration_seconds)} |",
            f"| Left-censored sessions | {report.left_censored_session_count} |",
            f"| Right-censored sessions | {report.right_censored_session_count} |",
            f"| Ambiguous (`UNKNOWN`) sessions | {report.ambiguous_session_count} |",
            f"| Observation-time fallbacks | {report.observation_time_fallback_count} |",
            f"| Gap-affected complete sessions | {report.gap_affected_session_count} |",
            f"| Gap-free complete sessions | {report.gap_free_session_count} |",
            "| Gap-free average duration (minutes) | "
            f"{_minutes(report.gap_free_average_duration_seconds)} |",
            "| Terminal statuses | "
            f"`{json.dumps(report.terminal_status_counts, sort_keys=True)}` |",
            "",
            f"Observation window (UTC): `{report.first_poll_started_at}` to "
            f"`{report.last_poll_completed_at}`.",
        ]
    )


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
