"""Command-line access to the SQLite result archive (read-only on the database).

Run from the project root:

    python -m src.archive_cli [--db PATH | --config PATH] COMMAND ...

Commands: ``list``, ``show RUN_ID``, ``compare RUN_ID_A RUN_ID_B``,
``export RUN_ID OUTPUT_PATH``, ``performance RUN_ID [limits]``,
``plot RUN_ID OUTPUT_PATH [limits]`` (a self-contained HTML report) and
``accuracy RUN_ID --reference-a AMPERES --reference-source TEXT``. ``--db`` and
``--config`` are global options and go before the command.

Database selection:
- ``--db PATH`` opens that file; a relative path is resolved against the current working
  directory. The configuration is not read, so it works whether or not archiving is
  enabled (or configured at all).
- Otherwise ``result_management.database_path`` from the configuration (``--config``, or
  the project's config/config.yaml) is used, with its usual project-root semantics.

No command creates a database, changes one, or starts the emulators.

Exit codes: 0 success, 1 expected operational failure (missing database or run, invalid
archive, export destination exists, configuration problem), 2 invalid command-line usage.
``performance`` also exits with 1, after printing the complete report, when a configured
limit fails or cannot be evaluated (status ``fail`` or ``insufficient_data``). ``accuracy``
exits with 0 for status ``ok`` or ``no_data`` and with 1, after printing the complete
report, for status ``error`` (a result that cannot be represented as a finite float).
Expected failures print one ``error:`` line on stderr; unexpected errors keep their traceback.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.testing.accuracy import assess_run
from src.testing.analysis import STATUS_ERROR
from src.testing.archive import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ArchiveError,
    ResultArchive,
    validate_run_id,
    write_new_file,
)
from src.testing.performance import (
    STATUS_NOT_EVALUATED,
    STATUS_PASS,
    PerformanceLimits,
    build_performance_report,
)
from src.testing.visualization import render_run_report
from src.utils.config import ConfigError, load_config, parse_result_management

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# --- Argument types (a ValueError here makes argparse exit with status 2) ---------------

def _run_id(text: str) -> str:
    try:
        return validate_run_id(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _aware_datetime(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"timestamp needs a UTC offset, e.g. {text}+00:00 or {text}Z: {text!r}")
    return moment


def _ammeter(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("must be a non-empty device name")
    return text


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError(f"must be a finite number, got {text!r}")
    return value


def _seconds_limit(text: str) -> float:
    value = _number(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be at least 0 seconds, got {text!r}")
    return value


def _rate_limit(text: str) -> float:
    value = _number(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {text!r}")
    return value


def _reference_source(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("must describe where the reference comes from (not blank)")
    return text


def _limit(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from exc
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise argparse.ArgumentTypeError(f"must be from 1 to {MAX_LIST_LIMIT}, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.archive_cli",
        description="List, show, compare and export archived ammeter runs.",
        epilog="--db/--config go before the command. Exit codes: 0 ok, 1 operational failure, 2 usage error.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path, metavar="PATH",
                        help="archive database to read (relative to the current directory); "
                             "the configuration is not used")
    source.add_argument("--config", type=Path, metavar="PATH",
                        help="configuration file whose result_management.database_path is used "
                             "(default: the project's config/config.yaml)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    list_parser = commands.add_parser("list", help="summarize archived runs, newest first")
    list_parser.add_argument("--ammeter", type=_ammeter, help="only runs of this device (exact, case-sensitive match)")
    list_parser.add_argument("--since", type=_aware_datetime, metavar="TIMESTAMP",
                             help="runs started at or after this time, e.g. 2026-11-08T00:00:00Z")
    list_parser.add_argument("--until", type=_aware_datetime, metavar="TIMESTAMP",
                             help="runs started before this time")
    list_parser.add_argument("--limit", type=_limit, default=DEFAULT_LIST_LIMIT,
                             help=f"maximum number of runs (1-{MAX_LIST_LIMIT}, default {DEFAULT_LIST_LIMIT})")

    show_parser = commands.add_parser("show", help="print one complete run as JSON")
    show_parser.add_argument("run_id", type=_run_id, metavar="RUN_ID")

    compare_parser = commands.add_parser("compare", help="compare two runs side by side (JSON)")
    compare_parser.add_argument("run_id_a", type=_run_id, metavar="RUN_ID_A")
    compare_parser.add_argument("run_id_b", type=_run_id, metavar="RUN_ID_B")

    export_parser = commands.add_parser("export", help="write one complete run to a new JSON file")
    export_parser.add_argument("run_id", type=_run_id, metavar="RUN_ID")
    export_parser.add_argument("output", type=Path, metavar="OUTPUT_PATH",
                               help="new file (relative to the current directory); never overwritten")

    performance_parser = commands.add_parser(
        "performance", help="timing and failure consistency of one run, with optional limits (JSON)",
        description="Report failure rate, skipped-slot rate, start lateness and request duration for one "
                    "archived run. Limits are optional and user-defined; a value equal to a limit passes. "
                    "Exit 1 (after printing the report) if a configured limit fails or cannot be evaluated.")
    performance_parser.add_argument("run_id", type=_run_id, metavar="RUN_ID")
    _add_limit_options(performance_parser)

    plot_parser = commands.add_parser(
        "plot", help="write a self-contained HTML report with SVG charts for one run",
        description="Write one run's details, statistics, performance summary and charts to a new HTML file "
                    "(no scripts or external resources). The file is never overwritten. Optional limits are "
                    "shown in the report's performance section; they do not change the exit code.")
    plot_parser.add_argument("run_id", type=_run_id, metavar="RUN_ID")
    plot_parser.add_argument("output", type=Path, metavar="OUTPUT_PATH",
                             help="new .html file (relative to the current directory); never overwritten")
    _add_limit_options(plot_parser)

    accuracy_parser = commands.add_parser(
        "accuracy", help="errors of one run's readings against a supplied reference current (JSON)",
        description="Report bias, mean absolute error, RMSE and the spread of one archived run's successful readings "
                    "against a constant reference current that you supply. The reference is not stored, and "
                    "supplying it does not establish that this current was applied during the run. The supplied "
                    "emulators return random values unrelated to any applied current, so for their runs the result "
                    "is only the distance from your value, not device accuracy. Exit 1 (after printing the report) "
                    "if a result cannot be represented.")
    accuracy_parser.add_argument("run_id", type=_run_id, metavar="RUN_ID")
    accuracy_parser.add_argument("--reference-a", type=_number, required=True, metavar="AMPERES",
                                 help="the known true current, in amperes (finite; zero and negative allowed)")
    accuracy_parser.add_argument("--reference-source", type=_reference_source, required=True, metavar="TEXT",
                                 help="where the reference comes from, e.g. a calibrator and its certificate")
    return parser


def _add_limit_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-start-lateness", type=_seconds_limit, metavar="SECONDS",
                        help="limit for the largest start lateness, in seconds")
    parser.add_argument("--max-failure-rate", type=_rate_limit, metavar="RATE",
                        help="limit for failed / attempted (0 to 1)")
    parser.add_argument("--max-skipped-slot-rate", type=_rate_limit, metavar="RATE",
                        help="limit for skipped / (attempted + skipped) (0 to 1)")


def _limits(args: argparse.Namespace) -> PerformanceLimits:
    return PerformanceLimits(max_start_lateness_s=args.max_start_lateness, max_failure_rate=args.max_failure_rate,
                             max_skipped_slot_rate=args.max_skipped_slot_rate)


def _database_path(args: argparse.Namespace) -> Path:
    if args.db is not None:
        return args.db
    settings = parse_result_management(load_config(args.config))
    if settings.database_path is None:
        raise ConfigError("no archive database is configured (result_management.database_path); "
                          "pass --db PATH")
    return settings.database_path


def format_run_table(runs: List[Dict[str, Any]]) -> str:
    """A compact text table of list_runs() summaries."""
    if not runs:
        return "No archived runs match."
    header = ("STARTED (UTC)", "RUN ID", "AMMETER", "OK/ATTEMPTS", "SKIPPED", "STOP", "ANALYSIS", "MEAN (A)")
    rows = [(
        run["started_at_utc"][:23].replace("T", " "),
        run["run_id"],
        run["ammeter"],
        f"{run['counts']['succeeded']}/{run['counts']['attempted']}",
        str(run["counts"]["skipped_slots"]),
        run["stop_reason"],
        run["analysis_status"],
        "n/a" if run["mean_a"] is None else f"{run['mean_a']:.6g}",
    ) for run in runs]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in (header, *rows)]
    return "\n".join(lines)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, allow_nan=False)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)  # exits with status 2 on invalid usage
    if args.command == "list" and args.since is not None and args.until is not None \
            and args.since >= args.until:
        print("error: --since must be earlier than --until", file=sys.stderr)
        return EXIT_USAGE

    try:
        archive = ResultArchive(_database_path(args))
        if args.command == "list":
            runs = archive.list_runs(ammeter=args.ammeter, since=args.since, until=args.until, limit=args.limit)
            print(format_run_table(runs))
        elif args.command == "show":
            print(_json(archive.load_run(args.run_id)))
        elif args.command == "compare":
            print(_json(archive.compare_runs(args.run_id_a, args.run_id_b)))
        elif args.command == "performance":
            report = build_performance_report(archive.load_run(args.run_id), _limits(args))
            print(_json(report))
            status = report["thresholds"]["status"]
            if status not in (STATUS_PASS, STATUS_NOT_EVALUATED):
                print(f"performance limits: {status}", file=sys.stderr)
                return EXIT_FAILURE
        elif args.command == "accuracy":
            report = assess_run(archive.load_run(args.run_id), args.reference_a,
                                reference_source=args.reference_source)
            print(_json(report))
            if report["status"] == STATUS_ERROR:
                print(f"accuracy: {report['error']['message']}", file=sys.stderr)
                return EXIT_FAILURE
        elif args.command == "plot":
            document = render_run_report(archive.load_run(args.run_id), _limits(args)).encode("utf-8")
            written = write_new_file(args.output, document)  # the document exists before the file is created
            print(f"Wrote the report for run {args.run_id} to {written}")
        else:
            written = archive.export_run(args.run_id, args.output)
            print(f"Exported run {args.run_id} to {written}")
    except (ArchiveError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
