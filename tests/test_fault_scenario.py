"""Fault-simulation logic: scenario, identity, comparison, record and report (no sockets, no real time)."""
import json
import threading

import pytest

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
    Step,
    compare_scenario,
    is_fault_simulation,
    observe_attempt,
    outcome_matches,
    parse_device_name,
    scenario_record,
    validate_scenario_record,
)
from src.testing.fault_server import read_command
from src.testing.visualization import render_run_report
from test_visualization import make_run

DEVICE = "fault-sim:greenlee:basic-recovery:v1"
COMPLETE_CLEANUP = {"complete": True}

# Client-side outcomes the basic scenario expects, as run_test records them.
EXPECTED_ERRORS = {
    2: ("InvalidMeasurementError", "Ammeter on port 1: response is not a number: '12.3.4'"),
    3: ("InvalidMeasurementError", "Ammeter on port 1: empty response (command may not be recognised)"),
    4: ("AmmeterTimeoutError", "Ammeter on port 1: request did not complete within the 1.0 s timeout budget"),
}


def attempt(number, error=None):
    record = {"attempt": number, "ok": error is None}
    if error is None:
        record["current_a"] = 0.1 * number
    else:
        record["error"] = {"type": error[0], "message": error[1]}
    return record


def expected_attempts():
    return [attempt(n, EXPECTED_ERRORS.get(n)) for n in range(1, 6)]


def connection(number, fault, *, command=COMMAND_OK, reply_sent=None, delay_interrupted=None, error=None,
               finished=True, socket_closed=True):
    if reply_sent is None:
        reply_sent = fault in (FAULT_OK, FAULT_MALFORMED)
    return {"connection": number, "fault": fault, "command": command, "reply_sent": reply_sent,
            "delay_interrupted": delay_interrupted, "finished": finished, "socket_closed": socket_closed,
            "error": error}


def expected_connections():
    return [connection(n, step.fault, delay_interrupted=True if step.fault == FAULT_DELAY else None)
            for n, step in enumerate(BASIC_RECOVERY.steps, start=1)]


def compare(attempts=None, connections=None, errors=(), cleanup=COMPLETE_CLEANUP):
    return compare_scenario(BASIC_RECOVERY,
                            expected_attempts() if attempts is None else attempts,
                            expected_connections() if connections is None else connections,
                            list(errors), cleanup)


# --- Scenario and identity -----------------------------------------------------------------

def test_basic_scenario_order_and_expected_outcomes():
    steps = BASIC_RECOVERY.steps
    assert [s.fault for s in steps] == [FAULT_OK, FAULT_MALFORMED, FAULT_CLOSE, FAULT_DELAY, FAULT_OK]
    assert [s.expected.to_dict() for s in steps] == [
        {"outcome": "ok"},
        {"outcome": "error", "type": "InvalidMeasurementError", "message_contains": "response is not a number"},
        {"outcome": "error", "type": "InvalidMeasurementError", "message_contains": "empty response"},
        {"outcome": "error", "type": "AmmeterTimeoutError", "message_contains": None},
        {"outcome": "ok"},
    ]
    assert "not a TCP reset" in steps[2].description


def test_device_name_carries_emulator_scenario_and_version():
    assert BASIC_RECOVERY.device_name == DEVICE
    assert parse_device_name(DEVICE) == {"emulator": "greenlee", "scenario_id": "basic-recovery", "version": 1}


@pytest.mark.parametrize("name", ["fault-sim:greenlee", "fault-sim:greenlee:x:1", "fault-sim:::v1",
                                  "fault-sim:greenlee:x:v", "fault-sim:a:b:v1:extra"])
def test_incomplete_simulation_names_are_flagged_without_an_identity(name):
    assert parse_device_name(name) == {"emulator": None, "scenario_id": None, "version": None}
    assert is_fault_simulation({"ammeter": name})


@pytest.mark.parametrize("name", ["greenlee", "fault-simulator", "", None, 5, "FAULT-SIM:greenlee"])
def test_other_names_are_not_simulations(name):
    assert parse_device_name(name) is None
    assert not is_fault_simulation({"ammeter": name})


def test_unknown_fault_kinds_are_rejected():
    with pytest.raises(ValueError, match="unknown fault kind"):
        Step("reset", Expected(True), "x")


def test_scenario_description_is_json_ready():
    data = BASIC_RECOVERY.to_dict()
    assert data["id"] == "basic-recovery" and data["version"] == 1 and data["emulator"] == "greenlee"
    assert [s["connection"] for s in data["steps"]] == [1, 2, 3, 4, 5]
    json.dumps(data, allow_nan=False)


# --- Outcomes and comparison ---------------------------------------------------------------

def test_observe_attempt():
    assert observe_attempt(attempt(1)) == {"outcome": "ok"}
    assert observe_attempt(attempt(2, ("X", "msg"))) == {"outcome": "error", "type": "X", "message": "msg"}


@pytest.mark.parametrize("expected, observed, matches", [
    (Expected(True), {"outcome": "ok"}, True),
    (Expected(True), {"outcome": "error", "type": "X", "message": ""}, False),
    (Expected(False, "X", "abc"), {"outcome": "ok"}, False),
    (Expected(False, "X", "abc"), {"outcome": "error", "type": "X", "message": "..abc.."}, True),
    (Expected(False, "X", "abc"), {"outcome": "error", "type": "X", "message": "ab c"}, False),
    (Expected(False, "X", "abc"), {"outcome": "error", "type": "Y", "message": "abc"}, False),
    (Expected(False, "X", None), {"outcome": "error", "type": "X", "message": "anything"}, True),
])
def test_outcome_matches(expected, observed, matches):
    assert outcome_matches(expected, observed) is matches


def test_the_planned_outcomes_pass():
    result = compare()
    assert result["verdict"] == "pass" and result["recovered"] and result["problems"] == []
    assert [row["match"] for row in result["steps"]] == [True] * 5


@pytest.mark.parametrize("reply_sent, interrupted", [(False, True), (True, False), (False, False)])
def test_a_delayed_reply_may_or_may_not_be_sent(reply_sent, interrupted):
    connections = expected_connections()
    connections[3].update(reply_sent=reply_sent, delay_interrupted=interrupted)
    assert compare(connections=connections)["verdict"] == "pass"


def test_outcomes_in_the_wrong_order_fail():
    attempts = expected_attempts()
    attempts[1], attempts[2] = attempts[2], attempts[1]
    result = compare(attempts=attempts)
    assert result["verdict"] == "fail"
    assert [row["match"] for row in result["steps"]] == [True, False, False, True, True]


def test_a_missing_attempt_fails():
    result = compare(attempts=expected_attempts()[:4], connections=expected_connections()[:4])
    assert result["verdict"] == "fail"
    assert "step 5: no attempt was made" in result["problems"]
    assert "step 5: the server received no connection" in result["problems"]


def test_an_extra_attempt_and_connection_fail():
    attempts = expected_attempts() + [attempt(6)]
    connections = expected_connections() + [connection(6, "unplanned", reply_sent=False)]
    result = compare(attempts=attempts, connections=connections)
    assert result["verdict"] == "fail" and "step 6: not part of the scenario" in result["problems"]
    assert result["steps"][5]["fault"] is None and result["steps"][5]["expected"] is None


def test_a_connection_the_client_did_not_make_fails():
    connections = expected_connections() + [connection(6, "unplanned", command=COMMAND_INCOMPLETE, reply_sent=False)]
    result = compare(connections=connections)
    assert result["verdict"] == "fail" and "step 6: no attempt was made" in result["problems"]


@pytest.mark.parametrize("command", [COMMAND_WRONG, COMMAND_INCOMPLETE])
def test_a_bad_command_in_the_server_log_fails(command):
    connections = expected_connections()
    connections[0].update(command=command, reply_sent=False)
    result = compare(connections=connections)
    assert result["verdict"] == "fail" and f"step 1: command was {command}" in result["problems"]


def test_a_server_error_fails_even_if_the_client_outcomes_match():
    connections = expected_connections()
    connections[0]["error"] = {"type": "RuntimeError", "message": "sensor broke"}
    errors = [{"where": "connection 1", "type": "RuntimeError", "message": "sensor broke"}]
    result = compare(connections=connections, errors=errors)
    assert result["verdict"] == "fail"
    assert "step 1: server error: RuntimeError: sensor broke" in result["problems"]
    assert "server error (connection 1): RuntimeError: sensor broke" in result["problems"]


def test_an_accept_loop_error_alone_fails():
    result = compare(errors=[{"where": "accept loop", "type": "OSError", "message": "boom"}])
    assert result["verdict"] == "fail" and result["steps"][0]["match"]


@pytest.mark.parametrize("change", [{"finished": False}, {"socket_closed": False}])
def test_an_unfinished_handler_fails(change):
    connections = expected_connections()
    connections[4].update(change)
    assert compare(connections=connections)["verdict"] == "fail"


def test_incomplete_cleanup_fails():
    result = compare(cleanup={"complete": False})
    assert result["verdict"] == "fail" and "server cleanup did not complete" in result["problems"]


def test_a_reply_on_the_close_step_or_none_on_a_reply_step_fails():
    connections = expected_connections()
    connections[2]["reply_sent"] = True
    assert compare(connections=connections)["verdict"] == "fail"
    connections = expected_connections()
    connections[1]["reply_sent"] = False
    assert compare(connections=connections)["verdict"] == "fail"


def test_a_server_applying_a_different_fault_fails():
    connections = expected_connections()
    connections[1]["fault"] = FAULT_OK
    assert compare(connections=connections)["verdict"] == "fail"


def test_no_recovery_fails():
    attempts = expected_attempts()
    attempts[4] = attempt(5, EXPECTED_ERRORS[3])
    result = compare(attempts=attempts)
    assert not result["recovered"] and result["verdict"] == "fail"


# --- Scenario record --------------------------------------------------------------------------

def sim_run():
    run = make_run([0.1, None, None, None, 0.5], ammeter=DEVICE)
    run["run_id"] = "8d5c8f6e-0000-4000-8000-000000000001"
    return run


def record_for(run, comparison=None):
    return scenario_record(BASIC_RECOVERY, run, {"host": "127.0.0.1", "port": 1, "delay_s": 3.0},
                           expected_connections(), [], {"complete": True, "elapsed_s": 0.01},
                           comparison or compare())


def test_scenario_record_shape_and_strict_json():
    run = sim_run()
    record = record_for(run)
    assert record["record_type"] == "ammeter-fault-simulation" and record["format_version"] == 1
    assert record["run_id"] == run["run_id"] and record["device"] == DEVICE
    assert record["settings"]["sampling"] == run["settings"] and record["settings"]["server"]["delay_s"] == 3.0
    assert record["scenario"]["id"] == "basic-recovery" and record["verdict"] == "pass"
    assert len(record["connections"]) == 5 and record["cleanup"]["complete"]
    assert json.loads(json.dumps(record, allow_nan=False)) == record


def test_a_matching_record_is_accepted():
    run = sim_run()
    record = record_for(run)
    assert validate_scenario_record(record, run) is record


@pytest.mark.parametrize("change, message", [
    ({"run_id": "8d5c8f6e-0000-4000-8000-000000000002"}, "not for run"),
    ({"device": "fault-sim:greenlee:basic-recovery:v2"}, "device"),
    ({"record_type": "other"}, "not a fault-simulation scenario record"),
    ({"format_version": 2}, "format version"),
    ({"verdict": "maybe"}, "no valid comparison"),
    ({"comparison": None}, "no valid comparison"),
])
def test_a_record_for_another_run_or_of_another_kind_is_rejected(change, message):
    run = sim_run()
    record = {**record_for(run), **change}
    with pytest.raises(ValueError, match=message):
        validate_scenario_record(record, run)
    with pytest.raises(ValueError, match=message):
        render_run_report(run, scenario=record)


def test_a_record_that_is_not_a_mapping_is_rejected():
    with pytest.raises(ValueError):
        validate_scenario_record(["x"], sim_run())


# --- Report ------------------------------------------------------------------------------------

def test_ordinary_runs_keep_the_emulator_note():
    document = render_run_report(make_run([1.0]))
    assert "readings from the supplied ammeter emulators" in document
    assert "Fault-simulation" not in document and "<title>Ammeter run " in document


def test_a_simulation_run_without_a_record_shows_only_its_stored_identity():
    run = sim_run()
    document = render_run_report(run)
    assert "<title>Fault-simulation run " in document and "Fault-simulation run report:" in document
    assert "injected the failures on purpose" in document
    assert "scenario basic-recovery version 1, emulator greenlee" in document
    assert "readings from the supplied ammeter emulators" not in document
    assert "No scenario record was supplied" in document and f"{run['run_id']}.scenario.json" in document
    assert "Verdict" not in document and "MISMATCH" not in document


def test_a_simulation_run_with_an_unparsed_name_does_not_invent_an_identity():
    run = sim_run()
    run["ammeter"] = "fault-sim:custom"
    document = render_run_report(run)
    assert "does not identify" in document and "scenario None" not in document and "Verdict" not in document


def test_a_simulation_run_with_its_record_shows_the_comparison_and_verdict():
    run = sim_run()
    document = render_run_report(run, scenario=record_for(run))
    assert "<strong>Verdict: pass</strong>" in document
    assert document.count("<td>match</td>") == 5
    assert "InvalidMeasurementError (message contains &#x27;empty response&#x27;)" in document
    assert f"record for run {run['run_id']}" in document


def test_a_failing_record_lists_mismatches_and_problems():
    run = sim_run()
    comparison = compare(cleanup={"complete": False})
    document = render_run_report(run, scenario=record_for(run, comparison))
    assert "<strong>Verdict: fail</strong>" in document and "server cleanup did not complete" in document


def test_record_text_is_escaped():
    run = sim_run()
    record = record_for(run)
    record["scenario"]["description"] = "<script>alert(1)</script>"
    record["comparison"]["steps"][1]["observed"] = {"outcome": "error", "type": "<b>", "message": "a & b"}
    record["comparison"]["problems"] = ["<i>x</i>"]
    document = render_run_report(run, scenario=record)
    assert "<script>" not in document and "<b>" not in document and "<i>x" not in document
    assert "&lt;script&gt;" in document and "a &amp; b" in document


# --- Reading the command across partial reads (fake socket, fake clock) ----------------------

class ChunkSocket:
    """Returns scripted chunks from recv; "TIMEOUT" raises socket.timeout, b"" is end-of-stream."""

    def __init__(self, chunks, clock=None, step=0.0):
        self.chunks = list(chunks)
        self.requested = []
        self.clock = clock
        self.step = step

    def settimeout(self, value):
        pass

    def recv(self, size):
        import socket
        self.requested.append(size)
        if self.clock is not None:
            self.clock.now += self.step
        chunk = self.chunks.pop(0)
        if chunk == "TIMEOUT":
            raise socket.timeout("timed out")
        assert len(chunk) <= size, "the server must never be handed more than it asked for"
        return chunk


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_command_is_assembled_from_partial_reads():
    sock = ChunkSocket([b"MEAS", "TIMEOUT", b"URE_", b"X"])
    status, data = read_command(sock, b"MEASURE_X", 10.0, threading.Event(), clock=FakeClock())
    assert (status, data) == (COMMAND_OK, b"MEASURE_X")
    assert sock.requested == [9, 5, 5, 1]  # never more than the bytes still missing


def test_bytes_that_are_not_a_prefix_end_the_read_at_once():
    sock = ChunkSocket([b"MEX", b"never read"])
    assert read_command(sock, b"MEASURE_X", 10.0, threading.Event(), clock=FakeClock()) == (COMMAND_WRONG, b"MEX")
    assert len(sock.chunks) == 1


def test_end_of_stream_before_the_full_command_is_incomplete():
    sock = ChunkSocket([b"MEAS", b""])
    assert read_command(sock, b"MEASURE_X", 10.0, threading.Event(), clock=FakeClock()) == \
        (COMMAND_INCOMPLETE, b"MEAS")


def test_an_idle_client_is_incomplete_when_the_deadline_passes():
    clock = FakeClock()
    sock = ChunkSocket(["TIMEOUT"] * 100, clock=clock, step=0.5)
    assert read_command(sock, b"MEASURE_X", 2.0, threading.Event(), clock=clock) == (COMMAND_INCOMPLETE, b"")
    assert len(sock.requested) == 4


def test_the_stop_event_ends_the_read():
    stop = threading.Event()
    stop.set()
    sock = ChunkSocket([b"MEASURE_X"])
    assert read_command(sock, b"MEASURE_X", 10.0, stop, clock=FakeClock()) == (COMMAND_INCOMPLETE, b"")
    assert sock.requested == []


# --- Committed samples --------------------------------------------------------------------------

def test_committed_simulation_samples_are_consistent_and_reproducible():
    from src.utils.config import PROJECT_ROOT

    samples = PROJECT_ROOT / "sample_results"
    run = json.loads((samples / "fault_simulation_run.json").read_text(encoding="utf-8"))
    record = json.loads((samples / "fault_simulation_scenario.json").read_text(encoding="utf-8"))
    assert run["ammeter"] == DEVICE and is_fault_simulation(run)
    assert validate_scenario_record(record, run)["verdict"] == "pass"
    assert record["cleanup"]["complete"] and record["comparison"]["recovered"]
    assert [a["ok"] for a in run["attempts"]] == [True, False, False, False, True]
    # The verdict is recomputed from the committed data, not just trusted.
    recomputed = compare_scenario(BASIC_RECOVERY, run["attempts"], record["connections"],
                                  record["server_errors"], record["cleanup"])
    assert recomputed == record["comparison"]
    expected_html = (samples / "fault_simulation_report.html").read_text(encoding="utf-8")
    assert render_run_report(run, scenario=record) == expected_html
