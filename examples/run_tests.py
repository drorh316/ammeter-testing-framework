"""Sampling demo: sample every configured ammeter from the running emulators.

Run from the project root:

    python -m examples.run_tests

It validates config/config.yaml, starts the three emulators with the same functions and
fixed startup delay as main.py, runs AmmeterTestFramework.run_test() for each configured
ammeter, and prints a summary per run. The emulators print their own lines while sampling.
If archiving is enabled, each run is reloaded from the archive through a new connection
and compared with the in-memory result.

The exit code is 0 only if every ammeter had at least one attempt, no attempt failed, the
statistics were calculated, and (when archiving is enabled) the run was archived and
reloaded unchanged. Collected results are still shown when a later step fails.
"""
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from main import run_circutor_emulator, run_entes_emulator, run_greenlee_emulator
from src.testing.archive import ArchiveError, ResultArchive, ResultArchiveError
from src.testing.test_framework import AmmeterTestFramework

STARTUP_DELAY_SECONDS = 5  # same fixed delay as main.py


def _amperes(value) -> str:
    """Display a metric rounded to 6 significant digits, or N/A if unavailable."""
    return "N/A" if value is None else f"{value:.6g} A"


def format_statistics(analysis: Dict[str, Any]) -> List[str]:
    """Summary lines for the ``analysis`` section of a run_test() result."""
    if analysis["status"] == "error":
        error = analysis["error"]
        return [f"  statistics of {analysis['count']} successful measurement(s): UNAVAILABLE, "
                f"analysis failed ({error['type']}: {error['message']})"]
    return [
        f"  statistics of {analysis['count']} successful measurement(s): "
        f"mean {_amperes(analysis['mean_a'])}, median {_amperes(analysis['median_a'])}, "
        f"population std dev {_amperes(analysis['stddev_population_a'])}",
        f"  min {_amperes(analysis['min_a'])}, max {_amperes(analysis['max_a'])}",
    ]


def summarize_run(name: str, result: Dict[str, Any]) -> Tuple[List[str], bool]:
    """Summary lines for one run_test() result, and whether it demonstrates successful sampling.

    A run demonstrates success only if it made at least one attempt, none failed, and
    its statistics were calculated.
    """
    counts = result["counts"]
    attempts = result["attempts"]
    analysis_ok = result["analysis"]["status"] != "error"
    lines = [
        f"{name} (port {result['port']}): stop reason {result['stop_reason']}, elapsed {result['elapsed_s']:.3f} s",
        f"  run {result['run_id']}, started {result['started_at_utc']}",
        f"  attempted {counts['attempted']}, succeeded {counts['succeeded']}, "
        f"failed {counts['failed']}, skipped slots {counts['skipped_slots']}",
        *format_statistics(result["analysis"]),
    ]
    if not attempts:
        lines.append("  timing: unavailable (no attempts were made)")
        lines.append("  NO MEASUREMENTS: this run does not demonstrate sampling")
        return lines, False

    lateness_ms = [a["lateness_s"] * 1000 for a in attempts]
    lines.append(f"  start lateness vs. target: min {min(lateness_ms):.3f} ms, max {max(lateness_ms):.3f} ms")
    for a in attempts:
        outcome = f"{a['current_a']:.6g} A" if a["ok"] else f"FAILED {a['error']['type']}: {a['error']['message']}"
        lines.append(f"  #{a['attempt']:>2} slot {a['slot']:>2}  target {a['scheduled_offset_s']:.3f} s  "
                     f"start {a['start_offset_s']:.4f} s  took {a['elapsed_s'] * 1000:.2f} ms  {outcome}")
    return lines, counts["failed"] == 0 and analysis_ok


def check_archived(archive: Optional[ResultArchive], result: Dict[str, Any],
                   storage_error: Optional[str]) -> Tuple[str, bool]:
    """Archive status line for one run, and whether archiving succeeded (or is disabled).

    A saved run is reloaded through a new ResultArchive (a new connection) and compared
    with the in-memory result.
    """
    if archive is None:
        return "  archive: disabled", True
    if storage_error is not None:
        return f"  archive: NOT SAVED ({storage_error})", False
    try:
        reloaded = ResultArchive(archive.path).load_run(result["run_id"])
    except ArchiveError as exc:
        return f"  archive: saved, but reloading FAILED ({type(exc).__name__}: {exc})", False
    if reloaded != result:
        return "  archive: saved, but the reloaded run DIFFERS from the collected result", False
    return f"  archive: saved and reloaded unchanged from {archive.path}", True


def main() -> int:
    # Load and validate the configuration before starting anything.
    framework = AmmeterTestFramework()

    for start_emulator in (run_greenlee_emulator, run_entes_emulator, run_circutor_emulator):
        threading.Thread(target=start_emulator, daemon=True).start()
    time.sleep(STARTUP_DELAY_SECONDS)

    results: Dict[str, Dict[str, Any]] = {}
    storage_errors: Dict[str, Optional[str]] = {}
    for name in framework.ammeters:
        try:
            results[name] = framework.run_test(name)
            storage_errors[name] = None
        except ResultArchiveError as exc:  # the run was collected; keep and report it
            results[name] = exc.result
            storage_errors[name] = f"{type(exc.__cause__).__name__}: {exc.__cause__}"

    settings = framework.sampling
    print("\n=== Sampling summary ===")
    print(f"frequency {settings.sampling_frequency_hz} Hz, count limit {settings.measurements_count}, "
          f"duration limit {settings.total_duration_seconds} s, request timeout {settings.request_timeout_seconds} s")
    not_demonstrated = []
    for name, result in results.items():
        lines, demonstrated = summarize_run(name, result)
        archive_line, archived = check_archived(framework.archive, result, storage_errors[name])
        print()
        print("\n".join(lines[:2] + [archive_line] + lines[2:]))
        if not (demonstrated and archived):
            not_demonstrated.append(name)

    if not_demonstrated:
        print(f"\nRESULT: NOT successful for: {', '.join(not_demonstrated)} "
              "(no attempts, failed attempts, failed analysis, or archive failure)")
        return 1
    print("\nRESULT: every configured ammeter was sampled, analysed"
          + (" and archived" if framework.archive else "") + " with no failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
