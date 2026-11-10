"""Fault server and demo over real loopback TCP (ports chosen by the operating system).

Timing is used only with wide margins: client timeouts far below the server's delay, and
generous waits for things that normally take milliseconds. Nothing is retried, and no test
asserts how long anything took.
"""
import json
import socket
import threading

import pytest

from Ammeters.client import (
    AmmeterTimeoutError,
    InvalidMeasurementError,
    request_current_from_ammeter,
)
from examples import fault_simulation as demo
from src.testing.archive import ExportError, ResultArchive
from src.testing.fault_scenario import (
    BASIC_RECOVERY,
    COMMAND_INCOMPLETE,
    COMMAND_OK,
    COMMAND_WRONG,
    FAULT_CLOSE,
    FAULT_DELAY,
    FAULT_MALFORMED,
    FAULT_OK,
    Expected,
    Scenario,
    Step,
    compare_scenario,
)
from src.testing.fault_server import FaultServerError, FaultSimulationServer

OK = Expected(True)
LONG_DELAY_S = 30.0       # never waited for in full: cleanup interrupts it
GENEROUS_TIMEOUT_S = 5.0  # for requests that are answered at once


class FixedEmulator:
    get_current_command = b"MEASURE_TEST -get"

    def __init__(self, value=1.25):
        self.value = value

    def measure_current(self):
        return self.value


class BrokenEmulator(FixedEmulator):
    def measure_current(self):
        raise RuntimeError("sensor broke")


def steps(*faults):
    return [Step(fault, OK, fault) for fault in faults]


def server(*faults, emulator=None, **options):
    options.setdefault("delay_s", LONG_DELAY_S)
    return FaultSimulationServer(steps(*faults), emulator=emulator or FixedEmulator(), **options)


def request(srv, timeout=GENEROUS_TIMEOUT_S):
    return request_current_from_ammeter(srv.port, srv.command, timeout=timeout)


def read_until_closed(sock):
    data = b""
    while True:
        chunk = sock.recv(256)
        if not chunk:
            return data
        data += chunk


def raw_client(srv):
    sock = socket.create_connection(("127.0.0.1", srv.port), timeout=GENEROUS_TIMEOUT_S)
    sock.settimeout(GENEROUS_TIMEOUT_S)
    return sock


def assert_cleaned_up(srv):
    cleanup = srv.cleanup
    assert cleanup["complete"], cleanup
    assert cleanup["accept_thread_stopped"] and cleanup["listener_closed"]
    assert cleanup["handlers_stopped"] == cleanup["handlers"]
    assert cleanup["connection_sockets_closed"] == cleanup["connection_sockets"]
    assert all(c["finished"] and c["socket_closed"] for c in srv.connections())
    assert all(not t.name.startswith("fault-sim") for t in threading.enumerate())


# --- Each fault through the real client ---------------------------------------------------

def test_each_fault_reaches_the_real_client_in_connection_order():
    with server(FAULT_OK, FAULT_MALFORMED, FAULT_CLOSE, FAULT_OK) as srv:
        assert srv.port not in (0, 5001, 5002, 5003)
        assert request(srv) == 1.25
        with pytest.raises(InvalidMeasurementError, match=r"response is not a number: '12\.3\.4'"):
            request(srv)
        with pytest.raises(InvalidMeasurementError, match="empty response"):
            request(srv)
        assert request(srv) == 1.25
    log = srv.connections()
    assert [c["connection"] for c in log] == [1, 2, 3, 4]  # readiness used no connection
    assert [c["fault"] for c in log] == [FAULT_OK, FAULT_MALFORMED, FAULT_CLOSE, FAULT_OK]
    assert [c["reply_sent"] for c in log] == [True, True, False, True]
    assert all(c["command"] == COMMAND_OK and c["error"] is None for c in log)
    assert srv.errors() == []
    assert_cleaned_up(srv)


def test_a_delayed_reply_times_out_without_blocking_the_next_request():
    with server(FAULT_DELAY, FAULT_OK) as srv:
        with pytest.raises(AmmeterTimeoutError):
            request(srv, timeout=0.3)
        assert request(srv) == 1.25  # answered while connection 1 is still being delayed
        assert srv.connections()[0]["finished"] is False
    delayed = srv.connections()[0]
    assert delayed["delay_interrupted"] is True and delayed["reply_sent"] is False
    assert_cleaned_up(srv)


def test_the_default_emulator_is_greenlee_and_its_readings_are_used():
    with FaultSimulationServer(steps(FAULT_OK), delay_s=LONG_DELAY_S) as srv:
        assert srv.command == b"MEASURE_GREENLEE -get_measurement"
        value = request(srv)
    assert 0.01 <= value <= 100.0  # Greenlee: 1-10 V over 0.1-100 ohm
    assert_cleaned_up(srv)


def test_connections_beyond_the_scenario_are_unplanned_and_get_no_reply():
    with server(FAULT_OK) as srv:
        request(srv)
        with pytest.raises(InvalidMeasurementError, match="empty response"):
            request(srv)
    assert [c["fault"] for c in srv.connections()] == [FAULT_OK, "unplanned"]
    assert_cleaned_up(srv)


# --- Command reading over real sockets ------------------------------------------------------

def test_a_command_sent_in_pieces_is_assembled():
    with server(FAULT_OK) as srv:
        with raw_client(srv) as sock:
            sock.sendall(srv.command[:5])
            threading.Event().wait(0.2)  # makes separate segments likely; correctness does not depend on it
            sock.sendall(srv.command[5:])
            assert float(read_until_closed(sock)) == 1.25
    assert srv.connections()[0]["command"] == COMMAND_OK
    assert_cleaned_up(srv)


def test_a_wrong_command_is_closed_without_a_reply():
    with server(FAULT_OK) as srv:
        with raw_client(srv) as sock:
            sock.sendall(b"MEASURE_WRONG")
            try:
                assert read_until_closed(sock) == b""
            except ConnectionResetError:
                pass  # unread bytes at close may turn the close into a reset; either way, no reply
    entry = srv.connections()[0]
    assert entry["command"] == COMMAND_WRONG and entry["reply_sent"] is False
    assert_cleaned_up(srv)


def test_a_partial_command_is_abandoned_at_the_read_deadline():
    with server(FAULT_OK, command_timeout_s=0.3) as srv:
        with raw_client(srv) as sock:
            sock.sendall(srv.command[:5])
            assert read_until_closed(sock) == b""  # the server gives up and closes
    entry = srv.connections()[0]
    assert entry["command"] == COMMAND_INCOMPLETE and entry["reply_sent"] is False
    assert_cleaned_up(srv)


def test_an_idle_client_does_not_hold_up_cleanup():
    with server(FAULT_OK, command_timeout_s=LONG_DELAY_S) as srv:
        sock = raw_client(srv)
        accepted = threading.Event()
        for _ in range(100):  # wait (at most 5 s) until the server has accepted it
            if srv.connections():
                accepted.set()
                break
            threading.Event().wait(0.05)
        assert accepted.is_set()
    sock.close()
    assert srv.connections()[0]["command"] == COMMAND_INCOMPLETE
    assert srv.cleanup["elapsed_s"] < LONG_DELAY_S
    assert_cleaned_up(srv)


# --- Errors and lifecycle -----------------------------------------------------------------------

def test_a_handler_error_is_recorded_and_the_connection_closed():
    with server(FAULT_OK, emulator=BrokenEmulator()) as srv:
        with pytest.raises(InvalidMeasurementError, match="empty response"):
            request(srv)
    entry = srv.connections()[0]
    assert entry["error"] == {"type": "RuntimeError", "message": "sensor broke"}
    assert srv.errors() == [{"where": "connection 1", "type": "RuntimeError", "message": "sensor broke"}]
    assert_cleaned_up(srv)


def test_an_exception_inside_the_context_still_cleans_up():
    srv = server(FAULT_DELAY)
    with pytest.raises(KeyError):
        with srv:
            with pytest.raises(AmmeterTimeoutError):
                request(srv, timeout=0.3)
            raise KeyError("test")
    assert_cleaned_up(srv)
    assert srv.connections()[0]["delay_interrupted"] is True


def test_a_port_in_use_fails_startup_and_cleans_up():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        srv = server(FAULT_OK, port=occupied.getsockname()[1])
        with pytest.raises(FaultServerError, match="could not start"):
            with srv:
                pytest.fail("the body must not run")
    assert_cleaned_up(srv)


def test_an_accept_loop_that_never_becomes_ready_fails_startup_and_cleans_up():
    class NeverReady(FaultSimulationServer):
        def _accept_loop(self):
            return  # exits without signalling readiness

    srv = NeverReady(steps(FAULT_OK), delay_s=LONG_DELAY_S, emulator=FixedEmulator(), ready_timeout_s=0.5)
    with pytest.raises(FaultServerError, match="did not start"):
        srv.start()
    assert_cleaned_up(srv)


def test_a_server_starts_only_once_and_close_is_idempotent():
    srv = server(FAULT_OK)
    with srv:
        pass
    with pytest.raises(FaultServerError, match="only be started once"):
        srv.start()
    assert srv.close() is srv.cleanup


# --- The demo -------------------------------------------------------------------------------------

def test_demo_runs_the_scenario_with_relative_paths_resolved_against_the_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert demo.main(["--db", "sim.sqlite3", "--output-dir", "out"]) == 0
    printed = capsys.readouterr().out
    assert "scenario verdict: pass" in printed and "RESULT: the planned faults occurred in order" in printed

    [run] = ResultArchive(tmp_path / "sim.sqlite3").list_runs()
    assert run["ammeter"] == BASIC_RECOVERY.device_name
    assert run["counts"]["attempted"] == 5 and run["counts"]["succeeded"] == 2 and run["counts"]["failed"] == 3
    stored = ResultArchive(tmp_path / "sim.sqlite3").load_run(run["run_id"])

    record = json.loads((tmp_path / "out" / f"{run['run_id']}.scenario.json").read_text(encoding="utf-8"))
    assert record["run_id"] == run["run_id"] and record["device"] == run["ammeter"]
    assert record["verdict"] == "pass" and record["cleanup"]["complete"]
    assert record["settings"]["sampling"] == stored["settings"]
    assert record["settings"]["server"]["port"] == stored["port"]
    assert [c["connection"] for c in record["connections"]] == [1, 2, 3, 4, 5]
    assert [a["ok"] for a in stored["attempts"]] == [True, False, False, False, True]
    html = (tmp_path / "out" / f"{run['run_id']}.html").read_text(encoding="utf-8")
    assert "<strong>Verdict: pass</strong>" in html and "Fault-simulation run report" in html


def test_demo_exits_1_when_the_outcomes_differ_from_the_plan_but_keeps_the_run(tmp_path, monkeypatch, capsys):
    wrong = Scenario(BASIC_RECOVERY.scenario_id, BASIC_RECOVERY.version, BASIC_RECOVERY.emulator, "wrong plan",
                     tuple(Step(s.fault, OK if n == 3 else s.expected, s.description)
                           for n, s in enumerate(BASIC_RECOVERY.steps, start=1)))
    monkeypatch.setattr(demo, "BASIC_RECOVERY", wrong)
    assert demo.main(["--db", str(tmp_path / "sim.sqlite3"), "--output-dir", str(tmp_path)]) == 1
    printed = capsys.readouterr().out
    assert "step 3: client outcome differs from the expected outcome" in printed and "NOT as expected" in printed
    [run] = ResultArchive(tmp_path / "sim.sqlite3").list_runs()
    record = json.loads((tmp_path / f"{run['run_id']}.scenario.json").read_text(encoding="utf-8"))
    assert record["verdict"] == "fail"


def test_demo_reports_the_run_and_written_files_when_an_output_cannot_be_written(tmp_path, monkeypatch, capsys):
    real_write = demo.write_new_file

    def fail_html(path, data):
        if path.suffix == ".html":
            raise ExportError(f"disk full: {path}")
        return real_write(path, data)

    monkeypatch.setattr(demo, "write_new_file", fail_html)
    assert demo.main(["--db", str(tmp_path / "sim.sqlite3"), "--output-dir", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    [run] = ResultArchive(tmp_path / "sim.sqlite3").list_runs()  # the run stays saved
    assert f"run {run['run_id']} (saved in the archive)" in error and "disk full" in error
    assert f"{run['run_id']}.scenario.json" in error
    assert (tmp_path / f"{run['run_id']}.scenario.json").exists()
    assert not (tmp_path / f"{run['run_id']}.html").exists()


def test_demo_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        demo.main(["--unknown"])
    assert exc.value.code == 2


# --- The delayed reply: emulator errors versus a client that has gone -----------------------------

class FailsOnFirstReading(FixedEmulator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def measure_current(self):
        self.calls += 1
        if self.calls == 1:
            raise OSError("sensor read failed")
        return self.value


class SignalsReading(FixedEmulator):
    def __init__(self):
        super().__init__()
        self.measured = threading.Event()

    def measure_current(self):
        self.measured.set()
        return self.value


def test_an_emulator_oserror_in_the_delayed_step_is_recorded_and_fails_the_comparison():
    # The client outcome is planned to match exactly, so only the server error can fail it.
    scenario = Scenario("delay-error", 1, "test", "regression",
                        (Step(FAULT_DELAY, Expected(False, "InvalidMeasurementError", "empty response"), "delay"),
                         Step(FAULT_OK, OK, "recovery")))
    with FaultSimulationServer(scenario.steps, delay_s=0.2, emulator=FailsOnFirstReading()) as srv:
        # The request returns only once the handler has closed the connection, i.e. after the error.
        with pytest.raises(InvalidMeasurementError, match="empty response") as first:
            request(srv)
        assert request(srv) == 1.25
    entry = srv.connections()[0]
    assert entry["delay_interrupted"] is False and entry["reply_sent"] is False
    assert entry["error"] == {"type": "OSError", "message": "sensor read failed"}
    assert srv.errors() == [{"where": "connection 1", "type": "OSError", "message": "sensor read failed"}]
    assert_cleaned_up(srv)

    attempts = [{"attempt": 1, "ok": False, "error": {"type": type(first.value).__name__, "message": str(first.value)}},
                {"attempt": 2, "ok": True, "current_a": 1.25}]
    result = compare_scenario(scenario, attempts, srv.connections(), srv.errors(), srv.cleanup)
    assert result["verdict"] == "fail" and result["recovered"]
    assert result["problems"] == ["step 1: server error: OSError: sensor read failed",
                                  "server error (connection 1): OSError: sensor read failed"]


def test_a_late_reply_to_a_client_that_gave_up_is_not_an_error():
    emulator = SignalsReading()
    with server(FAULT_DELAY, emulator=emulator, delay_s=1.5) as srv:
        with pytest.raises(AmmeterTimeoutError):
            request(srv, timeout=0.1)
        assert emulator.measured.wait(GENEROUS_TIMEOUT_S)  # the delay ran out and the reading was taken
    entry = srv.connections()[0]
    assert entry["delay_interrupted"] is False and entry["error"] is None and srv.errors() == []
    assert_cleaned_up(srv)  # the reply may or may not have been sent; either is expected


class FakeConnection:
    """Delivers the command, then fails ``sendall`` with ``send_error``."""

    def __init__(self, command, send_error):
        self.pending = command
        self.send_error = send_error
        self.closed = False

    def settimeout(self, value):
        pass

    def recv(self, size):
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def sendall(self, data):
        raise self.send_error

    def close(self):
        self.closed = True

    def fileno(self):
        return -1 if self.closed else 3


def handle_delayed(send_error):
    """Run one delayed-step handler directly (no threads, no network) with a failing send."""
    srv = FaultSimulationServer(steps(FAULT_DELAY), delay_s=0.01, emulator=FixedEmulator())
    entry = {"connection": 1, "fault": FAULT_DELAY, "command": None, "reply_sent": False,
             "delay_interrupted": None, "finished": False, "socket_closed": False, "error": None}
    srv._handle(FakeConnection(srv.command, send_error), entry, srv.steps[0])
    return srv, entry


@pytest.mark.parametrize("error", [ConnectionResetError("reset"), ConnectionAbortedError("aborted"),
                                   BrokenPipeError("broken pipe")])
def test_a_disconnected_client_during_the_late_reply_is_expected(error):
    srv, entry = handle_delayed(error)
    assert entry["error"] is None and srv.errors() == []
    assert entry["delay_interrupted"] is False and entry["reply_sent"] is False
    assert entry["finished"] and entry["socket_closed"]


@pytest.mark.parametrize("error", [socket.timeout("write timed out"), OSError("network is down")])
def test_other_send_errors_during_the_late_reply_are_recorded(error):
    srv, entry = handle_delayed(error)
    assert entry["error"] == {"type": type(error).__name__, "message": str(error)}
    assert len(srv.errors()) == 1
    assert entry["finished"] and entry["socket_closed"]
