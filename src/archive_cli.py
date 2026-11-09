"""Command-line access to the SQLite result archive (read-only on the database).

Run from the project root:

    python -m src.archive_cli [--db PATH | --config PATH] COMMAND ...

Commands: ``list``, ``show RUN_ID``, ``compare RUN_ID_A RUN_ID_B`` and
``export RUN_ID OUTPUT_PATH``. ``--db`` and ``--config`` are global options and go
before the command.

Database selection:
- ``--db PATH`` opens that file; a relative path is resolved against the current working
  directory. The configuration is not read, so it works whether or not archiving is
  enabled (or configured at all).
- Otherwise ``result_management.database_path`` from the configuration (``--config``, or
  the project's config/config.yaml) is used, with its usual project-root semantics.

No command creates a database, changes one, or starts the emulators.

Exit codes: 0 success, 1 expected operational failure (missing database or run, invalid
archive, export destination exists, configuration problem), 2 invalid command-line usage.
Expected failures print one ``error:`` line on stderr; unexpected errors keep their traceback.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.testing.archive import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ArchiveError,
    ResultArchive,
    validate_run_id,
)
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
    return parser


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
        else:
            written = archive.export_run(args.run_id, args.output)
            print(f"Exported run {args.run_id} to {written}")
    except (ArchiveError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
