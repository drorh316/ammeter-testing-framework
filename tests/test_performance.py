"""Performance consistency report (src/testing/performance.py) and the ``performance`` CLI command."""
import json
import math
import statistics

import pytest

from Ammeters.client import AmmeterConnectionError
from src import archive_cli
from src.testing.performance import (
    PerformanceLimits,
    build_performance_report,
    overall_status,
    p95_nearest_rank,
    summarize_timing,
)
from src.utils.config import PROJECT_ROOT
from test_archive import snapshot
from test_archive_queries import archive_with, run_id, variant


def run(lateness, durations, ok=None, skipped=0):
    """A minimal loaded-run structure with the given per-attempt timings (seconds)."""
    ok = ok if ok is not None else [True] * len(lateness)
    attempts = []
    for number, (late, took, success) in enumerate(zip(lateness, durations, ok), start=1):
        attempt = {"attempt": number, "slot": number - 1, "lateness_s": late, "elapsed_s": took, "ok": success}
        if success:
            attempt["current_a"] = 1.0
        else:
            attempt["error"] = {"type": "AmmeterTimeoutError", "message": "timed out"}
        attempts.append(attempt)
    succeeded = sum(ok)
    return {
        "run_id": run_id(1), "ammeter": "greenlee", "started_at_utc": "2026-11-08T17:30:00.000000+00:00",
        "settings": {"sampling_frequency_hz": 5.0, "measurements_count": 10, "total_duration_seconds": None,
                     "request_timeout_seconds": 1.0},
        "counts": {"attempted": len(attempts), "succeeded": succeeded, "failed": len(attempts) - succeeded,
                   "skipped_slots": skipped},
        "attempts": attempts,
    }


# --- Timing summaries and p95 ---------------------------------------------------------------

def test_known_timing_values():
    summary = summarize_timing([0.004, 0.001, 0.003, 0.002])

    assert summary == {
        "count": 4,
        "mean_s": statistics.mean([0.004, 0.001, 0.003, 0.002]),
        "median_s": statistics.median([0.001, 0.002, 0.003, 0.004]),
        "stddev_population_s": statistics.pstdev([0.004, 0.001, 0.003, 0.002]),
        "p95_s": 0.004,
        "max_s": 0.004,
    }
    assert summary["mean_s"] == pytest.approx(0.0025) and summary["median_s"] == pytest.approx(0.0025)
    assert summary["stddev_population_s"] == pytest.approx(math.sqrt(1.25e-6))


@pytest.mark.parametrize("n, rank", [(1, 1), (2, 2), (10, 10), (19, 19), (20, 19), (21, 20), (40, 38), (100, 95), (101, 96)])
def test_p95_is_the_nearest_rank(n, rank):
    values = [float(i) for i in range(n, 0, -1)]  # n .. 1, unsorted on purpose
    assert p95_nearest_rank(values) == float(rank)
    assert rank == math.ceil(0.95 * n)


def test_p95_needs_values_and_timing_rejects_invalid_values():
    with pytest.raises(ValueError):
        p95_nearest_rank([])
    for bad in (math.nan, math.inf, True, "0.1", None):
        with pytest.raises(ValueError, match="finite number"):
            summarize_timing([0.1, bad])


def test_one_attempt_and_zero_attempts():
    one = build_performance_report(run([0.002], [0.005]))
    assert one["start_lateness_s"] == {"count": 1, "mean_s": 0.002, "median_s": 0.002,
                                       "stddev_population_s": 0.0, "p95_s": 0.002, "max_s": 0.002}
    assert one["rates"] == {"failure_rate": 0.0, "skipped_slot_rate": 0.0}

    zero = build_performance_report(run([], []))
    empty = {"count": 0, "mean_s": None, "median_s": None, "stddev_population_s": None, "p95_s": None, "max_s": None}
    assert zero["start_lateness_s"] == empty and zero["request_duration_s"] == empty
    assert zero["rates"] == {"failure_rate": None, "skipped_slot_rate": None}
    json.dumps(zero, allow_nan=False)


def test_failed_attempts_are_included_in_timing():
    report = build_performance_report(run([0.001, 0.009], [0.002, 1.0], ok=[True, False]))

    assert report["counts"] == {"attempted": 2, "succeeded": 1, "failed": 1, "skipped_slots": 0}
    assert report["start_lateness_s"]["count"] == 2 and report["start_lateness_s"]["max_s"] == 0.009
    assert report["request_duration_s"]["max_s"] == 1.0  # the failed attempt's (timed-out) duration
    assert report["rates"]["failure_rate"] == 0.5


@pytest.mark.parametrize("attempted, skipped, expected", [(3, 1, 0.25), (3, 0, 0.0), (0, 2, 1.0), (0, 0, None)])
def test_skipped_slot_rate_denominator_is_attempted_plus_skipped(attempted, skipped, expected):
    report = build_performance_report(run([0.0] * attempted, [0.0] * attempted, skipped=skipped))
    assert report["rates"]["skipped_slot_rate"] == expected


def test_report_keeps_full_precision_and_run_context():
    value = 0.00039328997954725663
    report = build_performance_report(run([value], [value]))

    assert report["start_lateness_s"]["max_s"] == value
    assert {key: report[key] for key in ("run_id", "ammeter", "started_at_utc")} == {
        "run_id": run_id(1), "ammeter": "greenlee", "started_at_utc": "2026-11-08T17:30:00.000000+00:00"}
    assert report["settings"]["sampling_frequency_hz"] == 5.0
    assert set(report) == {"run_id", "ammeter", "started_at_utc", "settings", "counts", "rates",
                           "start_lateness_s", "request_duration_s", "thresholds"}  # no current or accuracy data


def test_inconsistent_attempt_count_is_rejected():
    broken = run([0.1], [0.1])
    broken["counts"]["attempted"] = 2
    with pytest.raises(ValueError, match="attempt records"):
        build_performance_report(broken)


# --- Thresholds ---------------------------------------------------------------------------

def statuses(report):
    return report["thresholds"]["status"], {c["name"]: c["status"] for c in report["thresholds"]["checks"]}


def test_no_limits_means_not_evaluated():
    report = build_performance_report(run([0.5], [0.5], ok=[False], skipped=3))
    assert report["thresholds"] == {"status": "not_evaluated", "checks": []}


def test_values_equal_to_their_limits_pass():
    report = build_performance_report(
        run([0.001, 0.004], [0.0, 0.0], ok=[True, False], skipped=2),
        PerformanceLimits(max_start_lateness_s=0.004, max_failure_rate=0.5, max_skipped_slot_rate=0.5))

    assert statuses(report) == ("pass", {"max_start_lateness_s": "pass", "failure_rate": "pass",
                                         "skipped_slot_rate": "pass"})
    assert report["thresholds"]["checks"][0] == {"name": "max_start_lateness_s", "status": "pass",
                                                 "observed": 0.004, "limit": 0.004}


def test_any_failed_check_fails_the_report():
    report = build_performance_report(
        run([0.001, 0.0041], [0.0, 0.0]),
        PerformanceLimits(max_start_lateness_s=0.004, max_failure_rate=0.0))

    assert statuses(report) == ("fail", {"max_start_lateness_s": "fail", "failure_rate": "pass"})


def test_checks_without_data_are_insufficient_unless_another_fails():
    no_attempts = run([], [])
    insufficient = build_performance_report(no_attempts, PerformanceLimits(max_start_lateness_s=1.0, max_failure_rate=1.0))
    assert statuses(insufficient) == ("insufficient_data", {"max_start_lateness_s": "insufficient_data",
                                                             "failure_rate": "insufficient_data"})
    assert insufficient["thresholds"]["checks"][0]["observed"] is None

    skipped_only = run([], [], skipped=1)  # skipped-slot rate 1.0 can be evaluated; lateness cannot
    failed = build_performance_report(skipped_only, PerformanceLimits(max_start_lateness_s=1.0, max_skipped_slot_rate=0.5))
    assert statuses(failed) == ("fail", {"max_start_lateness_s": "insufficient_data", "skipped_slot_rate": "fail"})


def test_overall_status_precedence():
    def check(status):
        return {"name": "x", "status": status, "observed": None, "limit": 0.0}
    assert overall_status([]) == "not_evaluated"
    assert overall_status([check("pass"), check("pass")]) == "pass"
    assert overall_status([check("pass"), check("insufficient_data")]) == "insufficient_data"
    assert overall_status([check("insufficient_data"), check("fail"), check("pass")]) == "fail"


@pytest.mark.parametrize("kwargs, message", [
    ({"max_start_lateness_s": -0.001}, "at least 0"),
    ({"max_start_lateness_s": math.inf}, "finite number"),
    ({"max_start_lateness_s": True}, "finite number"),
    ({"max_start_lateness_s": "0.1"}, "finite number"),
    ({"max_failure_rate": 1.5}, "between 0 and 1"),
    ({"max_failure_rate": -0.1}, "between 0 and 1"),
    ({"max_failure_rate": math.nan}, "finite number"),
    ({"max_skipped_slot_rate": False}, "finite number"),
    ({"max_skipped_slot_rate": 2}, "between 0 and 1"),
])
def test_invalid_limits_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        PerformanceLimits(**kwargs)


def test_boundary_limits_are_accepted():
    limits = PerformanceLimits(max_start_lateness_s=0, max_failure_rate=0, max_skipped_slot_rate=1)
    assert (limits.max_start_lateness_s, limits.max_failure_rate, limits.max_skipped_slot_rate) == (0.0, 0.0, 1.0)


def test_committed_sample_report_matches_the_committed_run():
    # sample_results/greenlee_performance.json was generated by the CLI from the archived run that
    # sample_results/greenlee_run.json exports, with the example limits documented in its README.
    samples = PROJECT_ROOT / "sample_results"
    loaded_run = json.loads((samples / "greenlee_run.json").read_text(encoding="utf-8"))
    limits = PerformanceLimits(max_start_lateness_s=0.005, max_failure_rate=0, max_skipped_slot_rate=0)

    expected = json.loads((samples / "greenlee_performance.json").read_text(encoding="utf-8"))

    assert build_performance_report(loaded_run, limits) == expected


# --- CLI ------------------------------------------------------------------------------------

@pytest.fixture
def archive(tmp_path):
    return archive_with(tmp_path, variant(tmp_path, 1, outcomes=(1.0, AmmeterConnectionError(5001, "refused"), 3.0)))


def cli(capsys, *argv):
    code = archive_cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_report_without_limits_is_not_evaluated_and_leaves_the_database_unchanged(capsys, archive):
    before = archive.path.read_bytes()
    rows = snapshot(archive.path)

    code, out, err = cli(capsys, "--db", str(archive.path), "performance", run_id(1))

    report = json.loads(out, parse_constant=lambda name: pytest.fail(f"non-standard JSON {name}"))
    assert (code, err) == (0, "")
    assert report == build_performance_report(archive.load_run(run_id(1)))
    assert report["thresholds"]["status"] == "not_evaluated"
    assert report["rates"]["failure_rate"] == pytest.approx(1 / 3)
    assert archive.path.read_bytes() == before and snapshot(archive.path) == rows


def test_cli_passing_limits_exit_0(capsys, archive):
    code, out, _ = cli(capsys, "--db", str(archive.path), "performance", run_id(1),
                       "--max-start-lateness", "10", "--max-failure-rate", "0.5", "--max-skipped-slot-rate", "0")
    assert code == 0 and json.loads(out)["thresholds"]["status"] == "pass"


def test_cli_failed_limit_prints_the_complete_report_and_exits_1(capsys, archive):
    code, out, err = cli(capsys, "--db", str(archive.path), "performance", run_id(1), "--max-failure-rate", "0.2")

    report = json.loads(out)
    assert code == 1 and err == "performance limits: fail\n"
    assert report["thresholds"]["checks"] == [{"name": "failure_rate", "status": "fail",
                                               "observed": pytest.approx(1 / 3), "limit": 0.2}]
    assert report["start_lateness_s"]["count"] == 3  # the full report is still printed


def test_cli_insufficient_data_exits_1(capsys, tmp_path):
    # A valid archived run with zero attempts (as when a run starts too late to reach slot 0).
    result = variant(tmp_path, 2)
    result.update(attempts=[], counts={"attempted": 0, "succeeded": 0, "failed": 0, "skipped_slots": 0})
    result["analysis"] = {"status": "no_data", "count": 0, "mean_a": None, "median_a": None,
                          "stddev_population_a": None, "min_a": None, "max_a": None}
    archive = archive_with(tmp_path, result)

    code, out, err = cli(capsys, "--db", str(archive.path), "performance", run_id(2), "--max-start-lateness", "1")

    assert code == 1 and err == "performance limits: insufficient_data\n"
    assert json.loads(out)["thresholds"]["status"] == "insufficient_data"


@pytest.mark.parametrize("argv, message", [
    (["--max-failure-rate", "1.5"], "must be between 0 and 1"),
    (["--max-skipped-slot-rate", "-0.1"], "must be between 0 and 1"),
    (["--max-start-lateness", "-1"], "must be at least 0 seconds"),
    (["--max-start-lateness", "nan"], "must be a finite number"),
    (["--max-start-lateness", "inf"], "must be a finite number"),
    (["--max-failure-rate", "half"], "not a number"),
])
def test_cli_invalid_limits_are_usage_errors(capsys, tmp_path, argv, message):
    missing = tmp_path / "never.sqlite3"
    with pytest.raises(SystemExit) as excinfo:
        archive_cli.main(["--db", str(missing), "performance", run_id(1), *argv])

    assert excinfo.value.code == 2 and message in capsys.readouterr().err
    assert not missing.exists()


def test_cli_invalid_run_id_and_missing_run(capsys, archive):
    with pytest.raises(SystemExit) as excinfo:
        archive_cli.main(["--db", str(archive.path), "performance", "not-a-uuid"])
    assert excinfo.value.code == 2 and "run_id must be a UUID" in capsys.readouterr().err

    code, out, err = cli(capsys, "--db", str(archive.path), "performance", run_id(9))
    assert code == 1 and out == "" and err.startswith(f"error: run {run_id(9)} is not archived")
