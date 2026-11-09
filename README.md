# Ammeter Emulators

This project provides emulators for different types of ammeters: Greenlee, ENTES, and CIRCUTOR. Each ammeter emulator runs on a separate thread and can respond to current measurement requests.

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
  - `testing/`
    - `sampling.py`: Sampling scheduler (`SamplingSettings`, `run_sampling`) and result records; no network or file access.
    - `analysis.py`: `summarize_measurements`: statistics of the successful measurements.
    - `archive.py`: `ResultArchive`: SQLite archive of results (schema, transactional save, load).
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
  - `test_demo_summary.py`: Demo summary tests (no emulators started).
  - `test_archive.py`: SQLite archive integration tests on real temporary database files.
  - `fakes.py`: Shared fake clock, sleep and measurement function.
  - `conftest.py`: Stops any test from writing to the shipped results database.
- `pytest.ini`: pytest settings (test folder and import path).
- `requirements.txt`: Runtime dependencies (PyYAML).
- `requirements-dev.txt`: Development dependencies (runtime dependencies plus pytest).

## Usage

From the project root, start the ammeter emulators and request one current measurement from each:
```sh
python main.py
```

`main.py` starts the three emulators in background threads, waits 5 seconds for them to start listening, then sends one request to each port and prints the measurement each one returns. If any request fails, it prints the error and exits with code 1.

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

**Timing precision** depends on the operating system and its load; it is not guaranteed. Every attempt records its scheduled and actual start, so the actual timing can be checked. The scheduler uses `time.perf_counter`, which is monotonic like `time.monotonic` but finer-grained on some platforms: on the development Windows machine, `time.monotonic` advanced in steps of 15.625 ms, which made the recorded offsets and durations coarse.

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

Listing, comparing and exporting archived runs are planned for the next stage.

### Client behaviour

`request_current_from_ammeter(port, command, timeout=5.0, host="127.0.0.1", max_response_bytes=1024)` in `Ammeters/client.py`:

- Returns the measured current as a `float`.
- Reads until the emulator closes the connection (which the emulators do after replying), so a response split across several TCP reads is reassembled.
- `timeout` (seconds, must be positive and finite) is **one shared budget** for the whole network exchange: connecting, sending the command, and receiving the complete response. One deadline is set before connecting; each blocking operation (`connect`, `sendall`, every `recv`) is given only the time left, and a response whose end arrives after the deadline is rejected. The total can still overrun slightly because of normal operating-system scheduling; it is not a hard real-time guarantee.
- `host` must be a **numeric IPv4 address** (default `127.0.0.1`, where the emulators listen). Hostnames such as `localhost`, IPv6 addresses and DNS resolution are not supported; an unsupported value raises `ValueError` before any network activity.
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

The tests replace the socket, clock, sleep and client with fakes, so they run in about a second and do not start the emulators, open network connections or really sleep.

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
- **Fix**: Implemented sampling (see [Sampling](#sampling)) behind the existing `run_test(ammeter_type)` interface, with validated configuration that matches the working emulators and a default configuration path independent of the working directory. Replaced the example with a working [sampling demo](#sampling-demo). `main.py` and the emulators are unchanged.