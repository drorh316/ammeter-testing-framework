"""SQLite archive integration tests: real database files in tmp_path, separate connections."""
import copy
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest
import yaml

from Ammeters.client import AmmeterConnectionError, AmmeterTimeoutError
from fakes import FakeTime
from src.testing import archive as archive_module
from src.testing.archive import (
    APPLICATION_ID,
    ArchiveFormatError,
    ArchiveIntegrityError,
    ArchiveLockedError,
    ArchiveNotFoundError,
    DuplicateRunError,
    ResultArchive,
    RunNotFoundError,
    UnsupportedSchemaVersionError,
)
from src.testing.test_framework import AmmeterTestFramework

RUN_A = "aaaaaaaa-1111-4111-8111-111111111111"
RUN_B = "bbbbbbbb-2222-4222-8222-222222222222"
TABLES = ("runs", "measurements", "run_statistics")

# Values chosen to expose any loss of precision on the round trip.
PRECISE_VALUES = [0.1 + 0.2, 1e-300, 5e-324, 123456.78901234568, -2.5e-7]


class ScriptedRequest:
    def __init__(self, fake_time, outcomes):
        self._time = fake_time
        self._outcomes = list(outcomes)

    def __call__(self, port, command, *, timeout, host):
        self._time.now += 0.01
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_result(tmp_path, outcomes, run_id=RUN_A):
    """A real run_test() result (archiving disabled) from scripted client outcomes."""
    config = {
        "testing": {"sampling": {"sampling_frequency_hz": 2.0, "measurements_count": len(outcomes),
                                 "request_timeout_seconds": 1.0}},
        "ammeters": {"greenlee": {"port": 5001, "command": "MEASURE_GREENLEE -get_measurement"}},
    }
    config_path = tmp_path / f"config-{run_id}.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    fake_time = FakeTime()
    fw = AmmeterTestFramework(
        config_path, request=ScriptedRequest(fake_time, outcomes), clock=fake_time.clock, sleep=fake_time.sleep,
        now=lambda: datetime(2026, 11, 8, 17, 30, 0, 654321, tzinfo=timezone.utc), new_run_id=lambda: run_id)
    return fw.run_test("greenlee")


def mixed_result(tmp_path, run_id=RUN_A):
    outcomes = PRECISE_VALUES[:2] + [AmmeterTimeoutError(5001, "timed out")] + PRECISE_VALUES[2:]
    return make_result(tmp_path, outcomes, run_id)


def snapshot(path):
    """Every row of every archive table, read through an independent connection."""
    with closing(sqlite3.connect(path)) as conn:
        return {table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr) for table in TABLES}


def raw_execute(path, *statements):
    with closing(sqlite3.connect(path)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


# --- Round trip ---------------------------------------------------------------------

def test_round_trip_preserves_the_complete_result_through_a_new_connection(tmp_path):
    db = tmp_path / "archive.sqlite3"
    result = mixed_result(tmp_path)
    ResultArchive(db).save_run(result)

    loaded = ResultArchive(db).load_run(RUN_A)  # a separate archive object and connection

    assert loaded == result
    assert [a.get("current_a") for a in loaded["attempts"]] == PRECISE_VALUES[:2] + [None] + PRECISE_VALUES[2:]
    assert loaded["attempts"][2]["error"] == {"type": "AmmeterTimeoutError",
                                              "message": "Ammeter on port 5001: timed out"}
    assert loaded["analysis"]["status"] == "ok" and loaded["analysis"]["count"] == 5
    json.dumps(loaded, allow_nan=False)


def test_connections_are_closed_and_no_sidecar_files_remain(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))
    ResultArchive(db).load_run(RUN_A)

    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("archive")) == ["archive.sqlite3"]
    os.remove(db)  # on Windows this fails if any connection were still open


def test_rows_are_stored_relationally(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT attempted, succeeded, failed FROM runs").fetchone() == (6, 5, 1)
        assert conn.execute("SELECT count(*) FROM measurements WHERE current_a IS NULL").fetchone() == (1,)
        assert conn.execute("SELECT status, successful_count FROM run_statistics").fetchone() == ("ok", 5)
        assert conn.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)


def test_all_failed_run_is_archived_with_no_data_statistics(tmp_path):
    db = tmp_path / "archive.sqlite3"
    result = make_result(tmp_path, [AmmeterConnectionError(5001, "refused")] * 3)
    ResultArchive(db).save_run(result)

    loaded = ResultArchive(db).load_run(RUN_A)

    assert loaded == result
    assert loaded["analysis"]["status"] == "no_data"
    assert [a["error"]["type"] for a in loaded["attempts"]] == ["AmmeterConnectionError"] * 3


def test_analysis_failure_is_archived_with_the_raw_measurements(tmp_path):
    db = tmp_path / "archive.sqlite3"
    result = make_result(tmp_path, [1.7e308, 1.7e308])  # median overflows
    assert result["analysis"]["status"] == "error"
    ResultArchive(db).save_run(result)

    assert ResultArchive(db).load_run(RUN_A) == result
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT current_a FROM measurements ORDER BY attempt").fetchall() == [(1.7e308,), (1.7e308,)]
        assert conn.execute("SELECT status, error_type FROM run_statistics").fetchone() == ("error", "AnalysisError")


# --- Duplicates and rollback ------------------------------------------------------------

def test_duplicate_run_id_is_rejected_without_changing_the_original(tmp_path):
    db = tmp_path / "archive.sqlite3"
    original = mixed_result(tmp_path)
    ResultArchive(db).save_run(original)
    before = snapshot(db)

    altered = copy.deepcopy(original)
    altered["elapsed_s"] += 1.0
    altered["attempts"][0]["current_a"] = 99.0
    altered["analysis"] = make_result(tmp_path, [99.0] + PRECISE_VALUES[1:2] + [AmmeterTimeoutError(5001, "timed out")]
                                      + PRECISE_VALUES[2:], run_id=RUN_B)["analysis"]
    with pytest.raises(DuplicateRunError, match=RUN_A):
        ResultArchive(db).save_run(altered)

    assert snapshot(db) == before
    assert ResultArchive(db).load_run(RUN_A) == original


@pytest.mark.parametrize(
    "table, rows_already_inserted",
    [
        # Fails on the third measurement, after the runs row and two measurement rows.
        ("measurements", 2),
        # Fails on the last insert (statistics), after the runs row and all six measurement rows.
        ("run_statistics", 6),
    ],
)
def test_failure_after_inserts_rolls_back_the_whole_run(tmp_path, table, rows_already_inserted):
    db = tmp_path / "archive.sqlite3"
    first = mixed_result(tmp_path, RUN_A)
    ResultArchive(db).save_run(first)
    # A test-only trigger forces a database failure part-way through the next save. It fires
    # only if the new run's runs row and the expected measurement rows are already visible in
    # the transaction, so the failure provably happens after those inserts.
    raw_execute(db, f"""
        CREATE TRIGGER force_failure BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id)
         AND (SELECT count(*) FROM measurements WHERE run_id = NEW.run_id) = {rows_already_inserted}
        BEGIN SELECT RAISE(ABORT, 'forced failure in {table}'); END""")
    before = snapshot(db)

    with pytest.raises(ArchiveIntegrityError, match=f"forced failure in {table}") as excinfo:
        ResultArchive(db).save_run(mixed_result(tmp_path, RUN_B))

    assert not isinstance(excinfo.value, DuplicateRunError)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert snapshot(db) == before  # nothing of run B remains
    assert ResultArchive(db).load_run(RUN_A) == first  # the earlier run is unchanged
    with pytest.raises(RunNotFoundError):
        ResultArchive(db).load_run(RUN_B)


# --- Validation before any database access -----------------------------------------------

@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(run_id="not-a-uuid"), "run_id must be a UUID"),
        (lambda r: r.update(run_id=RUN_A.upper()), "canonical lowercase"),
        (lambda r: r.update(started_at_utc="2026-11-08T17:30:00"), "started_at_utc must use the form"),
        (lambda r: r.update(ended_at_utc="2026-11-08T19:30:00.000000+02:00"), "ended_at_utc must use the form"),
        (lambda r: r.update(elapsed_s=math.nan), "elapsed_s must be finite"),
        (lambda r: r["attempts"][0].update(current_a=math.inf), r"attempts\[0\].current_a must be finite"),
        (lambda r: r["attempts"][1].update(attempt=5), r"attempts\[1\].attempt must be 2"),
        (lambda r: r["attempts"][0].update(error={"type": "X", "message": "m"}), "successful but has error"),
        (lambda r: r["counts"].update(succeeded=4), "counts.succeeded is 4 but the attempt records give 5"),
        (lambda r: r["analysis"].update(count=4), "analysis.count is 4 but 5 attempts succeeded"),
        (lambda r: r["settings"].update(measurements_count=6.0), "measurements_count must be an integer"),
        (lambda r: r.pop("stop_reason"), "result is not a complete run_test"),
    ],
)
def test_invalid_results_are_rejected_before_touching_the_database(tmp_path, mutate, message):
    db = tmp_path / "archive.sqlite3"
    result = mixed_result(tmp_path)
    mutate(result)

    with pytest.raises(ValueError, match=message):
        ResultArchive(db).save_run(result)

    assert not db.exists()


@pytest.mark.parametrize("bad_timeout", [-1, math.nan, math.inf, True, "5"])
def test_lock_timeout_is_validated(tmp_path, bad_timeout):
    with pytest.raises(ValueError, match="lock_timeout_s"):
        ResultArchive(tmp_path / "a.sqlite3", lock_timeout_s=bad_timeout)


# --- Database constraints (checked directly, bypassing Python validation) ----------------

def valid_rows(tmp_path, run_id):
    run_row, measurement_rows, statistics_row = archive_module._rows_from_result(mixed_result(tmp_path, run_id))
    return (dict(zip(archive_module._RUN_COLUMNS, run_row)),
            [dict(zip(archive_module._MEASUREMENT_COLUMNS, row)) for row in measurement_rows],
            dict(zip(archive_module._STATISTICS_COLUMNS, statistics_row)))


def insert(conn, table, values):
    columns = list(values)
    conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                 [values[c] for c in columns])


def test_every_connection_enforces_foreign_keys(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))

    for mode in ("rwc", "ro"):
        conn = archive_module._connect(db, mode, 0)
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        finally:
            conn.close()


@pytest.mark.parametrize(
    "table, change",
    [
        ("runs", {"run_id": None}),
        ("runs", {"attempted": 7}),
        ("runs", {"stop_reason": "gave_up"}),
        ("runs", {"port": 70000}),
        ("runs", {"measurements_count": None, "total_duration_seconds": None}),
        ("runs", {"started_at_utc": "2026-11-08 17:30:00"}),
        ("runs", {"sampling_frequency_hz": "fast"}),
        ("runs", {"elapsed_s": math.inf}),
        ("measurements", {"run_id": "cccccccc-3333-4333-8333-333333333333"}),  # no such run
        ("measurements", {"attempt": 0}),
        ("measurements", {"attempt": 1}),  # already used in this run
        ("measurements", {"slot": 0}),  # already used in this run
        ("measurements", {"current_a": 1.0, "error_type": "X", "error_message": "both"}),
        ("measurements", {"current_a": None, "error_type": None, "error_message": None}),
        ("measurements", {"current_a": math.nan}),  # SQLite stores NaN as NULL
        ("measurements", {"current_a": math.inf}),
        ("measurements", {"timeout_clipped_by_deadline": 2}),
        ("run_statistics", {"run_id": None}),
        ("run_statistics", {"run_id": "cccccccc-3333-4333-8333-333333333333"}),  # no such run
        ("run_statistics", {"mean_a": None}),  # status ok needs all metrics
        ("run_statistics", {"status": "no_data"}),  # no_data needs count 0 and no metrics
        ("run_statistics", {"status": "error", "mean_a": None, "median_a": None, "stddev_population_a": None,
                            "min_a": None, "max_a": None}),  # error needs error details
        ("run_statistics", {"min_a": 10.0, "max_a": 1.0}),
        ("run_statistics", {"stddev_population_a": -1.0}),
        ("run_statistics", {"status": "unknown"}),
    ],
)
def test_database_constraints_reject_invalid_rows(tmp_path, table, change):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path, RUN_A))
    run, measurements, statistics = valid_rows(tmp_path, RUN_B)
    conn = archive_module._connect(db, "rwc", 0)
    try:
        if table == "runs":
            row = dict(run, **change)
        elif table == "measurements":
            insert(conn, "runs", run)
            insert(conn, "measurements", measurements[0])  # attempt 1, slot 0
            row = dict(measurements[1], **change)
        else:
            insert(conn, "runs", run)
            row = dict(statistics, **change)
        with pytest.raises(sqlite3.IntegrityError):
            insert(conn, table, row)
        insert(conn, table, dict(run if table == "runs" else measurements[1] if table == "measurements"
                                 else statistics))  # the unchanged row is accepted
    finally:
        conn.close()


# --- Missing, malformed, foreign and unsupported databases -----------------------------

def test_missing_database_is_not_created_by_a_read(tmp_path):
    db = tmp_path / "missing" / "archive.sqlite3"

    with pytest.raises(ArchiveNotFoundError):
        ResultArchive(db).load_run(RUN_A)

    assert not db.exists() and not db.parent.exists()


def test_unknown_run_id_is_reported(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))

    with pytest.raises(RunNotFoundError, match=RUN_B):
        ResultArchive(db).load_run(RUN_B)


def test_malformed_file_is_rejected_unchanged(tmp_path):
    db = tmp_path / "archive.sqlite3"
    db.write_bytes(b"this is not a SQLite database\n" * 40)
    original = db.read_bytes()

    with pytest.raises(ArchiveFormatError):
        ResultArchive(db).load_run(RUN_A)
    with pytest.raises(ArchiveFormatError):
        ResultArchive(db).save_run(mixed_result(tmp_path))

    assert db.read_bytes() == original


@pytest.mark.parametrize(
    "setup",
    [
        ["CREATE TABLE unrelated (x)", "INSERT INTO unrelated VALUES (1)"],  # someone else's tables
        ["PRAGMA user_version = 7"],  # no tables, but not a new file either
        ["PRAGMA application_id = 12345"],
    ],
)
def test_foreign_nonempty_database_is_rejected_unchanged(tmp_path, setup):
    db = tmp_path / "archive.sqlite3"
    raw_execute(db, *setup)
    original = db.read_bytes()

    with pytest.raises(ArchiveFormatError):
        ResultArchive(db).save_run(mixed_result(tmp_path))
    with pytest.raises(ArchiveFormatError):
        ResultArchive(db).load_run(RUN_A)

    assert db.read_bytes() == original


def test_unsupported_schema_version_is_rejected_unchanged(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path, RUN_A))
    raw_execute(db, "PRAGMA user_version = 2")
    original = db.read_bytes()

    with pytest.raises(UnsupportedSchemaVersionError, match="schema version 2"):
        ResultArchive(db).load_run(RUN_A)
    with pytest.raises(UnsupportedSchemaVersionError):
        ResultArchive(db).save_run(mixed_result(tmp_path, RUN_B))

    assert db.read_bytes() == original


def test_archive_with_a_missing_table_is_rejected(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path, RUN_A))
    raw_execute(db, "DROP TABLE run_statistics")

    with pytest.raises(ArchiveFormatError, match="missing archive table"):
        ResultArchive(db).load_run(RUN_A)


def test_empty_existing_file_is_initialized_on_save_but_not_by_a_read(tmp_path):
    db = tmp_path / "archive.sqlite3"
    db.write_bytes(b"")

    with pytest.raises(ArchiveFormatError):
        ResultArchive(db).load_run(RUN_A)
    assert db.read_bytes() == b""  # the read did not initialize it

    result = mixed_result(tmp_path)
    ResultArchive(db).save_run(result)
    assert ResultArchive(db).load_run(RUN_A) == result


def test_reads_do_not_modify_the_file(tmp_path):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))
    original = db.read_bytes()

    ResultArchive(db).load_run(RUN_A)
    with pytest.raises(RunNotFoundError):
        ResultArchive(db).load_run(RUN_B)

    assert db.read_bytes() == original


# --- Incomplete archived runs are reported, not returned --------------------------------

@pytest.mark.parametrize(
    "statement, problem",
    [
        (f"DELETE FROM run_statistics WHERE run_id = '{RUN_A}'", "run_statistics row is missing"),
        (f"DELETE FROM measurements WHERE run_id = '{RUN_A}' AND attempt = 6", "expected attempts 1..6"),
        (f"UPDATE runs SET attempted = attempted + 1, failed = failed + 1 WHERE run_id = '{RUN_A}'",
         "expected attempts 1..7"),
        (f"UPDATE run_statistics SET successful_count = 4 WHERE run_id = '{RUN_A}'", "its statistics 4"),
    ],
)
def test_load_detects_incomplete_or_inconsistent_runs(tmp_path, statement, problem):
    db = tmp_path / "archive.sqlite3"
    ResultArchive(db).save_run(mixed_result(tmp_path))
    raw_execute(db, statement)

    with pytest.raises(ArchiveIntegrityError, match=problem):
        ResultArchive(db).load_run(RUN_A)


# --- Locking ---------------------------------------------------------------------------

def test_write_lock_contention_fails_fast_then_save_succeeds_after_release(tmp_path):
    db = tmp_path / "archive.sqlite3"
    first = mixed_result(tmp_path, RUN_A)
    second = mixed_result(tmp_path, RUN_B)
    ResultArchive(db).save_run(first)

    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # another connection holds the single write lock

        with pytest.raises(ArchiveLockedError) as excinfo:
            ResultArchive(db, lock_timeout_s=0).save_run(second)
        cause = excinfo.value.__cause__
        assert isinstance(cause, sqlite3.OperationalError)
        assert cause.sqlite_errorname == "SQLITE_BUSY"
        assert getattr(cause, "__notes__", None) is None  # no masking "no transaction is active" rollback

        # Readers are not blocked by the pending writer (rollback-journal mode).
        assert ResultArchive(db, lock_timeout_s=0).load_run(RUN_A) == first
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    ResultArchive(db, lock_timeout_s=0).save_run(second)
    assert ResultArchive(db).load_run(RUN_B) == second
    assert ResultArchive(db).load_run(RUN_A) == first


# --- Paths -------------------------------------------------------------------------------

def test_path_with_spaces_and_uri_special_characters(tmp_path):
    # '#' and '%' are special in URIs; '%20' must stay literal, not become a space.
    folder = tmp_path / "archive dir #1 %20 50%"
    db = folder / "results #2.sqlite3"
    result = mixed_result(tmp_path)

    ResultArchive(db).save_run(result)

    assert db.is_file()
    assert [p.name for p in folder.iterdir()] == ["results #2.sqlite3"]
    assert ResultArchive(db).load_run(RUN_A) == result
