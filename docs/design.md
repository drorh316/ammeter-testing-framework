# Design decisions

This document explains how the testing framework is put together and why. The [README](../README.md) is the reference for usage, configuration and exact behaviour.

## Flow: measurement → sampling → analysis → storage

```
config/config.yaml ──► AmmeterTestFramework (validates everything before any network use)
                              │ run_test("greenlee")
                              ▼
                       run_sampling(measure, settings)       src/testing/sampling.py
                              │ calls measure(timeout) on a fixed schedule
                              ▼
          request_current_from_ammeter(port, command, ...)   Ammeters/client.py  ──TCP──► emulator
                              │ float, or an AmmeterCommunicationError
                              ▼
                       attempt records (value or error, timing, slot)
                              │ successful values only
                              ▼
                       summarize_measurements(values)        src/testing/analysis.py
                              │
                              ▼
                       result dict ──► ResultArchive.save_run   src/testing/archive.py (SQLite)
                                                  │
                                                  ▼
                              list / show / compare / export    src/archive_cli.py
```

1. **Measurement.** `request_current_from_ammeter` sends one command and returns a validated `float`, or raises an `AmmeterCommunicationError` subclass: `AmmeterConnectionError`, `AmmeterTimeoutError` or `InvalidMeasurementError`. It is the same for all three ammeter types; only the port and command differ, and they come from the configuration. That shared call is the unified measurement API.
2. **Sampling.** `run_sampling` calls a `measure(timeout)` function at slot *k*, scheduled *k / f* after the start of the run. It stops at the count limit, the duration limit, or whichever comes first. It knows nothing about sockets or files, so it is tested with a fake clock.
3. **Analysis.** `summarize_measurements` computes the mean, median, population standard deviation, minimum and maximum of the **successful** values only. A failed attempt is never counted as 0 A.
4. **Storage.** `run_test` builds one JSON-compatible result: run ID, device, UTC times, effective settings, counts, every attempt and the analysis. If archiving is enabled, it saves the result to SQLite. The CLI reads it back for listing, comparison and export.

Every run returns the same result structure, whichever ammeter produced it. That structure is what is archived, exported and compared.

## Why SQLite through the standard-library `sqlite3` module

The assignment asks for unique run IDs, metadata, and easy retrieval and comparison of past results. It also asks for minimal external dependencies and cross-platform code.

- **No new dependency or server.** `sqlite3` ships with Python, and the archive is one portable file. PyYAML stays the only runtime dependency.
- **Built-in querying, relational constraints and transactional storage.**
  - **Querying:** listing filters runs by device and start time in SQL (supported by an index on device and start time), without reading measurement rows.
  - **Constraints:** the schema enforces types, finite numbers, foreign keys between the tables and statistics consistent with their status, in addition to the Python validation.
  - **Transactions:** a run and all its rows are saved in one transaction.
- **Comparison is done in Python.** `compare_runs` loads both runs through `load_run` and computes the side-by-side view and the differences in Python; SQL is only used to read the runs.
- **Alternatives considered:**
  - One JSON file per run would be simpler and would also work. Filtering would then mean reading and parsing every file, and the checks that the database enforces would have to be written and kept in Python.
  - A server database such as PostgreSQL would handle many concurrent writers better, but it would need a server and a driver dependency, which is out of proportion for one test station.

## Tables and relationships

| Table | One row per | Key |
|---|---|---|
| `runs` | run: ID (UUID4), device, UTC start/end, effective settings, counts, stop reason | `run_id` |
| `measurements` | attempt: slot, timing, effective timeout, and either `current_a` or the error | `(run_id, attempt)`, foreign key to `runs` |
| `run_statistics` | run: analysis status (`ok` / `no_data` / `error`), successful count, metrics or error | `run_id`, foreign key to `runs` |

`runs` 1 ── N `measurements`, and `runs` 1 ── 1 `run_statistics`.

The file is identified by SQLite's `application_id`, and the schema version (1) by `user_version`. A foreign database, another version, or a missing table or column is rejected without changing the file. There is no migration code.

## Transaction boundaries and recovery

- **One save, one transaction.** `save_run` validates the result in Python, then runs `BEGIN IMMEDIATE`, checks or initialises the schema, checks for a duplicate run ID, inserts all rows and commits. Any failure rolls the whole save back, and an existing run is never replaced.
- **Reads** (load, list, compare, export) open the file read-only, never create it, and read inside one transaction per operation. `compare_runs` loads its two runs in **separate** read snapshots.
- **One writer at a time.** SQLite allows a single writer; a save waits up to 5 s for the write lock and then fails.
- **Crash during sampling.** A run is held in memory and saved only when it finishes, so a crash during sampling loses that run. There are no incremental checkpoints.
- **Crash during a save.** SQLite's rollback journal undoes the interrupted save the next time a connection that can write opens the file. Until then, a read-only load fails with an `ArchiveError`. This was checked once by killing a process during a save; that check is not part of the automated tests.
- **Export is not atomic.** It creates the file exclusively and never overwrites. On a handled write failure it removes its partial file, or reports that it could not. If the process is killed while writing, a partial file can remain.

## Error handling

- **Configuration** is validated completely in the `AmmeterTestFramework` constructor, before any network activity. Errors are `ConfigError` and name the field.
- **Communication failures** are recorded as failed attempts, and sampling continues at the next slot. There are no automatic retries. Other, unexpected exceptions are not caught; they propagate to the caller.
- **Analysis:** only `AnalysisError` (a statistic that cannot be represented as a finite float) is caught. The measurements are kept and the analysis status becomes `error`.
- **Storage** errors are `ArchiveError` subclasses.
  - When an SQLite or filesystem error is wrapped (for example a locked or malformed database, a rejected insert, or a failed directory creation or export write), the original exception is kept as `__cause__`.
  - Errors the archive detects itself have no underlying SQLite exception. Examples: a missing database or run, a foreign archive, an unsupported schema version, a duplicate run ID, or inconsistent rows.
  - Invalid input is rejected with `ValueError` before the database is opened.
  - If a finished run cannot be saved, `ResultArchiveError.result` still holds the complete in-memory result, so no data is silently lost.
- **CLI exit codes:** `0` success, `1` expected operational failure (one `error:` line on stderr), `2` invalid usage. Unexpected exceptions are not caught and propagate with their traceback.

## Testing strategy

The suite has 413 pytest tests and runs in a few seconds.

- **Unit tests with fakes.** A fake socket, fake clock and sleep, and a scripted measurement function cover the client, scheduler, analysis and configuration. No emulators, network or real sleeping are involved. This is how communication errors, timeouts and invalid replies are simulated.
- **Real SQLite** files in temporary directories for the archive, queries and CLI. These tests cover:
  - round trips;
  - constraint violations inserted directly;
  - rollback forced after inserts have started, using a test trigger;
  - write-lock contention between two connections;
  - malformed and foreign databases;
  - export failures.
- **CLI tests** call `main()` in-process, and one test runs the module as a separate process.
- **A guard** (`tests/conftest.py`) fails any test that would open or write the shipped `results/` database.
- **End-to-end:** `main.py` and the sampling demo run against the real emulators. The demo saves each run, reloads it through a new connection and checks that it is unchanged.

## Key tradeoffs and limitations

- **Timing is best-effort.** The scheduler uses `time.perf_counter`, computes every target from the start time so lateness does not accumulate, and skips missed slots instead of catching up. Each attempt records its scheduled and actual start, so timing can be checked afterwards, but precision depends on the operating system and is not guaranteed. In the demo runs on the development Windows machine, start lateness was at most about 1.1 ms. The client's own timeout deadline uses `time.monotonic`.
- **One TCP connection per sample.** The emulators reply once and close the connection, so a response ends when the connection closes. This limits the practical sampling rate; the configured maximum is 1000 Hz.
- **Emulator limitations:**
  - The emulators generate independent random values with no known true current, so the statistics describe spread within a run, not accuracy.
  - The comparison shows signed differences only, without ranking devices or labelling results as pass or fail.
  - The emulators listen on IPv4 only (an IPv4 socket bound to `localhost`).
  - The demo waits a fixed 5 s for them to start, like `main.py`.
- **Numeric IPv4 addresses only (client policy).** Separately from what the emulators support, the client deliberately accepts only numeric IPv4 addresses, such as `127.0.0.1`. This avoids hostname resolution and connection attempts to several resolved addresses, so one connection attempt shares a single timeout budget with sending and receiving. Supporting hostnames or IPv6 would need resolution and a policy for dividing the budget between several addresses.
- **Bounded runs.** At most 1,000,000 potential attempts per run, because every attempt record is kept in memory, and `measurements_count` must fit a signed 64-bit integer so it can be archived.
- **Platforms.** Development and validation were done on Windows 10 with Python 3.11.3. The code uses only portable standard-library features plus PyYAML, and the README gives Linux/macOS commands, but those were not run on Linux or macOS.

## Bonus challenges

| Bonus | Status |
|---|---|
| Configuration-driven testing | Implemented: devices, sampling limits and archiving come from the validated `config/config.yaml`. |
| Error simulation | Only in the automated tests, through fakes (connection errors, timeouts, invalid replies). There is no runtime fault-injection mode. |
| Visualization | Not implemented; prioritized required functionality. |
| Performance consistency evaluation | Not implemented as a feature. Per-run standard deviation and per-attempt timing are recorded and can be compared, but no consistency score is computed. |
| Accuracy assessment | Not implemented. The emulators produce independent random values with no reference current, so relative accuracy cannot be established from their output. |
