"""Supplied emulators and the shared startup helper (no fixed ports are used)."""
import io
import os
import socket
import sys
import threading

import pytest

import main
from Ammeters import base_ammeter
from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from examples import run_tests


# --- Emulator output -------------------------------------------------------------------

@pytest.mark.parametrize("emulator_class", [GreenleeAmmeter, EntesAmmeter, CircutorAmmeter])
def test_measurement_output_can_be_written_to_a_strict_cp1252_stream(monkeypatch, emulator_class):
    # Redirected stdout on Windows may use cp1252, which has no ohm sign; printing one
    # used to crash the Greenlee emulator thread.
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    current = emulator_class(port=0).measure_current()

    stream.flush()
    assert isinstance(current, float)
    assert raw.getvalue().decode("ascii")  # the diagnostic line is plain ASCII


# --- Port reuse ----------------------------------------------------------------------------

class StopServer(Exception):
    pass


class RecordingSocket:
    """Records the calls start_server makes, then ends its loop at accept()."""

    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def listen(self):
        self.calls.append(("listen",))

    def accept(self):
        raise StopServer


@pytest.mark.parametrize("reuse", [True, False])
def test_so_reuseaddr_is_set_before_bind_only_where_enabled(monkeypatch, reuse):
    calls = []
    monkeypatch.setattr(base_ammeter, "REUSE_ADDRESS", reuse)
    monkeypatch.setattr(base_ammeter.socket, "socket", lambda family, kind: RecordingSocket(calls))

    with pytest.raises(StopServer):
        GreenleeAmmeter(5001).start_server()

    expected = [("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)] if reuse else []
    assert calls == expected + [("bind", ("localhost", 5001)), ("listen",)]


def test_address_reuse_is_enabled_on_posix_only():
    assert base_ammeter.REUSE_ADDRESS is (os.name == "posix")


# --- Startup helper and liveness check ---------------------------------------------------

def no_sleep(seconds):
    pass


def test_running_emulators_are_not_reported_as_stopped():
    release = threading.Event()
    threads = main.start_emulators([("A", release.wait), ("B", release.wait)], delay_s=0, sleep=no_sleep)
    try:
        assert main.stopped_emulators(threads) == []
        assert all(thread.daemon for thread in threads.values())
    finally:
        release.set()


def test_an_emulator_whose_port_is_in_use_is_reported_as_stopped(monkeypatch):
    errors = []
    monkeypatch.setattr(threading, "excepthook", errors.append)  # capture instead of printing
    release = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        threads = main.start_emulators(
            [("Greenlee", GreenleeAmmeter(port).start_server), ("ENTES", release.wait)], delay_s=0, sleep=no_sleep)
        threads["Greenlee"].join(timeout=10)

        assert main.stopped_emulators(threads) == ["Greenlee"]
    release.set()
    assert len(errors) == 1 and issubclass(errors[0].exc_type, OSError)  # bind failed: address in use


def test_main_exits_with_1_before_measuring_if_an_emulator_stopped(monkeypatch, capsys):
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(main, "start_emulators", lambda: {"Greenlee": dead})
    monkeypatch.setattr(main, "request_current_from_ammeter",
                        lambda *args, **kwargs: pytest.fail("no request may be sent"))

    assert main.main() == 1
    assert "emulator(s) failed to start: Greenlee" in capsys.readouterr().err


def test_demo_exits_with_1_before_sampling_if_an_emulator_stopped(monkeypatch, capsys):
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(run_tests, "start_emulators", lambda: {"ENTES": dead})
    monkeypatch.setattr(run_tests.AmmeterTestFramework, "run_test",
                        lambda self, name: pytest.fail("no run may start"))

    assert run_tests.main() == 1
    assert "emulator(s) failed to start: ENTES" in capsys.readouterr().err
