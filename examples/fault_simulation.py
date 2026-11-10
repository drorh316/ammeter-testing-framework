"""Fault-simulation demo: the real client and sampling loop against injected faults.

Run from the project root:

    python -m examples.fault_simulation [--db PATH] [--output-dir DIR]

It starts a local fault server (``src/testing/fault_server.py``) on a port chosen by the
operating system and samples it 5 times at 2 Hz with a 1 s request timeout, through
AmmeterTestFramework, the real TCP client, analysis and the SQLite archive. Scenario
``basic-recovery`` v1 makes the server answer connection by connection:

    1 real Greenlee reading            -> success
    2 reply "12.3.4"                   -> InvalidMeasurementError (not a number)
    3 orderly close without a reply    -> InvalidMeasurementError (empty response)
    4 reply held longer than 1 s       -> AmmeterTimeoutError
    5 real Greenlee reading            -> success (recovery)

The order of outcomes is repeatable; the current values and timings are not. The existing
client, framework, sampling, analysis and archive code, the Greenlee measurement logic and
the check_archived helper from examples/run_tests.py are reused unchanged. main.py, the
normal sampling demo's entry point and the supplied emulator servers are not started.

Outputs:
- the run, saved in ``--db`` (default ``results/fault_simulation.sqlite3`` under the
  project root, kept apart from the normal archive) under the device name
  ``fault-sim:greenlee:basic-recovery:v1``;
- ``<run_id>.scenario.json``: the scenario, its settings, the server's connection log,
  cleanup evidence, expected vs. observed outcomes and the verdict;
- ``<run_id>.html``: the run report, with the scenario section.
Both files go to ``--output-dir`` (default ``results/fault_simulation/`` under the project
root) and are never overwritten. Explicit relative ``--db`` and ``--output-dir`` paths are
resolved against the current working directory.

Saving the run and writing the two files are separate operations, not one transaction: if
writing a file fails, the run stays saved, and the run ID and any file already written are
reported.

Exit codes: 0 only if the scenario happened exactly as planned (including recovery and a
complete server cleanup), the run was saved and reloaded unchanged, and both files were
written. The injected failures are expected and do not by themselves cause a non-zero exit.
1 otherwise; 2 for invalid command-line usage.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from examples.run_tests import check_archived
from src.testing.archive import ArchiveError, ResultArchive, ResultArchiveError, write_new_file
from src.testing.fault_scenario import BASIC_RECOVERY, VERDICT_PASS, compare_scenario, scenario_record
from src.testing.fault_server import HOST, FaultServerError, FaultSimulationServer
from src.testing.test_framework import AmmeterTestFramework
from src.testing.visualization import render_run_report
from src.utils.config import PROJECT_ROOT, ConfigError

EXIT_OK, EXIT_FAILURE = 0, 1
DEFAULT_DB = PROJECT_ROOT / "results" / "fault_simulation.sqlite3"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results" / "fault_simulation"

SAMPLING = {"sampling_frequency_hz": 2.0, "measurements_count": 5, "total_duration_seconds": None,
            "request_timeout_seconds": 1.0}
DELAY_FACTOR = 3  # the delayed reply is held for 3x the client's timeout (cleanup cuts it short)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m examples.fault_simulation",
        description="Sample a local fault-injecting server through the real client, sampling, "
                    "analysis and archive, and check the planned outcomes.",
        epilog="Exit codes: 0 scenario as expected and outputs saved, 1 otherwise, 2 usage error.")
    parser.add_argument("--db", type=Path, metavar="PATH",
                        help="archive database (relative to the current directory; "
                             "default results/fault_simulation.sqlite3 under the project root)")
    parser.add_argument("--output-dir", type=Path, metavar="DIR",
                        help="directory for <run_id>.scenario.json and <run_id>.html (relative to the current "
                             "directory; default results/fault_simulation/ under the project root)")
    return parser


def _absolute(path: Optional[Path], default: Path) -> Path:
    if path is None:
        return default
    return path if path.is_absolute() else Path.cwd() / path


def write_config(directory: Path, device: str, port: int, command: bytes, database: Path) -> Path:
    """A temporary configuration for one device at the fault server, archiving to ``database``."""
    config = {
        "testing": {"sampling": dict(SAMPLING)},
        "ammeters": {device: {"host": HOST, "port": port, "command": command.decode("ascii")}},
        "result_management": {"archive_enabled": True, "database_path": str(database)},
    }
    path = directory / "fault_simulation.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _expected_text(expected: Optional[Dict[str, Any]]) -> str:
    if expected is None:
        return "(not planned)"
    if expected["outcome"] == "ok":
        return "success"
    phrase = f" ({expected['message_contains']})" if expected["message_contains"] else ""
    return f"{expected['type']}{phrase}"


def _observed_text(observed: Optional[Dict[str, Any]]) -> str:
    if observed is None:
        return "(no attempt)"
    if observed["outcome"] == "ok":
        return "success"
    return f"{observed['type']}: {observed['message']}"


def summary_lines(record: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    comparison = record["comparison"]
    lines = [f"run {record['run_id']}, device {record['device']}",
             f"attempted {result['counts']['attempted']}, succeeded {result['counts']['succeeded']}, "
             f"failed {result['counts']['failed']}, skipped slots {result['counts']['skipped_slots']}",
             "", "step  fault                    expected -> observed"]
    for row in comparison["steps"]:
        lines.append(f"{row['step']:>4}  {str(row['fault']):<23}  {_expected_text(row['expected'])} -> "
                     f"{_observed_text(row['observed'])}  [{'match' if row['match'] else 'MISMATCH'}]")
    lines.append("")
    lines.append("server connections (acceptance order):")
    for c in record["connections"]:
        lines.append(f"  #{c['connection']} {c['fault']}: command {c['command']}, reply sent {c['reply_sent']}"
                     + ("" if c["delay_interrupted"] is None else f", delay interrupted {c['delay_interrupted']}")
                     + f", finished {c['finished']}, socket closed {c['socket_closed']}"
                     + (f", ERROR {c['error']['type']}: {c['error']['message']}" if c["error"] else ""))
    cleanup = record["cleanup"]
    lines.append(f"server cleanup: complete {cleanup['complete']} (accept thread stopped "
                 f"{cleanup['accept_thread_stopped']}, listener closed {cleanup['listener_closed']}, handlers stopped "
                 f"{cleanup['handlers_stopped']}/{cleanup['handlers']}, connection sockets closed "
                 f"{cleanup['connection_sockets_closed']}/{cleanup['connection_sockets']}, "
                 f"{cleanup['elapsed_s'] * 1000:.1f} ms)")
    lines.append(f"recovered after the faults: {comparison['recovered']}")
    for problem in comparison["problems"]:
        lines.append(f"PROBLEM: {problem}")
    lines.append(f"scenario verdict: {record['verdict']}")
    return lines


def write_outputs(output_dir: Path, run: Dict[str, Any], record: Dict[str, Any]) -> List[Path]:
    """Write the scenario record, then the HTML report; return the files written.

    Each document is generated before its file is created. On a write failure ExportError
    is raised; ``written`` on the exception lists the files that were already written.
    """
    written: List[Path] = []
    documents = [
        (output_dir / f"{run['run_id']}.scenario.json",
         lambda: (json.dumps(record, indent=2, allow_nan=False) + "\n").encode("utf-8")),
        (output_dir / f"{run['run_id']}.html",
         lambda: render_run_report(run, scenario=record).encode("utf-8")),
    ]
    for path, build in documents:
        try:
            written.append(write_new_file(path, build()))
        except ArchiveError as exc:
            exc.written = list(written)
            raise
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)  # exits with status 2 on invalid usage
    database = _absolute(args.db, DEFAULT_DB)
    output_dir = _absolute(args.output_dir, DEFAULT_OUTPUT_DIR)
    scenario = BASIC_RECOVERY
    device = scenario.device_name
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create the output directory {output_dir}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    delay_s = DELAY_FACTOR * SAMPLING["request_timeout_seconds"]
    save_error: Optional[str] = None
    try:
        with FaultSimulationServer(scenario.steps, delay_s=delay_s) as server:
            print(f"fault server listening on {HOST}:{server.port}; scenario {scenario.scenario_id} "
                  f"v{scenario.version}; archive {database}")
            with tempfile.TemporaryDirectory() as tmp:
                framework = AmmeterTestFramework(write_config(Path(tmp), device, server.port, server.command, database))
            try:
                result = framework.run_test(device)
            except ResultArchiveError as exc:  # collected but not saved: keep it and report it
                result = exc.result
                save_error = f"{type(exc.__cause__).__name__}: {exc.__cause__}"
    except (FaultServerError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    archive_line, archived = check_archived(framework.archive, result, save_error)
    comparison = compare_scenario(scenario, result["attempts"], server.connections(), server.errors(),
                                  server.cleanup)
    record = scenario_record(scenario, result, server.settings, server.connections(), server.errors(),
                             server.cleanup, comparison)

    print("\n=== Fault-simulation summary ===")
    print("\n".join(summary_lines(record, result)))
    print(archive_line.strip())
    try:
        written = write_outputs(output_dir, result, record)
    except ArchiveError as exc:
        already = ", ".join(str(p) for p in getattr(exc, "written", [])) or "none"
        saved = "saved in the archive" if archived else "NOT reliably archived"
        print(f"error: run {result['run_id']} ({saved}): could not write its output files: {exc}; "
              f"files already written: {already}", file=sys.stderr)
        return EXIT_FAILURE
    for path in written:
        print(f"wrote {path}")

    if comparison["verdict"] == VERDICT_PASS and archived:
        print("\nRESULT: the planned faults occurred in order, the client and sampling recovered, and the run "
              "was archived and reloaded unchanged. The failed attempts above are the intended faults.")
        return EXIT_OK
    print("\nRESULT: NOT as expected (scenario mismatch, server error, incomplete cleanup or archive failure)")
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
