# Ammeter Emulators

This project provides emulators for different types of ammeters: Greenlee, ENTES, and CIRCUTOR. Each ammeter emulator runs on a separate thread and can respond to current measurement requests.

## Project Structure

- `main.py`: Main script (project root) to start the ammeter emulators and request current measurements.
- `Ammeters/`
  - `Circutor_Ammeter.py`: Emulator for the CIRCUTOR ammeter.
  - `Entes_Ammeter.py`: Emulator for the ENTES ammeter.
  - `Greenlee_Ammeter.py`: Emulator for the Greenlee ammeter.
  - `base_ammeter.py`: Base class for all ammeter emulators.
  - `client.py`: Client to request current measurements from the ammeter emulators.
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

## Usage

From the project root, start the ammeter emulators and request one current measurement from each:
```sh
python main.py
```

`main.py` starts the three emulators in background threads, waits 5 seconds for them to start listening, then sends one request to each port and prints the measurement each one returns.

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