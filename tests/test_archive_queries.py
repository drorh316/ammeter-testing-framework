"""list_runs, compare_runs and export_run against real temporary SQLite archives."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from Ammeters.client import AmmeterConnectionError
from src.testing import archive as archive_module
from src.testing.archive import (
    MAX_LIST_LIMIT,
    ArchiveIntegrityError,
    ArchiveNotFoundError,
    ExportError,
    ResultArchive,
    RunNotFoundError,
    UnsupportedSchemaVersionError,
)
from test_archive import make_result, raw_execute, snapshot

START = datetime(2026, 11, 8, 17, 30, 0, 654321, tzinfo=timezone.utc)


def run_id(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-000000000000"


def variant(tmp_path, number, *, ammeter="greenlee", started=START, outcomes=(1.0, 2.0)):
    """A real run_test() result with its ID, device name and start time replaced."""
    result = make_result(tmp_path, list(outcomes), run_id(number))
    result["ammeter"] = ammeter
    result["started_at_utc"] = result["ended_at_utc"] = archive_module.utc_timestamp(started)
    return result


def archive_with(tmp_path, *results):
    archive = ResultArchive(tmp_path / "archive.sqlite3")
    for result in results:
        archive.save_run(result)
    return archive


def ids(summaries):
    return [summary["run_id"] for summary in summaries]


# --- list_runs -----------------------------------------------------------------------

def test_list_returns_newest_first_with_run_id_as_tie_breaker(tmp_path):
    archive = archive_with(
        tmp_path,
        variant(tmp_path, 1, started=START),
        variant(tmp_path, 2, started=START + timedelta(seconds=5)),
        variant(tmp_path, 3, started=START + timedelta(seconds=5)),  # same start time as run 2
        variant(tmp_path, 4, started=START - timedelta(days=1)),
    )

    assert ids(archive.list_runs()) == [run_id(3), run_id(2), run_id(1), run_id(4)]


def test_list_summaries_are_concise_and_do_not_read_measurements(tmp_path, monkeypatch):
    archive = archive_with(tmp_path, variant(tmp_path, 1, outcomes=(1.0, AmmeterConnectionError(5001, "x"), 3.0)))
    statements = []
    original_connect = archive_module._connect

    def tracing_connect(path, mode, timeout):
        conn = original_connect(path, mode, timeout)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(archive_module, "_connect", tracing_connect)

    [summary] = archive.list_runs()

    assert summary == {
        "run_id": run_id(1), "ammeter": "greenlee",
        "started_at_utc": "2026-11-08T17:30:00.654321+00:00", "ended_at_utc": "2026-11-08T17:30:00.654321+00:00",
        "counts": {"attempted": 3, "succeeded": 2, "failed": 1, "skipped_slots": 0},
        "stop_reason": "count_reached", "analysis_status": "ok", "mean_a": 2.0,
    }
    assert statements and not any("FROM measurements" in s for s in statements)


def test_list_filters_by_exact_case_sensitive_ammeter(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1, ammeter="greenlee"), variant(tmp_path, 2, ammeter="entes"),
                           variant(tmp_path, 3, ammeter="Greenlee"), variant(tmp_path, 4, ammeter="greenlee2"))

    assert ids(archive.list_runs(ammeter="greenlee")) == [run_id(1)]
    assert archive.list_runs(ammeter="green") == []


@pytest.mark.parametrize("name", [
    "o'neil", 'say "hi"', "x' OR '1'='1", "greenlee'; DROP TABLE runs; --", "%", "green%", "_reenlee", "50% load",
])
def test_device_names_with_quotes_sql_and_wildcards_are_matched_literally(tmp_path, name):
    archive = archive_with(tmp_path, variant(tmp_path, 1, ammeter="greenlee"), variant(tmp_path, 2, ammeter=name))
    before = snapshot(archive.path)

    assert ids(archive.list_runs(ammeter=name)) == [run_id(2)]
    assert archive.load_run(run_id(2))["ammeter"] == name
    assert snapshot(archive.path) == before


def test_since_is_inclusive_and_until_is_exclusive(tmp_path):
    times = [START + timedelta(minutes=m) for m in range(4)]  # run n+1 starts at minute n
    archive = archive_with(tmp_path, *(variant(tmp_path, n + 1, started=t) for n, t in enumerate(times)))

    assert ids(archive.list_runs(since=times[1])) == [run_id(4), run_id(3), run_id(2)]
    assert ids(archive.list_runs(until=times[2])) == [run_id(2), run_id(1)]
    assert ids(archive.list_runs(since=times[1], until=times[3])) == [run_id(3), run_id(2)]
    assert ids(archive.list_runs(since=times[1] + timedelta(microseconds=1), until=times[2] + timedelta(microseconds=1))) \
        == [run_id(3)]


def test_time_bounds_in_other_timezones_are_normalized_to_utc(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1, started=START),
                           variant(tmp_path, 2, started=START + timedelta(hours=1)))
    plus_two = timezone(timedelta(hours=2))

    # 19:30:00.654321+02:00 is the start of run 1; text comparison of the raw offsets would be wrong.
    assert ids(archive.list_runs(since=START.astimezone(plus_two))) == [run_id(2), run_id(1)]
    assert ids(archive.list_runs(until=(START + timedelta(hours=1)).astimezone(plus_two))) == [run_id(1)]


def test_limit_defaults_to_20_and_accepts_the_maximum(tmp_path):
    archive = archive_with(tmp_path, *(variant(tmp_path, n, started=START + timedelta(seconds=n)) for n in range(1, 23)))

    assert ids(archive.list_runs()) == [run_id(n) for n in range(22, 2, -1)]
    assert ids(archive.list_runs(limit=1)) == [run_id(22)]
    assert len(archive.list_runs(limit=MAX_LIST_LIMIT)) == 22


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"limit": 0}, "limit must be a whole number from 1 to 1000"),
        ({"limit": 1001}, "limit must be"),
        ({"limit": True}, "limit must be"),
        ({"limit": 5.0}, "limit must be"),
        ({"limit": "5"}, "limit must be"),
        ({"since": datetime(2026, 11, 8)}, "since must be timezone-aware"),
        ({"until": "2026-11-08T00:00:00+00:00"}, "until must be a datetime"),
        ({"since": START, "until": START}, "must be earlier than until"),
        ({"since": START + timedelta(seconds=1), "until": START}, "must be earlier than until"),
        ({"ammeter": ""}, "ammeter must be a non-empty string"),
        ({"ammeter": 5}, "ammeter must be a non-empty string"),
    ],
)
def test_invalid_list_arguments_are_rejected_before_opening_the_database(tmp_path, kwargs, message):
    archive = ResultArchive(tmp_path / "missing.sqlite3")  # would raise ArchiveNotFoundError if opened

    with pytest.raises(ValueError, match=message):
        archive.list_runs(**kwargs)


def test_list_reports_a_run_without_statistics_instead_of_omitting_it(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1), variant(tmp_path, 2, ammeter="entes"))
    raw_execute(archive.path, f"DELETE FROM run_statistics WHERE run_id = '{run_id(2)}'")

    with pytest.raises(ArchiveIntegrityError, match=f"no run_statistics row: {run_id(2)}$"):
        archive.list_runs()
    with pytest.raises(ArchiveIntegrityError):
        archive.list_runs(ammeter="entes")
    # The check covers the runs being listed: a filter that excludes the broken run still works.
    assert ids(archive.list_runs(ammeter="greenlee")) == [run_id(1)]


def test_list_on_a_missing_or_unsupported_database(tmp_path):
    missing = tmp_path / "missing" / "archive.sqlite3"
    with pytest.raises(ArchiveNotFoundError):
        ResultArchive(missing).list_runs()
    assert not missing.parent.exists()

    archive = archive_with(tmp_path, variant(tmp_path, 1))
    raw_execute(archive.path, "PRAGMA user_version = 2")
    with pytest.raises(UnsupportedSchemaVersionError):
        archive.list_runs()


# --- Run ID validation ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["not-a-uuid", run_id(0xabc).upper(), run_id(1).replace("-", ""), "", None, 7])
@pytest.mark.parametrize("operation", [
    lambda a, bad: a.load_run(bad),
    lambda a, bad: a.compare_runs(bad, run_id(1)),
    lambda a, bad: a.compare_runs(run_id(1), bad),
    lambda a, bad: a.export_run(bad, a.path.parent / "out.json"),
])
def test_malformed_run_ids_are_rejected_before_opening_the_database(tmp_path, operation, bad):
    archive = ResultArchive(tmp_path / "missing.sqlite3")  # would raise ArchiveNotFoundError if opened

    with pytest.raises(ValueError, match="run_id must be"):
        operation(archive, bad)
    assert not (tmp_path / "out.json").exists()


# --- compare_runs --------------------------------------------------------------------

def test_compare_runs_with_matching_settings(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1, outcomes=(1.0, 3.0)),
                           variant(tmp_path, 2, outcomes=(2.0, 6.0)))

    comparison = archive.compare_runs(run_id(1), run_id(2))

    assert comparison["context_differences"] == []
    assert comparison["run_a"]["run_id"] == run_id(1) and comparison["run_b"]["run_id"] == run_id(2)
    assert comparison["run_a"]["settings"] == comparison["run_b"]["settings"]
    assert comparison["run_b"]["counts"] == {"attempted": 2, "succeeded": 2, "failed": 0, "skipped_slots": 0}
    assert comparison["metrics"] == {
        "mean_a": {"a": 2.0, "b": 4.0, "difference_b_minus_a": 2.0},
        "median_a": {"a": 2.0, "b": 4.0, "difference_b_minus_a": 2.0},
        "stddev_population_a": {"a": 1.0, "b": 2.0, "difference_b_minus_a": 1.0},
        "min_a": {"a": 1.0, "b": 2.0, "difference_b_minus_a": 1.0},
        "max_a": {"a": 3.0, "b": 6.0, "difference_b_minus_a": 3.0},
    }
    # Only data: no attempt lists, and no ranking, verdict or pass/fail fields.
    assert set(comparison) == {"run_a", "run_b", "context_differences", "metrics"}
    assert set(comparison["run_a"]) == {"run_id", "ammeter", "host", "port", "command", "started_at_utc",
                                        "settings", "counts", "stop_reason", "analysis_status"}
    assert '"attempts"' not in json.dumps(comparison, allow_nan=False)


def test_compare_highlights_different_devices_and_settings_but_still_compares(tmp_path):
    a = variant(tmp_path, 1, outcomes=(1.0, 2.0))
    b = variant(tmp_path, 2, ammeter="entes", outcomes=(1.0, 2.0, 3.0))
    b.update(port=5002, command="MEASURE_ENTES -get_data")
    archive = archive_with(tmp_path, a, b)

    comparison = archive.compare_runs(run_id(1), run_id(2))

    assert comparison["context_differences"] == ["ammeter", "port", "command", "settings.measurements_count"]
    assert comparison["metrics"]["mean_a"] == {"a": 1.5, "b": 2.0, "difference_b_minus_a": 0.5}


def test_compare_with_no_data_and_failed_analysis(tmp_path):
    archive = archive_with(
        tmp_path,
        variant(tmp_path, 1, outcomes=(1.0, 2.0)),
        variant(tmp_path, 2, outcomes=[AmmeterConnectionError(5001, "refused")] * 2),  # no_data
        variant(tmp_path, 3, outcomes=(1.7e308, 1.7e308)),  # analysis error (the median overflows)
    )

    no_data = archive.compare_runs(run_id(1), run_id(2))
    failed = archive.compare_runs(run_id(3), run_id(2))

    assert no_data["run_b"]["analysis_status"] == "no_data"
    assert no_data["metrics"]["mean_a"] == {
        "a": 1.5, "b": None, "difference_unavailable": "no value for run b (analysis status 'no_data')"}
    assert failed["run_a"]["analysis_status"] == "error"
    assert failed["metrics"]["max_a"] == {
        "a": None, "b": None,
        "difference_unavailable": "no value for run a (analysis status 'error') and run b (analysis status 'no_data')"}
    for comparison in (no_data, failed):
        assert all("difference_b_minus_a" not in entry for entry in comparison["metrics"].values())
        json.dumps(comparison, allow_nan=False)


def test_compare_reports_an_overflowing_difference_as_unavailable(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1, outcomes=(-1.7e308,)),
                           variant(tmp_path, 2, outcomes=(1.7e308,)))

    metrics = archive.compare_runs(run_id(1), run_id(2))["metrics"]

    assert metrics["mean_a"] == {"a": -1.7e308, "b": 1.7e308,
                                 "difference_unavailable": "b - a is too large to represent as a finite float"}
    assert metrics["stddev_population_a"] == {"a": 0.0, "b": 0.0, "difference_b_minus_a": 0.0}
    json.dumps(metrics, allow_nan=False)


def test_compare_reports_a_missing_run(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1))

    with pytest.raises(RunNotFoundError, match=run_id(9)):
        archive.compare_runs(run_id(1), run_id(9))


# --- export_run ----------------------------------------------------------------------

def test_export_round_trip_is_strict_utf8_json(tmp_path):
    result = variant(tmp_path, 1, ammeter="Ω-meter", outcomes=(0.1 + 0.2, AmmeterConnectionError(5001, "refused")))
    archive = archive_with(tmp_path, result)
    output = tmp_path / "exports" / "run.json"
    output.parent.mkdir()

    assert archive.export_run(run_id(1), output) == output

    raw = output.read_bytes()
    assert json.loads(raw.decode("utf-8")) == archive.load_run(run_id(1)) == result
    assert "Ω-meter".encode("utf-8") in raw and raw.endswith(b"\n")
    json.loads(raw, parse_constant=lambda name: pytest.fail(f"non-standard JSON constant {name}"))


def test_export_never_overwrites_an_existing_file(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1))
    output = tmp_path / "run.json"
    output.write_bytes(b"keep me")

    with pytest.raises(ExportError, match="already exists"):
        archive.export_run(run_id(1), output)

    assert output.read_bytes() == b"keep me"


def test_export_requires_an_existing_directory_and_an_archived_run(tmp_path):
    archive = archive_with(tmp_path, variant(tmp_path, 1))

    with pytest.raises(ExportError, match="does not exist"):
        archive.export_run(run_id(1), tmp_path / "no such dir" / "run.json")
    assert not (tmp_path / "no such dir").exists()

    with pytest.raises(RunNotFoundError):
        archive.export_run(run_id(9), tmp_path / "run.json")
    assert not (tmp_path / "run.json").exists()


class FailingStream:
    """Writes part of the data to the real file, then fails like a full disk."""

    def __init__(self, path):
        self._file = open(path, "xb")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._file.close()

    def write(self, data):
        self._file.write(data[:10])
        self._file.flush()
        raise OSError(28, "No space left on device")


def test_failed_write_removes_the_partial_new_file(tmp_path, monkeypatch):
    archive = archive_with(tmp_path, variant(tmp_path, 1))
    output = tmp_path / "run.json"
    monkeypatch.setattr(archive_module, "_create_exclusive", FailingStream)

    with pytest.raises(ExportError, match="partial file was removed") as excinfo:
        archive.export_run(run_id(1), output)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert not output.exists()


def test_failed_cleanup_is_reported_and_the_partial_file_is_named(tmp_path, monkeypatch):
    archive = archive_with(tmp_path, variant(tmp_path, 1))
    output = tmp_path / "run.json"
    monkeypatch.setattr(archive_module, "_create_exclusive", FailingStream)
    original_unlink = Path.unlink
    unlink_fails = True

    def failing_unlink(self, *args, **kwargs):
        if unlink_fails and self == output:
            raise PermissionError(13, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(ExportError) as excinfo:
        archive.export_run(run_id(1), output)

    message = str(excinfo.value)
    assert "was removed" not in message
    assert f"the partial file {output} could NOT be removed" in message
    assert "Access is denied" in message and "No space left on device" in message
    assert isinstance(excinfo.value.__cause__, OSError) and excinfo.value.__cause__.errno == 28
    unlink_fails = False
    assert output.exists() and len(output.read_bytes()) == 10  # the partial file is still there


def test_serialization_happens_before_the_file_is_created(tmp_path, monkeypatch):
    archive = archive_with(tmp_path, variant(tmp_path, 1))
    output = tmp_path / "run.json"
    loaded = archive.load_run(run_id(1))
    loaded["elapsed_s"] = math.nan  # cannot be written as strict JSON
    monkeypatch.setattr(ResultArchive, "load_run", lambda self, rid: loaded)

    with pytest.raises(ValueError, match="not JSON compliant"):
        archive.export_run(run_id(1), output)

    assert not output.exists()
