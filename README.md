# Ammeter Emulators

This project provides emulators for different types of ammeters: Greenlee, ENTES, and CIRCUTOR. Each ammeter emulator runs on a separate thread and can respond to current measurement requests.

On top of the emulators it adds a testing framework:
- one client call for all three ammeter types;
- configurable sampling by count, duration and frequency;
- statistics for each run (mean, median, standard deviation, minimum, maximum);
- a SQLite archive of runs, with a command-line tool to list, show, compare and export them.

**Requirements**
- **Python 3.11.** The code needs at least 3.11, because it uses 3.11 features such as exception notes and `sqlite3` error names. Only Python 3.11 has been tested (see below); later versions have not been tested.
- **PyYAML** is the only third-party runtime dependency, and **pytest** is used for the tests (see [Development Setup and Tests](#development-setup-and-tests)). `main.py` itself needs only the standard library.
- **Tested platforms:** the full test suite, `main.py`, the sampling demo and a separate-process check of the archive pass on:
  - GitHub Actions `windows-latest`: Windows Server 2025 (10.0.26100), Python 3.11.9;
  - GitHub Actions `ubuntu-latest`: Ubuntu 26.04.1 LTS, Python 3.11.18;
  - GitHub Actions `macos-latest`: macOS 26.7.2 on Apple silicon (arm64), Python 3.11.9;
  - locally: Windows 10 (build 19045), Python 3.11.3.

  The CI runs use the workflow in `.github/workflows/ci.yml` ([passing run](https://github.com/drorh316/ammeter-testing-framework/actions/runs/42576548988)). Other operating system versions, architectures and Python versions have not been tested.

**Quick start** (Windows PowerShell, from the project root; Linux/macOS equivalents are in each section):
```powershell
python main.py                                         # one measurement from each emulator
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                     # automated tests
.\.venv\Scripts\python.exe -m examples.run_tests         # sample every ammeter and archive the runs
.\.venv\Scripts\python.exe -m src.archive_cli list       # query the archive
```

**Further documentation**
- [docs/design.md](docs/design.md): design decisions, data flow, storage, error handling, testing strategy, tradeoffs and the status of the bonus challenges.
- [sample_results/](sample_results/README.md): three runs exported from the archive, one per ammeter type.
- [Known limitations](#known-limitations) and [Fixes to the Supplied Code](#fixes-to-the-supplied-code).

## Project Structure

- `main.py`: Main script (project root) to start the ammeter emulators and request current measurements.
- `Ammeters/`
  - `Circutor_Ammeter.py`: Emulator for the CIRCUTOR ammeter.
  - `Entes_Ammeter.py`: Emulator for the ENTES ammeter.
  - `Greenlee_Ammeter.py`: Emulator for the Greenlee ammeter.
  - `base_ammeter.py`: Base class for all ammeter emulators.
  - `client.py`: Client to request current measurements from the ammeter emulators (returns a validated `float`).
- `config/`
  - `config.yaml`: Sampling settings and the ammeters (host, port, command) to sample.
- `examples/`
  - `run_tests.py`: Sampling demo: starts the emulators and samples every configured ammeter (replaces the broken supplied placeholder).
- `src/`
  - `archive_cli.py`: Command-line interface to the archive: `list`, `show`, `compare`, `export`.
  - `testing/`
    - `sampling.py`: Sampling scheduler (`SamplingSettings`, `run_sampling`) and result records; no network or file access.
    - `analysis.py`: `summarize_measurements`: statistics of the successful measurements.
    - `performance.py`: `build_performance_report`: timing and failure consistency of one run, with optional user-defined limits.
    - `visualization.py`: `render_run_report`: self-contained HTML report with SVG charts for one run.
    - `archive.py`: `ResultArchive`: SQLite archive of results (schema, transactional save, load, list, compare, export).
    - `test_framework.py`: `AmmeterTestFramework`: loads and validates the configuration; `run_test(ammeter_type)` samples one ammeter.
  - `utils/`
    - `config.py`: Loads `config.yaml` and validates the sampling and ammeter sections.
    - `logger.py`: Logging setup (supplied; not used yet).
    - `Utils.py`: Utility functions, including `generate_random_float`.
- `tests/`
  - `test_client.py`: Unit tests for the client (fake socket and clock; no emulators needed).
  - `test_sampling.py`: Scheduler tests with a fake clock, sleep and measurement function.
  - `test_framework.py`: Configuration validation and `run_test` tests (fake client; no network).
  - `test_analysis.py`: Statistics tests.
  - `test_performance.py`: Performance report and `performance` command tests.
  - `test_visualization.py`: HTML/SVG report and `plot` command tests.
  - `test_demo_summary.py`: Demo summary tests (no emulators started).
  - `test_emulators.py`: Emulator output encoding, port-reuse option, and the startup helper's stopped-emulator check (threads, and one real loopback socket on a temporary port).
  - `test_archive.py`: SQLite archive integration tests on real temporary database files.
  - `test_archive_queries.py`: Listing, filtering, comparison and export tests (real temporary archives).
  - `test_archive_cli.py`: Command-line interface tests, including exit codes.
  - `fakes.py`: Shared fake clock, sleep and measurement function.
  - `conftest.py`: Stops any test from writing to the shipped results database.
- `docs/`
  - `design.md`: Design decisions (see above).
- `sample_results/`: Three runs exported from the demo archive, one per ammeter type (see its README).
- `pytest.ini`: pytest settings (test folder and import path).
- `requirements.txt`: Runtime dependencies (PyYAML).
- `requirements-dev.txt`: Development dependencies (runtime dependencies plus pytest).

## Usage

From the project root, start the ammeter emulators and request one current measurement from each:
```sh
python main.py
```

`main.py` starts the three emulators in background threads and waits 5 seconds for them to start listening. It then sends one request to each port and prints the measurement each one returns. If an emulator thread has already stopped after the wait (for example, its port was in use), `main.py` names it and exits with code 1 without sending requests; if any request fails, it prints the error and exits with code 1. The thread check only detects an emulator that crashed: a running thread does not prove the server is ready, so a request can still fail. The sampling demo uses the same startup helper and check.

`main.py` and the client use only the Python standard library; nothing needs to be installed to run it.

### Sampling demo

After the [development setup](#development-setup-and-tests) (which installs PyYAML into `.venv`), run from the project root:
```powershell
.\.venv\Scripts\python.exe -m examples.run_tests
```
(Linux / macOS: `.venv/bin/python -m examples.run_tests`.) It validates `config/config.yaml`, starts the three emulators the same way as `main.py` (including the 5-second startup delay), samples each configured ammeter with `AmmeterTestFramework().run_test(name)`, and prints a summary of each run. The emulators print their own lines while being sampled. With archiving enabled (the shipped configuration), each run is saved to `results/ammeter_results.sqlite3` and then reloaded through a new connection and compared with the collected result. The exit code is 0 only if every ammeter had at least one attempt, no attempt failed, the statistics were calculated, and the run was archived and reloaded unchanged. A run with no attempts is reported as "NO MEASUREMENTS" with its timing shown as unavailable; an analysis or archive failure is reported for that run while its collected measurements are still shown.

### Sampling

`AmmeterTestFramework(config_path=None).run_test(ammeter_type)` samples one ammeter (`"greenlee"`, `"entes"` or `"circutor"`, the keys of the `ammeters` section) using the settings in `testing.sampling`:

| Setting | Required | Meaning |
|---|---|---|
| `sampling_frequency_hz` | yes | Samples per second (from 1/86400 to 1000). |
| `measurements_count` | one of the two limits | Maximum number of **attempted** requests, failures included (from 1 to 2⁶³ − 1, the signed 64-bit range the archive can store; checked even when the duration is the tighter limit). |
| `total_duration_seconds` | one of the two limits | Upper bound on the sampling window, in seconds (at most 604800, one week). |
| `request_timeout_seconds` | no (default 5.0) | Timeout for one request (at most 3600); clipped to the time remaining in the window. |

**Implementation limits.** The numeric ranges above, and a maximum of **1,000,000 potential attempts per run**, are choices of this implementation, not assignment requirements, and they do not promise any timing precision. Every attempt record is kept in memory, so a run is limited by the attempts its stopping limits could allow: the count, or the number of schedule targets inside the duration window, whichever is smaller when both are set. The duration bound is computed with the same arithmetic as the schedule, so it is never lower than the real number of targets (for example, 2 Hz for 500,000 s allows exactly 1,000,000 and is accepted; 500,000.5 s allows 1,000,001 and is rejected). Settings that exceed the limit are rejected before sampling starts rather than stopped early. When a run starts, it also checks that the sampling interval and the duration deadline can be represented on the clock (a duration too small to change the clock's floating-point value is rejected with `ValueError` before any request).

Use `null` to leave out a limit. The configuration file is found relative to the project, not the current working directory. All settings and every ammeter entry are validated when `AmmeterTestFramework` is created, before any network activity; errors name the field, for example `testing.sampling: measurements_count must be a whole number or null, got 2.5 (float)`. Unknown keys are rejected to catch typos. YAML note: PyYAML reads `1e-3` (no decimal point) as text; write `1.0e-3` or `0.001`.

**Schedule.** Slot *k* has the target time *k / f* after the start of the run; the first request is sent immediately, and targets are computed from the start time, so lateness does not accumulate.
- A selected slot always runs, even if the wake-up from sleep is late; its actual start and lateness are recorded. If sleep returns early, the scheduler sleeps again, so it never samples before the target.
- After an attempt finishes, the next attempt uses the earliest later slot whose target is at or after the finish time. Slots passed while the request was running are skipped (no catch-up requests) and counted in `skipped_slots`, but only slots inside the duration window. For example, at 2 Hz, a request that ends at 1.2 s skips the 0.5 s and 1.0 s targets and the next request starts at 1.5 s. A request ending exactly at 0.5 s may still use the 0.5 s slot; one ending at 0.5001 s skips it.
- If the end of the duration window passes while waiting for a selected slot (for example, a very late wake-up), that slot and any later slots before the end of the window are counted as skipped. Targets at or after the end are never counted, and nothing is counted after the count limit has stopped the run.
- Slot decisions use the same arithmetic as the targets, so values such as 0.3 × 10 = 3.0000000000000004 do not cause a slot to be skipped.
- Communication failures are recorded and sampling continues at the next slot; there are no automatic retries. Other exceptions (for example, programming errors) are not caught.

**Stopping.**
- `count_reached`: `measurements_count` attempts were made. If the count and the duration both stop the run after the same attempt, `count_reached` is reported.
- `duration_reached`: the duration limit prevents any further attempt. No request starts at or after the end of the window, and the run returns as soon as the next target is at or after the end, without sleeping until then. So the elapsed time can be less than the duration (or slightly more, if the last request ran close to the end). A valid run can also end with zero attempts if it is delayed past the end of the window before the first request.
- The window is [0, D): with the first sample at 0, the targets are those below D. For example, D = 1.2 s at 2 Hz gives targets 0, 0.5 and 1.0 s (three attempts, not 2.4); D = 1.0 s gives 0 and 0.5 s.

**Timing precision** depends on the operating system and its load; it is not guaranteed. In the passing CI run (10 attempts per ammeter at 5 Hz, no failed attempts and no skipped slots on any system), the start of each request was at most 0.6 ms late on the Windows runner and 0.14 ms on the Ubuntu runner, but up to 85 ms late on the macOS runner (median 56 ms). These measurements come from shared virtual machines and show that correct results do not imply precise timing. Every attempt records its scheduled and actual start, so the actual timing can be checked. The scheduler uses `time.perf_counter`, which is monotonic like `time.monotonic` but finer-grained on some platforms: on the development Windows machine, `time.monotonic` advanced in steps of 15.625 ms, which made the recorded offsets and durations coarse.

**Result** (a JSON-compatible `dict`): `run_id` (a UUID4), `ammeter`, `host`, `port`, `command`, `started_at_utc` and `ended_at_utc` (wall-clock UTC, `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`; durations come from the monotonic `elapsed_s`), the effective `settings`, `counts` (`attempted`, `succeeded`, `failed`, `skipped_slots`), `elapsed_s`, `stop_reason`, `attempts` (one record per attempt, below), and `analysis` (see [Analysis](#analysis)):

```json
{"attempt": 2, "slot": 1, "scheduled_offset_s": 0.5, "start_offset_s": 0.5003, "lateness_s": 0.0003,
 "elapsed_s": 0.0004, "effective_timeout_s": 1.0, "timeout_clipped_by_deadline": false,
 "ok": true, "current_a": 0.0147}
```

A failed attempt has `"ok": false` and `"error": {"type": ..., "message": ...}` and **no** `current_a`, so later statistics cannot mistake a failure for 0 A. `effective_timeout_s` and `timeout_clipped_by_deadline` show whether the end of the window shortened the request timeout; this gives context for a timeout but does not prove whether the device or the short remaining time caused it. `src.testing.sampling.successful_values(result)` returns the successful values as a list.

### Analysis

Every `run_test` result also has an `analysis` section, computed by `summarize_measurements` in `src/testing/analysis.py` from the **successful measurements only**. Failed attempts have no value and are left out; they are never counted as 0 A. All values are in amperes (A) and kept at full floating-point precision; the demo rounds them only for display.

| Key | Meaning |
|---|---|
| `status` | `"ok"` (metrics calculated), `"no_data"` (no successful measurements) or `"error"` (see below). |
| `count` | Number of successful measurements used. |
| `mean_a` | Arithmetic mean. |
| `median_a` | Middle value; with an even count, the mean of the two middle values. |
| `stddev_population_a` | **Population** standard deviation (`statistics.pstdev`). |
| `min_a`, `max_a` | Smallest and largest measurement. |

- The **population** standard deviation is used because the metric describes the spread of the measurements collected in this run, not an estimate for a larger population (that would be the sample standard deviation, `statistics.stdev`).
- With no successful measurements, `count` is 0 and every metric is `null`. With one, the mean, median, minimum and maximum are that value and the standard deviation is 0.0.
- The metrics are computed with Python's `statistics` module; no other library is used. Zero and negative values are valid, and no acceptance thresholds are applied.
- Values that are not finite numbers (booleans, text, NaN, infinity) are rejected with `ValueError` rather than skipped; inside `run_test` that would be a programming error and is not caught. If finite but extreme values would give a statistic that is not a finite float (for example, the median of two values near the largest float), `summarize_measurements` raises `AnalysisError` (a `ValueError` subclass) instead of returning infinity. `run_test` catches only `AnalysisError`: it keeps every measurement and count, sets `status` to `"error"`, leaves the metrics `null`, and adds `"error": {"type": ..., "message": ...}`.
- The five metrics are always computed; the `analysis.statistical_metrics` key in `config.yaml` is an unused placeholder.

A small standard deviation shows that a run's measurements were consistent, not that they were accurate. The three emulators generate independent random values and have no shared reference or known true current, so these statistics cannot show which ammeter is more accurate.

### Result archive (SQLite)

When `result_management.archive_enabled` is true, `run_test` saves every result to a SQLite database using Python's built-in `sqlite3` module (no ORM, no server):

```yaml
result_management:
  archive_enabled: true
  database_path: results/ammeter_results.sqlite3   # relative paths are resolved against the project root
```

A missing or empty `result_management` section disables archiving. Generated databases and their `-journal`/`-wal`/`-shm` files are ignored by Git.

**Schema (version 1).** Three tables, with the archive identified by SQLite's `application_id` and its version by `user_version`:

| Table | Contents | Key constraints |
|---|---|---|
| `runs` | run ID, UTC start/end, device, effective settings, counts, elapsed time, stop reason | `run_id` primary key (NOT NULL); `attempted = succeeded + failed`; at least one stopping limit; valid stop reason, port and timestamp format |
| `measurements` | one row per attempt: slot, timing, effective timeout, clipped flag, and either `current_a` or the error type and message | foreign key to `runs`; primary key `(run_id, attempt)`; unique `(run_id, slot)`; exactly one of value or error |
| `run_statistics` | one row per run: analysis status, successful count, the five metrics, or the analysis error | `run_id` primary key and foreign key; the metrics and error columns must match the status |

```
runs (1 row per run)                      measurements (1 row per attempt)
┌──────────────────────────┐              ┌──────────────────────────────────┐
│ run_id            PK     │──1────────N──│ run_id   FK → runs.run_id        │
│ ammeter, host, port, ... │              │ attempt  PK (run_id, attempt)    │
│ settings, counts, ...    │              │ slot     UNIQUE (run_id, slot)   │
└──────────────────────────┘              │ current_a  XOR  error_type/msg   │
            │                             └──────────────────────────────────┘
            1
            │                             run_statistics (1 row per run)
            │                             ┌──────────────────────────────────┐
            └───────────────────────1─────│ run_id  PK, FK → runs.run_id     │
                                          │ status, successful_count, metrics│
                                          └──────────────────────────────────┘
```

Numeric columns must hold finite numbers of the right type. Integers must fit SQLite's signed 64-bit `INTEGER` range (−2⁶³ to 2⁶³ − 1); `save_run` rejects any integer field outside it with a field-specific `ValueError` before opening the database (otherwise `sqlite3` would raise `OverflowError`). The cross-table counts (attempt rows, successful rows, statistics count) are checked in Python before saving and again when loading.

**Behaviour.**
- A run and all its rows are saved in **one transaction**; if any insert fails, the whole save is rolled back, so a run is never partly archived. An existing run ID raises `DuplicateRunError` and the archived run is left unchanged.
- Every connection enables foreign keys, uses parameterized SQL, and is closed explicitly.
- Only a new or empty file is initialised. A file that is not a SQLite database, a database created by something else, or an archive missing a required table or column raises `ArchiveFormatError`; another schema version raises `UnsupportedSchemaVersionError`. These checks run in both `save_run` and `load_run` before any run row is read or inserted, and none of these cases changes the file. Tables and columns are checked by name only (column types and constraints are not compared). There is no migration code.
- `ResultArchive(path).load_run(run_id)` opens the file read-only: it never creates a missing file (`ArchiveNotFoundError`) and returns the same structure as `run_test`. A run whose rows are incomplete or inconsistent raises `ArchiveIntegrityError` instead of being returned.
- If a completed run cannot be archived, `run_test` raises `ResultArchiveError`; its `result` attribute holds the complete in-memory result, and `__cause__` holds the storage error.
- **Concurrency:** SQLite allows many readers but only one writer at a time. A save waits up to 5 seconds (`lock_timeout_s`) for the write lock and then raises `ArchiveLockedError`. This suits one test station, not many concurrent writers.
- **Crash during sampling:** a run is kept in memory and saved only when it finishes. If the sampling process crashes or is killed during a run, that run's measurements are lost (there is no incremental checkpointing).
- **Crash during a save:** each save is a SQLite transaction protected by SQLite's rollback journal (the archive keeps SQLite's default journal and sync settings). If the process stops mid-save, the next connection that can write to the file (the next save) rolls back the partial save, so runs that were already committed are kept and the interrupted run is absent. Until that happens, a read-only `load_run` cannot do the rollback and raises `ArchiveError` (cause `SQLITE_READONLY_ROLLBACK`). This was checked by killing a process during a save. As with any SQLite database, it relies on the storage honouring flushes to disk.

### Querying the archive (command line)

`src/archive_cli.py` lists, shows, compares and exports archived runs, and reports their performance consistency. Run it from the project root (so that `-m src.archive_cli` is found), with the virtual environment's Python:

```
python -m src.archive_cli [--db PATH | --config PATH] COMMAND ...
```

| Command | Output |
|---|---|
| `list [--ammeter NAME] [--since TIME] [--until TIME] [--limit N]` | A compact table of runs, newest start time first (ties ordered by run ID). |
| `show RUN_ID` | The complete run (settings, every attempt, statistics) as JSON. |
| `compare RUN_ID_A RUN_ID_B` | Both runs' device, settings, counts and analysis status, and the five metrics side by side, as JSON. |
| `export RUN_ID OUTPUT_PATH` | Writes the complete run to a new UTF-8 JSON file. |
| `performance RUN_ID [--max-start-lateness SECONDS] [--max-failure-rate RATE] [--max-skipped-slot-rate RATE]` | The run's [performance consistency report](#performance-consistency-report) as JSON, with optional limits. |
| `plot RUN_ID OUTPUT_PATH [limits]` | Writes a self-contained [HTML report with charts](#html-run-report-with-charts) to a new file. |

**Which database.** `--db` and `--config` are global options and go **before** the command; only one of them can be given.
- `--db PATH` reads that file; a relative path is resolved against the current working directory. The configuration is not read, so this works whether archiving is enabled, disabled or not configured.
- Without `--db`, the CLI uses `result_management.database_path` from `--config PATH` (default: the project's `config/config.yaml`), resolved against the project root as described above. This applies even when `archive_enabled` is false. If no path is configured, the command fails and asks for `--db`.
- No command creates a database, changes one, or starts the emulators. A missing database is reported, not created.

**Options and validation.**
- `--ammeter` matches the device name exactly and case-sensitively. `%`, `_`, quotes and SQL-like text are ordinary characters; every value is passed to SQLite as a parameter.
- `--since` (inclusive) and `--until` (exclusive) filter by run start time and need a UTC offset, for example `2026-11-08T17:00:00Z` or `2026-11-08T19:00:00+02:00`; they are converted to UTC before comparison. `--since` must be earlier than `--until`.
- `--limit` is 1 to 1000 (default 20). There is no paging.
- Run IDs must be canonical lowercase UUIDs, as shown by `list`; malformed IDs are rejected before the database is opened.
- `list` reads only the `runs` and `run_statistics` tables. A run without a statistics row is reported as an `ArchiveIntegrityError` rather than silently left out.

**Comparison.** For each metric, the output has both values and `difference_b_minus_a` (run B minus run A) when both exist. Otherwise, or if the difference is too large for a finite float, it has `difference_unavailable` with the reason (for example, `no value for run b (analysis status 'no_data')`). `context_differences` lists the device and settings fields that differ (such as `ammeter` or `settings.measurements_count`). These are context for reading the numbers, not a reason to refuse the comparison. The comparison does not rank devices, judge accuracy, or mark anything as pass or fail. The attempt lists are not repeated; use `show` for them.

**Export.**
- The JSON is produced before the destination is created, with `allow_nan=False` (strict JSON).
- The file is created exclusively: an existing file is never overwritten or removed, and the command fails instead.
- The output directory must already exist.
- If writing fails (for example, the disk is full), the partly written new file is removed. If it cannot be removed either, the error says so, names the file, and includes the reason, so it can be deleted by hand.
- Export is not atomic: if the process is killed while writing, a partial file can remain.

**Exit codes.**
- `0`: success.
- `1`: expected operational failure, such as a missing database or run, an invalid or unsupported archive, an existing export destination, or a configuration problem. The CLI prints one `error: ...` line on stderr. For `performance`, also a configured limit that fails or cannot be evaluated; the complete report is still printed, followed by `performance limits: fail` (or `insufficient_data`) on stderr.
- `2`: invalid command-line usage, such as an unknown option, a malformed run ID or timestamp, a limit out of range (including a negative lateness limit or a rate outside 0-1), or `--since` not earlier than `--until`.
- Unexpected exceptions are not caught; they propagate and show their traceback.

**Examples, Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m src.archive_cli list
.\.venv\Scripts\python.exe -m src.archive_cli list --ammeter greenlee --since 2026-11-08T00:00:00Z --limit 5
.\.venv\Scripts\python.exe -m src.archive_cli show 503ea15b-e9b0-42a6-af17-c36aeb520856
.\.venv\Scripts\python.exe -m src.archive_cli compare 503ea15b-e9b0-42a6-af17-c36aeb520856 a6a8e389-d288-46bb-843c-270dac01d654
.\.venv\Scripts\python.exe -m src.archive_cli export 503ea15b-e9b0-42a6-af17-c36aeb520856 .\greenlee_run.json
.\.venv\Scripts\python.exe -m src.archive_cli --db .\results\ammeter_results.sqlite3 list
```

**Examples, Linux / macOS:**

```bash
.venv/bin/python -m src.archive_cli list
.venv/bin/python -m src.archive_cli list --ammeter greenlee --since 2026-11-08T00:00:00Z --limit 5
.venv/bin/python -m src.archive_cli show 503ea15b-e9b0-42a6-af17-c36aeb520856
.venv/bin/python -m src.archive_cli compare 503ea15b-e9b0-42a6-af17-c36aeb520856 a6a8e389-d288-46bb-843c-270dac01d654
.venv/bin/python -m src.archive_cli export 503ea15b-e9b0-42a6-af17-c36aeb520856 ./greenlee_run.json
.venv/bin/python -m src.archive_cli --db ./results/ammeter_results.sqlite3 list
```

The run IDs above are those of the runs in [`sample_results/`](sample_results/README.md). Your own archive has different IDs; copy them from `list`.

The same operations are available in Python: `ResultArchive(path).list_runs(...)`, `load_run(run_id)`, `compare_runs(a, b)` and `export_run(run_id, path)`.

### Performance consistency report

`python -m src.archive_cli performance RUN_ID` (or `build_performance_report(run, limits)` in `src/testing/performance.py`) reports **how consistently a run was carried out**: failures, skipped schedule slots, how late each request started and how long it took. It is built from an archived run without changing the archive, and it changes nothing about sampling.

It does **not** describe the measured current: the spread of the current values is in the run's [analysis](#analysis). It also says nothing about accuracy, and it does not try to explain why a request was late.

| Field | Meaning |
|---|---|
| `run_id`, `ammeter`, `started_at_utc`, `settings` | Which run, and its effective sampling settings. |
| `counts` | `attempted`, `succeeded`, `failed`, `skipped_slots`. |
| `rates.failure_rate` | `failed / attempted`; `null` with no attempts. |
| `rates.skipped_slot_rate` | `skipped_slots / (attempted + skipped_slots)`: of the schedule slots inside the run, the share that was passed over instead of used. `null` when both are zero. |
| `start_lateness_s` | Summary of each attempt's actual start minus its scheduled start. |
| `request_duration_s` | Summary of each attempt's duration, until it returned or failed. |
| `thresholds` | Overall `status` and one entry per configured limit. |

**Timing summaries.**
- They are in **seconds** and use **every attempt, successful or failed**. A timed-out request counts with its real duration.
- Each summary has `count`, `mean_s`, `median_s`, `stddev_population_s`, `p95_s` and `max_s`.
- With no attempts, `count` is 0 and the other values are `null`.
- Values keep their full stored precision; nothing is rounded.
- `p95_s` uses the **nearest-rank** method, `sorted_values[ceil(0.95 * n) - 1]` (computed with integer arithmetic), so it is always an observed value. With fewer than 20 attempts it equals the maximum.

**Optional limits.** There are no built-in acceptance limits. These limits are chosen by the user and are not requirements of the assignment.

| Option | Check | Allowed values |
|---|---|---|
| `--max-start-lateness SECONDS` | `start_lateness_s.max_s` <= limit | finite, >= 0 |
| `--max-failure-rate RATE` | `rates.failure_rate` <= limit | 0 to 1 |
| `--max-skipped-slot-rate RATE` | `rates.skipped_slot_rate` <= limit | 0 to 1 |

- **Check results:** a value **equal to its limit passes**. Each configured check reports `pass`, `fail` or `insufficient_data` (the value is `null`, for example with no attempts), together with its `observed` value and `limit`.
- **Overall status:**
  - `not_evaluated` when no limits are given;
  - `fail` if any check fails;
  - `insufficient_data` if none fails but one cannot be evaluated;
  - `pass` otherwise.
- **Exit codes:** `0` for `pass` or `not_evaluated`; `1` for `fail` or `insufficient_data`, with the full report still printed; `2` for an invalid limit.

Example: [`sample_results/greenlee_performance.json`](sample_results/README.md#performance-report).

### HTML run report with charts

`python -m src.archive_cli plot RUN_ID OUTPUT_PATH` (or `render_run_report(run, limits)` in `src/testing/visualization.py`) writes one archived run as a **self-contained HTML file**.
- The charts are inline SVG. There is no JavaScript, no external stylesheet, font or CDN, and no new dependency, so the file opens offline in any browser.
- The command never opens a browser itself.
- The same optional `--max-start-lateness`, `--max-failure-rate` and `--max-skipped-slot-rate` limits as `performance` can be given. They are shown in the report ("Thresholds: not evaluated" when none are given) but do not change the exit code.

**Contents.**
- **Header:** run details (ID, device, address, command, UTC start and end, settings, counts, stop reason).
- **Current statistics** in amperes, from the run's `analysis`.
- **Performance summary** from `build_performance_report`, with timings in milliseconds.
- **Four charts.**
- **Table of failed attempts:** at most 100 rows; the report says when more exist.

A note at the top states that the values are random emulator readings and do not establish the accuracy of any device.

| Chart | x axis | y axis | Notes |
|---|---|---|---|
| Current vs. actual start offset | actual start (s) | current (A) | Successful readings as circles. Failed attempts have no current value, so they are red crosses in a separate "failed" strip below the plot area, never at 0 A. |
| Start lateness | actual start (s) | lateness (ms) | Every attempt; failed attempts as red crosses. |
| Request duration | actual start (s) | duration (ms) | Every attempt; failed attempts as red crosses (a timeout shows its full duration). |
| Histogram | current (A) | readings per bin | All successful readings of the run. |

- **Axes:** the three time-based charts share the same x axis, so they line up vertically. Each chart has axis titles with units, tick labels and a legend. Tick labels use the fewest significant digits (at least 3) that keep them distinct; only the labels are rounded, not the data.
- **Scatter only:** points are never connected, so no line crosses a failed attempt or a skipped slot.
- **Value axes are never clipped:** negative currents get a negative axis. A single value, or identical values, get a range of ±10% around the value (±1 for zero, or one float step either side for subnormal values, where 10% would round away).
- **Edge cases:**
  - A run with no attempts, or with no successful readings, shows a message in the empty chart.
  - A failed analysis is shown with its error, and the readings are still plotted.
- **Histogram bins:** Sturges' rule, `ceil(log2(n)) + 1` equal-width bins between the smallest and largest reading (at most 50). Each bin includes its left edge; the last bin also includes the maximum. Identical readings, including a single reading, give one bin. Readings are counted against the returned edges. If floating-point rounding makes neighbouring edges equal (a range only a few float steps wide), the duplicate edges are dropped, giving fewer bins; edges are never shifted.
- **Large runs:** the three point charts draw at most **2000 attempts** each.
  - Above that, they show an evenly spaced subset. Every failed attempt is kept if failures fit in half of the 2000-point cap; otherwise evenly spaced failures fill that half, and successful attempts fill the rest.
  - A "Downsampled charts" note gives the exact numbers shown.
  - The statistics, the performance summary and the histogram always use the full run.
- **Escaping:** all text taken from the run (device name, command, error messages, …) is HTML-escaped.
- **Writing the file:** the document is generated before the file is created. Writing then behaves exactly like `export` (shared code): a new file only, never overwritten, the directory must exist, and a partial file is removed if writing fails (or the error says it could not be).
- **Exit codes:** 0 when written; 1 for an operational failure (missing database or run, existing file, write failure); 2 for invalid usage.

Example: [`sample_results/greenlee_report.html`](sample_results/README.md#html-report).

### Client behaviour

`request_current_from_ammeter(port, command, timeout=5.0, host="127.0.0.1", max_response_bytes=1024)` in `Ammeters/client.py`:

- Returns the measured current as a `float`.
- Reads until the emulator closes the connection (which the emulators do after replying), so a response split across several TCP reads is reassembled.
- `timeout` (seconds, must be positive and finite) is **one shared budget** for the whole network exchange: connecting, sending the command, and receiving the complete response. One deadline is set before connecting; each blocking operation (`connect`, `sendall`, every `recv`) is given only the time left, and a response whose end arrives after the deadline is rejected. The total can still overrun slightly because of normal operating-system scheduling; it is not a hard real-time guarantee.
- `host` must be a **numeric IPv4 address** (default `127.0.0.1`). This is a deliberate client policy, separate from what the emulators support (they listen on IPv4 `localhost`). A numeric address needs no hostname resolution and gives exactly one address to connect to, so connecting, sending and receiving can share one timeout budget without trying several resolved addresses. Hostnames such as `localhost` and IPv6 addresses are therefore not accepted; an unsupported value raises `ValueError` before any network activity.
- Raises an `AmmeterCommunicationError` subclass that names the port, with the original exception kept as `__cause__`:
  - `AmmeterConnectionError`: the connection was refused, reset, or otherwise failed.
  - `AmmeterTimeoutError`: connecting, sending and receiving did not all finish within the timeout budget.
  - `InvalidMeasurementError`: the response was empty (for example, an unrecognised command), larger than `max_response_bytes`, not UTF-8, not a number, or NaN/infinity.

It never returns `None` or a placeholder value for a failed measurement.

## Development Setup and Tests

The framework needs **PyYAML** (the only runtime dependency, in `requirements.txt`) and the tests need `pytest`. `requirements-dev.txt` includes both; install it into a project-local virtual environment (the `.venv/` folder is ignored by Git):

Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Linux / macOS:
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Most unit tests replace the socket, clock, sleep and client with fakes, so the suite runs in a few seconds without starting the emulators on their ports, sending network requests or really sleeping. The emulator tests in `tests/test_emulators.py` are the exception: they start short-lived threads. One of them, the startup regression test, uses a real loopback socket on a temporary port chosen by the operating system (not 5001–5003) to check that an emulator whose port is in use is reported as stopped. The archive and CLI tests use real SQLite files in pytest's temporary directories, and one CLI test runs `python -m src.archive_cli` as a separate process; a guard in `tests/conftest.py` fails any test that would open or write the shipped `results/` database.

**Dependency change:** the supplied `requirements.txt` listed numpy, scipy, matplotlib, seaborn, pandas and pyyaml, none of which were used by the code. It now lists only PyYAML, following the "minimize external library dependencies" constraint. Libraries will be added back only if a later feature needs them.

## Ammeter Emulators

The ports below are the ones used by `main.py`. Each emulator only answers a request that matches its command exactly; any other request gets no reply.

### Greenlee Ammeter

- **Port**: 5001
- **Command**: `MEASURE_GREENLEE -get_measurement`
- **Measurement Logic**: Calculates current using voltage (1V - 10V) and (0.1Ω - 100Ω).
- **Measurement method** : Ohm's Law: I = V / R

### ENTES Ammeter

- **Port**: 5002
- **Command**: `MEASURE_ENTES -get_data`
- **Measurement Logic**: Calculates current using magnetic field strength (0.01T - 0.1T) and calibration factor (500 - 2000).
- **Measurement method** : Hall Effect: I = B * K

### CIRCUTOR Ammeter

- **Port**: 5003
- **Command**: `MEASURE_CIRCUTOR -get_measurement -current`
- **Measurement Logic**: Calculates current using voltage values (0.1V - 1.0V) over a number of samples and a random time step (0.001s - 0.01s).
- **Measurement method** : Rogowski Coil Integration: I = ∫V dt

## Known Limitations

- **Timing:** timing is best-effort and depends on the operating system and its load; it is not a real-time guarantee. Each attempt records its scheduled and actual start. Missed slots are skipped, not caught up. The client's request deadline uses `time.monotonic`, which advanced in 15.625 ms steps on the development Windows machine.
- **Connections:** each sample is a separate TCP connection, and a response ends when the emulator closes the connection. The emulators listen on IPv4 only. Separately, the client accepts only numeric IPv4 addresses, by design: this avoids hostname resolution and attempts to several addresses within one timeout budget (see [Client behaviour](#client-behaviour)).
- **Emulator startup:** `main.py` and the demo wait a fixed 5 seconds for the emulators to start, and the emulators print their own output while being sampled.
- **No accuracy assessment:** the emulators return independent random values with no known true current, so the statistics describe consistency within a run, not accuracy. The comparison does not rank devices.
- **Run size:** at most 1,000,000 potential attempts per run, because all attempt records are kept in memory.
- **Archive:**
  - One writer at a time.
  - No incremental checkpoints: a crash during sampling loses that run.
  - No schema migrations; tables and columns are checked by name only.
  - `compare` loads its two runs in separate snapshots.
  - `list` has no paging (at most 1000 runs).
- **Export** is not atomic: a process killed while writing can leave a partial file.
- **CLI:** it must be run from the project root, with `--db`/`--config` before the command.
- **Charts:** one run per HTML report (no cross-run dashboard). Markers are not clipped at the edge of the plot area, and the point charts draw at most 2000 attempts (larger runs are downsampled, with a note).
- **Not implemented** (optional bonuses): runtime error simulation and accuracy assessment. See [docs/design.md](docs/design.md#bonus-challenges).
- **Platforms:** tested only on the environments listed under [Tested platforms](#ammeter-emulators) (Windows Server 2025, Ubuntu 26.04 and macOS 26 runners with Python 3.11, and Windows 10 locally). On the macOS runner, requests started up to 85 ms after their target time.

## Fixes to the Supplied Code

### 1. `main.py` did not retrieve any measurements

- **Problem**: The three client requests in `main.py` were commented out. They also sent shortened commands (`MEASURE_GREENLEE`, `MEASURE_ENTES`, `MEASURE_CIRCUTOR`). Each emulator compares the request with its full command for an exact match and closes the connection without replying if it differs, so the client would only print `No data received.`
- **Fix**: Enabled the three requests and sent the exact command each emulator expects, keeping the existing ports (5001, 5002, 5003). The emulator code was not changed; the CIRCUTOR command keeps its `-current` suffix.
- **Documentation**: This README previously listed ports 5000–5002 and the CIRCUTOR command without `-current`, and referred to `Ammeters/main.py`, `examples/run_test.py` and `src/testing/AmmeterTester.py`. These were corrected to match the supplied code (`main.py` in the project root, `examples/run_tests.py`, `src/testing/test_framework.py`).

### 2. The client could not be reused for testing

- **Problem**: `request_current_from_ammeter` only printed the reply and returned `None`. It had no timeout, so an unresponsive ammeter could block forever. It made a single `recv` call, so a response split across TCP reads (possible even on localhost) could be cut short. It did not validate the reply, and a wrong command just printed `No data received.`
- **Fix**: The client now returns a validated `float`, reads until the server closes the connection within one overall deadline and a size limit, and raises a specific error naming the port for connection failures, timeouts, and invalid replies (see [Client behaviour](#client-behaviour)). Printing moved to `main.py`. Ports, commands, the emulators, and the 5-second startup delay are unchanged.

### 3. The client timeout was not a limit on the whole request

- **Problem**: The client connected to `localhost` with `socket.create_connection`. On the development machine `localhost` resolved to `::1` (IPv6) first and then `127.0.0.1`, but the emulators listen on IPv4 only. `create_connection` gives each resolved address its own full timeout, and sending and receiving each had their own timeout too, so one request could take several times the configured timeout. In local measurements on that Windows machine, a successful request to `localhost` took about 2 s, and a request to a closed port with `timeout=1.0` took about 2 s (these are observations from one machine, not general rules).
- **Fix**: The client now connects with an IPv4 socket directly to a numeric IPv4 address (default `127.0.0.1`) and uses one shared deadline across connecting, sending and receiving. In local measurements on the same machine (20 requests per emulator, timed after startup), the median successful request took about 0.3 ms and the slowest about 16 ms; a closed port with `timeout=1.0` failed after about 1.0 s.

### 4. The test framework, configuration and example were unusable

- **Problem**: `src/testing/test_framework.py` failed on import (`Dict` was not imported) and `run_test` was empty. `config/config.yaml` had the sampling values set to `NULL`, and its ammeter entries were commented out with the wrong ports (5000–5002) and the CIRCUTOR command without `-current`. `load_config` opened a path relative to the current working directory. `examples/run_tests.py` called `run_test()` without the required ammeter type.
- **Fix**: Implemented sampling (see [Sampling](#sampling)) behind the existing `run_test(ammeter_type)` interface, with validated configuration that matches the working emulators and a default configuration path independent of the working directory. Replaced the example with a working [sampling demo](#sampling-demo). `main.py` and the emulators were unchanged by this fix.

### 5. The Greenlee emulator crashed when its output was redirected on Windows

- **Problem**: `GreenleeAmmeter.measure_current` printed the ohm sign (`Ω`). With stdout redirected to a file or pipe on Windows, Python writes it in the locale encoding, and on the CI Windows runner and the development machine that was cp1252, which has no ohm sign. The print raised `UnicodeEncodeError` inside the emulator thread, so the thread ended. The request got an empty response and every later Greenlee request was refused. Found by running `main.py > log.txt` locally, and confirmed by the first Windows CI run.
- **Fix**: The diagnostic line now prints `ohm` instead of `Ω`. The measured value, the command and the reply are unchanged. A test calls each emulator's `measure_current` with a strict cp1252 stdout.

### 6. The emulators could not be restarted immediately on Linux and macOS

- **Problem**: Each emulator closes the connection first after replying, so the closed connections stay in TCP TIME_WAIT on the emulator's port. On Linux and macOS, binding a new listener to that port fails during TIME_WAIT unless `SO_REUSEADDR` is set. In the first CI run, the sampling demo started right after `main.py` and all three emulators failed with `OSError: [Errno 98] Address already in use` (Ubuntu) and `[Errno 48]` (macOS). Every request was refused. Windows allowed the immediate re-bind.
- **Fix**: `start_server` sets `SO_REUSEADDR` before `bind` on POSIX systems only. On Windows it is left unset, because there it would let a second socket bind a port that is in active use. A second listener on a port that is actively listening is still refused on all three systems. A unit test checks that the option is set before `bind` only where enabled, and the CI workflow runs the demo immediately after `main.py`.

### 7. Emulator startup failures were only detected indirectly

- **Problem**: `main.py` started the emulator threads and waited 5 seconds without checking them. If an emulator failed to start (for example, its port was in use), its traceback was printed but the script went on. The failure only showed up later as refused requests, or not at all if another program on that port answered.
- **Fix**: A shared helper in `main.py` (`start_emulators` and `stopped_emulators`), also used by the sampling demo, starts the threads, waits the same 5 seconds and then checks that every emulator thread is still alive. If one has stopped, the script names it and exits with code 1 before sending any request. This detects startup crashes only; it does not show that a server is ready to accept connections. Tests cover a real emulator whose port is already in use, and both scripts' early exit.
