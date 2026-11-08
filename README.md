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
  - `config.yaml`: Configuration file for the ammeter emulators.
- `examples/`
  - `run_tests.py`: super lyze example for run test **don't use it**.
- `src/`
  - `testing/`
    - `test_framework.py`: `AmmeterTestFramework` class to test the ammeter emulators (not yet implemented).
  - `utils/`
    - `config.py`: Configuration settings.
    - `logger.py`: Logging setup.
    - `Utils.py`: Utility functions, including `generate_random_float`.
- `tests/`
  - `test_client.py`: Unit tests for the client (fake socket and clock; no emulators needed).
- `pytest.ini`: pytest settings (test folder and import path).
- `requirements-dev.txt`: Development dependencies (pytest).

## Usage

From the project root, start the ammeter emulators and request one current measurement from each:
```sh
python main.py
```

`main.py` starts the three emulators in background threads, waits 5 seconds for them to start listening, then sends one request to each port and prints the measurement each one returns. If any request fails, it prints the error and exits with code 1.

`main.py` and the client use only the Python standard library; nothing needs to be installed to run it.

### Client behaviour

`request_current_from_ammeter(port, command, timeout=5.0, host="localhost", max_response_bytes=1024)` in `Ammeters/client.py`:

- Returns the measured current as a `float`.
- Reads until the emulator closes the connection (which the emulators do after replying), so a response split across several TCP reads is reassembled.
- `timeout` (seconds, must be positive and finite) applies to connecting and sending, and is also one overall deadline for receiving the whole response, however many partial reads that takes.
- Raises an `AmmeterCommunicationError` subclass that names the port, with the original exception kept as `__cause__`:
  - `AmmeterConnectionError`: the connection was refused, reset, or otherwise failed.
  - `AmmeterTimeoutError`: no complete response within the timeout.
  - `InvalidMeasurementError`: the response was empty (for example, an unrecognised command), larger than `max_response_bytes`, not UTF-8, not a number, or NaN/infinity.

It never returns `None` or a placeholder value for a failed measurement.

## Development Setup and Tests

The tests need `pytest`, installed into a project-local virtual environment (the `.venv/` folder is ignored by Git). The supplied `requirements.txt` is not needed for this.

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

The client tests replace the socket and the clock with fakes, so they run in well under a second and do not start the emulators or open network connections.

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