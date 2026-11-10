"""SQLite archive of run_test() results (Python's built-in sqlite3 module).

Tables
------
- ``runs``: one row per run: id, UTC start/end, device, effective settings, counts,
  elapsed time and stop reason.
- ``measurements``: one row per attempt; either ``current_a`` or the error details.
- ``run_statistics``: one row per run: the analysis status, successful count, the
  five metrics, or the analysis error.

Behaviour
---------
- A run and all its rows are saved in one transaction (BEGIN IMMEDIATE ... COMMIT). Any
  failure rolls the whole save back, so a run is never partly archived. An existing run
  ID is rejected; existing data is never replaced.
- Every connection enables foreign keys and is closed explicitly. All SQL values are
  passed as parameters.
- Only a genuinely new or empty file is initialised. Any other file must carry this
  archive's application id and schema version and have every required table and
  column (checked by name before any run row is read or inserted), otherwise it is
  rejected unchanged. There is no migration code.
- Integers must fit SQLite's signed 64-bit INTEGER; larger values are rejected by
  validation before the database is opened.
- Reads open the file read-only (mode=ro): they never create a missing file or change
  the schema. Loading checks that the measurement and statistics rows match the run's
  counts, so an incomplete run is reported instead of returned.
- SQLite allows one writer at a time. A save waits at most ``lock_timeout_s`` for the
  write lock and then fails with ArchiveLockedError.
- A run is held in memory until it finishes and is saved only then: if the sampling
  process crashes during a run, that run's measurements are lost. Runs already saved are
  protected by SQLite's own transactions: if a save is interrupted, SQLite's rollback
  journal restores the database to its state before that save the next time a writing
  connection opens it. Until then a read-only load cannot complete that recovery and
  fails with ArchiveError (cause SQLITE_READONLY_ROLLBACK).

Queries
-------
- ``list_runs`` returns short summaries (no measurement rows) filtered by device and
  start time, ``load_run`` one complete run, ``compare_runs`` two runs side by side, and
  ``export_run`` writes one run to a new JSON file. All are read-only on the database
  and validate run IDs as canonical UUIDs before opening it.
"""
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple, Union

SCHEMA_VERSION = 1
APPLICATION_ID = 0x414D4D52  # "AMMR": identifies this archive format in the SQLite header
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 1000
# SQLite stores INTEGER values as signed 64-bit integers; sqlite3 raises OverflowError
# (not a sqlite3.Error) for a larger Python int, so integers are range-checked first.
SQLITE_INTEGER_MIN = -2**63
SQLITE_INTEGER_MAX = 2**63 - 1

# Finite-number checks compare against 9e999, which SQLite reads as infinity: "x < 9e999"
# rejects +inf and "abs(x) < 9e999" rejects both. (SQLite 3.40 parses the literal
# 1.7976931348623157e308 as infinity too, so it cannot be used as the bound.) SQLite stores
# NaN as NULL, so NaN is rejected by NOT NULL or by the success/error checks. A CHECK whose
# expression is NULL passes, so nullable columns test "IS NULL OR (...)" explicitly.
_INF = "9e999"
_UTC = "'____-__-__T__:__:__.______+00:00'"

_SCHEMA = (
    f"""
    CREATE TABLE runs (
        run_id TEXT NOT NULL PRIMARY KEY CHECK (typeof(run_id) = 'text' AND length(run_id) = 36),
        started_at_utc TEXT NOT NULL CHECK (typeof(started_at_utc) = 'text' AND started_at_utc LIKE {_UTC}),
        ended_at_utc TEXT NOT NULL CHECK (typeof(ended_at_utc) = 'text' AND ended_at_utc LIKE {_UTC}),
        ammeter TEXT NOT NULL CHECK (typeof(ammeter) = 'text' AND length(ammeter) > 0),
        host TEXT NOT NULL CHECK (typeof(host) = 'text' AND length(host) > 0),
        port INTEGER NOT NULL CHECK (typeof(port) = 'integer' AND port BETWEEN 1 AND 65535),
        command TEXT NOT NULL CHECK (typeof(command) = 'text' AND length(command) > 0),
        sampling_frequency_hz REAL NOT NULL
            CHECK (typeof(sampling_frequency_hz) = 'real' AND sampling_frequency_hz > 0
                   AND sampling_frequency_hz < {_INF}),
        measurements_count INTEGER
            CHECK (measurements_count IS NULL
                   OR (typeof(measurements_count) = 'integer' AND measurements_count >= 1)),
        total_duration_seconds REAL
            CHECK (total_duration_seconds IS NULL
                   OR (typeof(total_duration_seconds) = 'real' AND total_duration_seconds > 0
                       AND total_duration_seconds < {_INF})),
        request_timeout_seconds REAL NOT NULL
            CHECK (typeof(request_timeout_seconds) = 'real' AND request_timeout_seconds > 0
                   AND request_timeout_seconds < {_INF}),
        attempted INTEGER NOT NULL CHECK (typeof(attempted) = 'integer' AND attempted >= 0),
        succeeded INTEGER NOT NULL CHECK (typeof(succeeded) = 'integer' AND succeeded >= 0),
        failed INTEGER NOT NULL CHECK (typeof(failed) = 'integer' AND failed >= 0),
        skipped_slots INTEGER NOT NULL CHECK (typeof(skipped_slots) = 'integer' AND skipped_slots >= 0),
        elapsed_s REAL NOT NULL CHECK (typeof(elapsed_s) = 'real' AND elapsed_s >= 0 AND elapsed_s < {_INF}),
        stop_reason TEXT NOT NULL CHECK (stop_reason IN ('count_reached', 'duration_reached')),
        CHECK (measurements_count IS NOT NULL OR total_duration_seconds IS NOT NULL),
        CHECK (attempted = succeeded + failed)
    )
    """,
    f"""
    CREATE TABLE measurements (
        run_id TEXT NOT NULL REFERENCES runs (run_id),
        attempt INTEGER NOT NULL CHECK (typeof(attempt) = 'integer' AND attempt >= 1),
        slot INTEGER NOT NULL CHECK (typeof(slot) = 'integer' AND slot >= 0),
        scheduled_offset_s REAL NOT NULL
            CHECK (typeof(scheduled_offset_s) = 'real' AND scheduled_offset_s >= 0 AND scheduled_offset_s < {_INF}),
        start_offset_s REAL NOT NULL
            CHECK (typeof(start_offset_s) = 'real' AND start_offset_s >= 0 AND start_offset_s < {_INF}),
        elapsed_s REAL NOT NULL CHECK (typeof(elapsed_s) = 'real' AND elapsed_s >= 0 AND elapsed_s < {_INF}),
        effective_timeout_s REAL NOT NULL
            CHECK (typeof(effective_timeout_s) = 'real' AND effective_timeout_s > 0 AND effective_timeout_s < {_INF}),
        timeout_clipped_by_deadline INTEGER NOT NULL CHECK (timeout_clipped_by_deadline IN (0, 1)),
        current_a REAL CHECK (current_a IS NULL OR (typeof(current_a) = 'real' AND abs(current_a) < {_INF})),
        error_type TEXT CHECK (error_type IS NULL OR (typeof(error_type) = 'text' AND length(error_type) > 0)),
        error_message TEXT CHECK (error_message IS NULL OR typeof(error_message) = 'text'),
        PRIMARY KEY (run_id, attempt),
        UNIQUE (run_id, slot),
        CHECK ((current_a IS NOT NULL AND error_type IS NULL AND error_message IS NULL)
               OR (current_a IS NULL AND error_type IS NOT NULL AND error_message IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE run_statistics (
        run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (run_id),
        status TEXT NOT NULL CHECK (status IN ('ok', 'no_data', 'error')),
        successful_count INTEGER NOT NULL CHECK (typeof(successful_count) = 'integer' AND successful_count >= 0),
        mean_a REAL CHECK (mean_a IS NULL OR (typeof(mean_a) = 'real' AND abs(mean_a) < {_INF})),
        median_a REAL CHECK (median_a IS NULL OR (typeof(median_a) = 'real' AND abs(median_a) < {_INF})),
        stddev_population_a REAL
            CHECK (stddev_population_a IS NULL
                   OR (typeof(stddev_population_a) = 'real' AND stddev_population_a >= 0
                       AND stddev_population_a < {_INF})),
        min_a REAL CHECK (min_a IS NULL OR (typeof(min_a) = 'real' AND abs(min_a) < {_INF})),
        max_a REAL CHECK (max_a IS NULL OR (typeof(max_a) = 'real' AND abs(max_a) < {_INF})),
        error_type TEXT CHECK (error_type IS NULL OR (typeof(error_type) = 'text' AND length(error_type) > 0)),
        error_message TEXT CHECK (error_message IS NULL OR typeof(error_message) = 'text'),
        CHECK (
            (status = 'ok' AND successful_count >= 1
             AND mean_a IS NOT NULL AND median_a IS NOT NULL AND stddev_population_a IS NOT NULL
             AND min_a IS NOT NULL AND max_a IS NOT NULL AND min_a <= max_a
             AND error_type IS NULL AND error_message IS NULL)
            OR (status = 'no_data' AND successful_count = 0
                AND mean_a IS NULL AND median_a IS NULL AND stddev_population_a IS NULL
                AND min_a IS NULL AND max_a IS NULL AND error_type IS NULL AND error_message IS NULL)
            OR (status = 'error' AND successful_count >= 1
                AND mean_a IS NULL AND median_a IS NULL AND stddev_population_a IS NULL
                AND min_a IS NULL AND max_a IS NULL AND error_type IS NOT NULL AND error_message IS NOT NULL)
        )
    )
    """,
    # Supports listing runs by device and start time (the planned list operation).
    "CREATE INDEX runs_by_ammeter_started ON runs (ammeter, started_at_utc)",
    f"PRAGMA application_id = {APPLICATION_ID}",
    f"PRAGMA user_version = {SCHEMA_VERSION}",
)

_RUN_COLUMNS = (
    "run_id", "started_at_utc", "ended_at_utc", "ammeter", "host", "port", "command",
    "sampling_frequency_hz", "measurements_count", "total_duration_seconds", "request_timeout_seconds",
    "attempted", "succeeded", "failed", "skipped_slots", "elapsed_s", "stop_reason",
)
_MEASUREMENT_COLUMNS = (
    "run_id", "attempt", "slot", "scheduled_offset_s", "start_offset_s", "elapsed_s",
    "effective_timeout_s", "timeout_clipped_by_deadline", "current_a", "error_type", "error_message",
)
_STATISTICS_COLUMNS = (
    "run_id", "status", "successful_count", "mean_a", "median_a", "stddev_population_a",
    "min_a", "max_a", "error_type", "error_message",
)
_METRICS = ("mean_a", "median_a", "stddev_population_a", "min_a", "max_a")

# Columns each table must have (checked by name only; types and constraints are not compared).
REQUIRED_COLUMNS = {
    "runs": _RUN_COLUMNS,
    "measurements": _MEASUREMENT_COLUMNS,
    "run_statistics": _STATISTICS_COLUMNS,
}
REQUIRED_TABLES = frozenset(REQUIRED_COLUMNS)


def _insert_sql(table: str, columns: Tuple[str, ...]) -> str:
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"


_INSERT_RUN = _insert_sql("runs", _RUN_COLUMNS)
_INSERT_MEASUREMENT = _insert_sql("measurements", _MEASUREMENT_COLUMNS)
_INSERT_STATISTICS = _insert_sql("run_statistics", _STATISTICS_COLUMNS)


# --- Errors ----------------------------------------------------------------------

class ArchiveError(Exception):
    """A result archive operation failed. The underlying error, if any, is ``__cause__``."""


class ArchiveNotFoundError(ArchiveError):
    """The archive file does not exist (read operations never create it)."""


class ArchiveFormatError(ArchiveError):
    """The file is not a SQLite database, or not an archive created by this program."""


class UnsupportedSchemaVersionError(ArchiveError):
    """The archive was created with a schema version this program does not support."""


class ArchiveLockedError(ArchiveError):
    """Another connection held the write lock for longer than the lock timeout."""


class DuplicateRunError(ArchiveError):
    """A run with this ID is already archived; the existing run was not changed."""


class RunNotFoundError(ArchiveError):
    """No run with this ID is archived."""


class ArchiveIntegrityError(ArchiveError):
    """The database rejected the data, or an archived run's rows are inconsistent."""


class ExportError(ArchiveError):
    """A run could not be exported (destination exists, directory missing, or write failed)."""


class ResultArchiveError(ArchiveError):
    """A run completed but could not be archived. ``result`` holds the complete in-memory result."""

    def __init__(self, message: str, result: Dict[str, Any]):
        super().__init__(message)
        self.result = result


# --- Helpers -----------------------------------------------------------------------

def utc_timestamp(moment: datetime) -> str:
    """Format a timezone-aware datetime as the archive's UTC text form (microsecond precision)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"timestamp must be timezone-aware, got {moment!r}")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sqlite_uri(path: Path, mode: str) -> str:
    # as_uri() percent-encodes spaces and URI-special characters such as '#', '?' and '%'.
    return f"{path.resolve().as_uri()}?mode={mode}"


def _translate(exc: sqlite3.Error, path: Path) -> ArchiveError:
    """Map a sqlite3 error to the matching ArchiveError (the caller chains ``exc`` as the cause)."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return ArchiveLockedError(f"archive {path} is locked by another connection: {exc}")
    if name.startswith(("SQLITE_NOTADB", "SQLITE_CORRUPT")):
        return ArchiveFormatError(f"{path} is not a valid SQLite database: {exc}")
    if isinstance(exc, sqlite3.IntegrityError):
        return ArchiveIntegrityError(f"archive {path} rejected the data: {exc}")
    return ArchiveError(f"archive {path} could not be accessed: {exc}")


def _connect(path: Path, mode: str, timeout: float) -> sqlite3.Connection:
    """Open ``path`` (mode "ro" or "rwc") with foreign keys enforced. The caller must close it."""
    try:
        conn = sqlite3.connect(_sqlite_uri(path, mode), uri=True, timeout=timeout, isolation_level=None)
    except sqlite3.Error as exc:
        raise _translate(exc, path) from exc
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ArchiveError("this SQLite build does not enforce foreign keys")
    except BaseException as exc:
        conn.close()
        if isinstance(exc, sqlite3.Error):
            raise _translate(exc, path) from exc
        raise
    return conn


def _rollback_if_active(conn: sqlite3.Connection, original: BaseException) -> None:
    """Roll back only an active transaction; a failed rollback is noted on the original error."""
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rollback_error:
            original.add_note(f"rollback also failed: {rollback_error!r}")


def _is_uninitialized(conn: sqlite3.Connection) -> bool:
    """True only for a new or empty database: no schema objects, no application id or version."""
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    objects = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    return application_id == 0 and version == 0 and objects == 0


def _check_identity(conn: sqlite3.Connection, path: Path) -> None:
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if application_id != APPLICATION_ID:
        raise ArchiveFormatError(f"{path} is not an ammeter results archive (application_id {application_id})")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"{path} uses archive schema version {version}; this program supports only version {SCHEMA_VERSION}")
    tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = REQUIRED_TABLES - tables
    if missing:
        raise ArchiveFormatError(f"{path} is missing archive table(s): {', '.join(sorted(missing))}")
    missing_columns = []
    for table, columns in REQUIRED_COLUMNS.items():
        present = {name for (name,) in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}
        missing_columns += [f"{table}.{column}" for column in columns if column not in present]
    if missing_columns:
        raise ArchiveFormatError(f"{path} is missing archive column(s): {', '.join(missing_columns)}")


# --- Validation and conversion -------------------------------------------------------

def _number(name: str, value, *, integer: bool = False, nullable: bool = False):
    if value is None:
        if nullable:
            return None
        raise ValueError(f"{name} is required")
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise ValueError(f"{name} must be {'an integer' if integer else 'a number'}, got {value!r}")
    if integer and not SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
        raise ValueError(f"{name} must be between {SQLITE_INTEGER_MIN} and {SQLITE_INTEGER_MAX} "
                         f"(SQLite's signed 64-bit INTEGER range), got {value!r}")
    if not integer:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def _text(name: str, value, *, nullable: bool = False) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def validate_run_id(value) -> str:
    """Return ``value`` if it is a run ID in canonical lowercase UUID form, else raise ValueError."""
    try:
        canonical = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"run_id must be a UUID string, got {value!r}") from exc
    if canonical != value:
        raise ValueError(f"run_id must be in canonical lowercase UUID form, got {value!r}")
    return value


def _utc_text(name: str, value) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO 8601 UTC timestamp, got {value!r}") from exc
    if parsed.tzinfo is None or utc_timestamp(parsed) != value:
        raise ValueError(f"{name} must use the form YYYY-MM-DDTHH:MM:SS.ffffff+00:00, got {value!r}")
    return value


def _rows_from_result(result: Dict[str, Any]):
    """Validate a run_test() result and convert it to (run row, measurement rows, statistics row)."""
    try:
        run_id = validate_run_id(result["run_id"])
        settings, counts = result["settings"], result["counts"]
        attempts, analysis = result["attempts"], result["analysis"]

        measurement_rows = []
        succeeded = 0
        for number, attempt in enumerate(attempts, start=1):
            prefix = f"attempts[{number - 1}]"
            if _number(f"{prefix}.attempt", attempt["attempt"], integer=True) != number:
                raise ValueError(f"{prefix}.attempt must be {number}, got {attempt['attempt']!r}")
            if attempt["ok"] is True:
                succeeded += 1
                current, error_type, error_message = _number(f"{prefix}.current_a", attempt["current_a"]), None, None
                if "error" in attempt:
                    raise ValueError(f"{prefix} is successful but has error details")
            elif attempt["ok"] is False:
                if "current_a" in attempt:
                    raise ValueError(f"{prefix} failed but has a current value")
                current = None
                error_type = _text(f"{prefix}.error.type", attempt["error"]["type"])
                error_message = attempt["error"]["message"]
                if not isinstance(error_message, str):
                    raise ValueError(f"{prefix}.error.message must be a string")
            else:
                raise ValueError(f"{prefix}.ok must be true or false, got {attempt['ok']!r}")
            clipped = attempt["timeout_clipped_by_deadline"]
            if not isinstance(clipped, bool):
                raise ValueError(f"{prefix}.timeout_clipped_by_deadline must be true or false")
            measurement_rows.append((
                run_id, number, _number(f"{prefix}.slot", attempt["slot"], integer=True),
                _number(f"{prefix}.scheduled_offset_s", attempt["scheduled_offset_s"]),
                _number(f"{prefix}.start_offset_s", attempt["start_offset_s"]),
                _number(f"{prefix}.elapsed_s", attempt["elapsed_s"]),
                _number(f"{prefix}.effective_timeout_s", attempt["effective_timeout_s"]),
                int(clipped), current, error_type, error_message,
            ))

        attempted = _number("counts.attempted", counts["attempted"], integer=True)
        expected = {"attempted": len(measurement_rows), "succeeded": succeeded,
                    "failed": len(measurement_rows) - succeeded}
        for key, value in expected.items():
            if counts[key] != value or isinstance(counts[key], bool):
                raise ValueError(f"counts.{key} is {counts[key]!r} but the attempt records give {value}")
        if analysis["count"] != succeeded or isinstance(analysis["count"], bool):
            raise ValueError(f"analysis.count is {analysis['count']!r} but {succeeded} attempts succeeded")

        run_row = (
            run_id,
            _utc_text("started_at_utc", result["started_at_utc"]),
            _utc_text("ended_at_utc", result["ended_at_utc"]),
            _text("ammeter", result["ammeter"]), _text("host", result["host"]),
            _number("port", result["port"], integer=True), _text("command", result["command"]),
            _number("settings.sampling_frequency_hz", settings["sampling_frequency_hz"]),
            _number("settings.measurements_count", settings["measurements_count"], integer=True, nullable=True),
            _number("settings.total_duration_seconds", settings["total_duration_seconds"], nullable=True),
            _number("settings.request_timeout_seconds", settings["request_timeout_seconds"]),
            attempted, succeeded, attempted - succeeded,
            _number("counts.skipped_slots", counts["skipped_slots"], integer=True),
            _number("elapsed_s", result["elapsed_s"]),
            _text("stop_reason", result["stop_reason"]),
        )

        error = analysis.get("error")
        statistics_row = (
            run_id, _text("analysis.status", analysis["status"]), succeeded,
            *(_number(f"analysis.{key}", analysis[key], nullable=True) for key in _METRICS),
            None if error is None else _text("analysis.error.type", error["type"]),
            None if error is None else error["message"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"result is not a complete run_test() result: missing or invalid {exc!r}") from exc
    return run_row, measurement_rows, statistics_row


def _result_from_rows(run: sqlite3.Row, measurements: List[sqlite3.Row], stats: Optional[sqlite3.Row],
                      path: Path) -> Dict[str, Any]:
    """Rebuild the run_test() result structure, checking that the rows are complete."""
    run_id = run["run_id"]

    def inconsistent(problem: str) -> ArchiveIntegrityError:
        return ArchiveIntegrityError(f"archived run {run_id} in {path} is incomplete or inconsistent: {problem}")

    if stats is None:
        raise inconsistent("its run_statistics row is missing")
    numbers = [row["attempt"] for row in measurements]
    if numbers != list(range(1, len(measurements) + 1)) or len(measurements) != run["attempted"]:
        raise inconsistent(f"expected attempts 1..{run['attempted']}, found {numbers}")
    succeeded = sum(1 for row in measurements if row["current_a"] is not None)
    if succeeded != run["succeeded"] or succeeded != stats["successful_count"]:
        raise inconsistent(f"{succeeded} successful measurement rows, but the run records {run['succeeded']} "
                           f"and its statistics {stats['successful_count']}")

    attempts = []
    for row in measurements:
        record = {
            "attempt": row["attempt"],
            "slot": row["slot"],
            "scheduled_offset_s": row["scheduled_offset_s"],
            "start_offset_s": row["start_offset_s"],
            "lateness_s": row["start_offset_s"] - row["scheduled_offset_s"],
            "elapsed_s": row["elapsed_s"],
            "effective_timeout_s": row["effective_timeout_s"],
            "timeout_clipped_by_deadline": bool(row["timeout_clipped_by_deadline"]),
            "ok": row["current_a"] is not None,
        }
        if record["ok"]:
            record["current_a"] = row["current_a"]
        else:
            record["error"] = {"type": row["error_type"], "message": row["error_message"]}
        attempts.append(record)

    analysis = {"status": stats["status"], "count": stats["successful_count"],
                **{key: stats[key] for key in _METRICS}}
    if stats["status"] == "error":
        analysis["error"] = {"type": stats["error_type"], "message": stats["error_message"]}

    return {
        "run_id": run_id,
        "ammeter": run["ammeter"],
        "host": run["host"],
        "port": run["port"],
        "command": run["command"],
        "started_at_utc": run["started_at_utc"],
        "ended_at_utc": run["ended_at_utc"],
        "settings": {
            "sampling_frequency_hz": run["sampling_frequency_hz"],
            "measurements_count": run["measurements_count"],
            "total_duration_seconds": run["total_duration_seconds"],
            "request_timeout_seconds": run["request_timeout_seconds"],
        },
        "counts": {"attempted": run["attempted"], "succeeded": run["succeeded"],
                   "failed": run["failed"], "skipped_slots": run["skipped_slots"]},
        "elapsed_s": run["elapsed_s"],
        "stop_reason": run["stop_reason"],
        "attempts": attempts,
        "analysis": analysis,
    }


# --- Archive -----------------------------------------------------------------------------

class ResultArchive:
    """Saves and loads run_test() results in one SQLite file. Each call opens and closes its own connection."""

    def __init__(self, path: Union[str, Path], *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_SECONDS):
        if isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, (int, float)) \
                or not math.isfinite(lock_timeout_s) or lock_timeout_s < 0:
            raise ValueError(f"lock_timeout_s must be a finite number >= 0, got {lock_timeout_s!r}")
        self.path = Path(path)
        self.lock_timeout_s = float(lock_timeout_s)

    def save_run(self, result: Dict[str, Any]) -> None:
        """Save a complete run in one transaction.

        Raises ValueError (before touching the database) if ``result`` is not a complete,
        consistent run_test() result, and an ArchiveError subclass if it cannot be stored:
        DuplicateRunError, ArchiveLockedError, ArchiveFormatError, UnsupportedSchemaVersionError,
        ArchiveIntegrityError, or ArchiveError for other storage and filesystem failures.
        """
        run_row, measurement_rows, statistics_row = _rows_from_result(result)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArchiveError(f"cannot create the directory for archive {self.path}: {exc}") from exc

        conn = _connect(self.path, "rwc", self.lock_timeout_s)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")  # take the write lock before checking anything
                if _is_uninitialized(conn):
                    for statement in _SCHEMA:
                        conn.execute(statement)
                else:
                    _check_identity(conn, self.path)
                if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_row[0],)).fetchone():
                    raise DuplicateRunError(f"run {run_row[0]} is already archived in {self.path}")
                conn.execute(_INSERT_RUN, run_row)
                conn.executemany(_INSERT_MEASUREMENT, measurement_rows)
                conn.execute(_INSERT_STATISTICS, statistics_row)
                conn.execute("COMMIT")
            except BaseException as exc:
                _rollback_if_active(conn, exc)
                raise
        except sqlite3.Error as exc:
            raise _translate(exc, self.path) from exc
        finally:
            conn.close()

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection in one snapshot, after the identity and schema checks.

        Never creates a missing file. sqlite3 errors become ArchiveError subclasses, and the
        connection is always closed.
        """
        if not self.path.is_file():
            raise ArchiveNotFoundError(f"archive {self.path} does not exist")
        conn = _connect(self.path, "ro", self.lock_timeout_s)
        conn.row_factory = sqlite3.Row
        try:
            try:
                conn.execute("BEGIN")  # one consistent snapshot for all queries of the operation
                _check_identity(conn, self.path)
                yield conn
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise _translate(exc, self.path) from exc
        finally:
            conn.close()

    def load_run(self, run_id: str) -> Dict[str, Any]:
        """Load one run and rebuild its run_test() result. Never creates or modifies the file.

        ``run_id`` must be a canonical UUID (ValueError otherwise, before the file is opened).
        """
        validate_run_id(run_id)
        with self._reading() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFoundError(f"run {run_id} is not archived in {self.path}")
            measurements = conn.execute(
                "SELECT * FROM measurements WHERE run_id = ? ORDER BY attempt", (run_id,)).fetchall()
            stats = conn.execute("SELECT * FROM run_statistics WHERE run_id = ?", (run_id,)).fetchone()
            return _result_from_rows(run, measurements, stats, self.path)

    def list_runs(self, *, ammeter: Optional[str] = None, since: Optional[datetime] = None,
                  until: Optional[datetime] = None, limit: int = DEFAULT_LIST_LIMIT) -> List[Dict[str, Any]]:
        """Summaries of archived runs, newest start time first (ties: run_id, descending).

        - ``ammeter``: exact, case-sensitive device name (no wildcards).
        - ``since`` (inclusive) and ``until`` (exclusive): timezone-aware datetimes compared
          with each run's start time; they are converted to UTC first.
        - ``limit``: 1 to MAX_LIST_LIMIT runs.

        Measurement rows are not read. A run without a statistics row raises
        ArchiveIntegrityError rather than being left out.
        """
        if ammeter is not None and (not isinstance(ammeter, str) or not ammeter):
            raise ValueError(f"ammeter must be a non-empty string, got {ammeter!r}")
        bounds = {name: None if value is None else _utc_bound(name, value)
                  for name, value in (("since", since), ("until", until))}
        if bounds["since"] is not None and bounds["until"] is not None and bounds["since"] >= bounds["until"]:
            raise ValueError(f"since ({bounds['since']}) must be earlier than until ({bounds['until']})")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be a whole number from 1 to {MAX_LIST_LIMIT}, got {limit!r}")

        conditions, parameters = [], []
        if ammeter is not None:
            conditions.append("r.ammeter = ?")
            parameters.append(ammeter)
        if bounds["since"] is not None:
            conditions.append("r.started_at_utc >= ?")
            parameters.append(bounds["since"])
        if bounds["until"] is not None:
            conditions.append("r.started_at_utc < ?")
            parameters.append(bounds["until"])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        # LEFT JOIN, so a run whose statistics row is missing is detected instead of dropped.
        sql = (
            "SELECT r.run_id, r.ammeter, r.started_at_utc, r.ended_at_utc, r.attempted, r.succeeded, "
            "r.failed, r.skipped_slots, r.stop_reason, s.run_id AS statistics_run_id, s.status, s.mean_a "
            f"FROM runs AS r LEFT JOIN run_statistics AS s ON s.run_id = r.run_id {where} "
            "ORDER BY r.started_at_utc DESC, r.run_id DESC LIMIT ?"
        )
        with self._reading() as conn:
            rows = conn.execute(sql, (*parameters, limit)).fetchall()

        missing = [row["run_id"] for row in rows if row["statistics_run_id"] is None]
        if missing:
            raise ArchiveIntegrityError(
                f"archived run(s) in {self.path} have no run_statistics row: {', '.join(missing)}")
        return [{
            "run_id": row["run_id"],
            "ammeter": row["ammeter"],
            "started_at_utc": row["started_at_utc"],
            "ended_at_utc": row["ended_at_utc"],
            "counts": {key: row[key] for key in ("attempted", "succeeded", "failed", "skipped_slots")},
            "stop_reason": row["stop_reason"],
            "analysis_status": row["status"],
            "mean_a": row["mean_a"],
        } for row in rows]

    def compare_runs(self, run_id_a: str, run_id_b: str) -> Dict[str, Any]:
        """Load two runs and compare them side by side (see compare_results)."""
        validate_run_id(run_id_a)
        validate_run_id(run_id_b)
        return compare_results(self.load_run(run_id_a), self.load_run(run_id_b))

    def export_run(self, run_id: str, output_path: Union[str, Path]) -> Path:
        """Write one complete run as UTF-8 JSON to a new file and return its path.

        The JSON is produced before the file is created. An existing file is never
        overwritten (or removed), and the directory must already exist. If writing fails,
        the partly written new file is removed and ExportError is raised. If the process is
        killed while writing, a partial file can remain; there is no atomic publication.
        """
        validate_run_id(run_id)
        data = (json.dumps(self.load_run(run_id), indent=2, ensure_ascii=False, allow_nan=False)
                + "\n").encode("utf-8")
        return write_new_file(output_path, data)


def write_new_file(output_path: Union[str, Path], data: bytes) -> Path:
    """Write ``data`` to a new file and return its path (used by export and the HTML report).

    The caller produces ``data`` before calling, so nothing is created if that fails. The
    directory must already exist, and an existing file is never overwritten or removed. If
    writing fails, the partly written new file is removed (or, if that also fails, the error
    says so and names it) and ExportError is raised with the write error as its cause. If the
    process is killed while writing, a partial file can remain; there is no atomic publication.
    """
    output = Path(output_path)
    if not output.parent.is_dir():
        raise ExportError(f"output directory {output.parent} does not exist")
    try:
        stream = _create_exclusive(output)
    except FileExistsError as exc:
        raise ExportError(f"{output} already exists; it was not overwritten") from exc
    except OSError as exc:
        raise ExportError(f"cannot create {output}: {exc}") from exc
    # From here on the file is ours, so it may be removed if the write fails.
    try:
        with stream:
            stream.write(data)
    except BaseException as exc:
        cleanup_error: Optional[OSError] = None
        try:
            output.unlink()
        except OSError as unlink_error:
            cleanup_error = unlink_error
            exc.add_note(f"the partial file {output} could not be removed: {unlink_error!r}")
        if isinstance(exc, OSError):
            if cleanup_error is None:
                message = f"writing {output} failed; the partial file was removed: {exc}"
            else:
                message = (f"writing {output} failed: {exc}; the partial file {output} could NOT be "
                           f"removed ({cleanup_error}); delete it manually")
            raise ExportError(message) from exc
        raise
    return output


def _create_exclusive(path: Path) -> BinaryIO:
    """Create ``path`` for writing; FileExistsError if it already exists (never truncates)."""
    return open(path, "xb")


def _utc_bound(name: str, value) -> str:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime, got {value!r}")
    try:
        return utc_timestamp(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be timezone-aware, got {value!r}") from exc


# --- Comparison --------------------------------------------------------------------

_RUN_CONTEXT_KEYS = ("ammeter", "host", "port", "command")


def compare_results(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two run_test() results side by side, without the attempt lists.

    ``metrics`` holds each of the five metrics for both runs. ``difference_b_minus_a`` is
    present only when both values exist and their difference is a finite float; otherwise
    ``difference_unavailable`` explains why. ``context_differences`` lists the device and
    settings fields that differ: context for reading the numbers, not a reason to refuse the
    comparison. No ranking or pass/fail judgement is made.
    """
    def summary(result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "run_id": result["run_id"],
            **{key: result[key] for key in _RUN_CONTEXT_KEYS},
            "started_at_utc": result["started_at_utc"],
            "settings": result["settings"],
            "counts": result["counts"],
            "stop_reason": result["stop_reason"],
            "analysis_status": result["analysis"]["status"],
        }

    differences = [key for key in _RUN_CONTEXT_KEYS if a[key] != b[key]]
    differences += [f"settings.{key}" for key in a["settings"] if a["settings"][key] != b["settings"].get(key)]

    metrics = {}
    for key in _METRICS:
        value_a, value_b = a["analysis"][key], b["analysis"][key]
        entry: Dict[str, Any] = {"a": value_a, "b": value_b}
        if value_a is None or value_b is None:
            missing = [f"run {label} (analysis status {result['analysis']['status']!r})"
                       for label, result, value in (("a", a, value_a), ("b", b, value_b)) if value is None]
            entry["difference_unavailable"] = f"no value for {' and '.join(missing)}"
        else:
            difference = value_b - value_a
            if math.isfinite(difference):
                entry["difference_b_minus_a"] = difference
            else:
                entry["difference_unavailable"] = "b - a is too large to represent as a finite float"
        metrics[key] = entry

    return {"run_a": summary(a), "run_b": summary(b), "context_differences": differences, "metrics": metrics}
