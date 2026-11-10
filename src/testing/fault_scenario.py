"""Fault-simulation scenarios: definition, identity, comparison and the scenario record.

This module is pure logic (no sockets, threads or clocks), so it is tested deterministically.
The server that injects the faults is ``src/testing/fault_server.py``, and the demo that
runs a scenario is ``examples/fault_simulation.py``.

Scenario and connection numbers
-------------------------------
A scenario is an ordered list of steps. The fault server numbers incoming TCP connections
1, 2, 3, ... in the order it accepts them, and connection *n* gets step *n*. The client makes
exactly one connection per sampling attempt, so attempt *n* is expected to meet step *n*.
The order of outcomes is therefore repeatable; the current values (the Greenlee emulator's
random numbers) and all timings are not.

Identity without a schema change
--------------------------------
The archived device name carries the emulator, scenario ID and version, for example
``fault-sim:greenlee:basic-recovery:v1``. That identifies which scenario a run used; it
does not by itself identify one particular run. The link to a run is the scenario record
(``<run_id>.scenario.json``), which contains the exact ``run_id`` and device name, and is
checked against the run before it is used (``validate_scenario_record``).
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

SIMULATION_PREFIX = "fault-sim"
RECORD_TYPE = "ammeter-fault-simulation"
RECORD_FORMAT_VERSION = 1

# Fault kinds the server can apply to one connection.
FAULT_OK = "ok"                                # reply with a real emulator reading
FAULT_MALFORMED = "malformed"                  # reply with text that is not a number
FAULT_CLOSE = "close_without_response"         # close the connection cleanly without replying
FAULT_DELAY = "delay"                          # wait longer than the client's timeout before replying
FAULT_KINDS = (FAULT_OK, FAULT_MALFORMED, FAULT_CLOSE, FAULT_DELAY)

MALFORMED_REPLY = b"12.3.4"

# Command handling recorded by the server for each connection.
COMMAND_OK = "ok"
COMMAND_WRONG = "wrong"            # the bytes received differ from the expected command
COMMAND_INCOMPLETE = "incomplete"  # the client closed, or the read deadline or cleanup came first

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"


@dataclass(frozen=True)
class Expected:
    """Expected client-side outcome of one attempt: success, or an error type and message phrase."""

    ok: bool
    error_type: Optional[str] = None
    message_contains: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        if self.ok:
            return {"outcome": "ok"}
        return {"outcome": "error", "type": self.error_type, "message_contains": self.message_contains}


@dataclass(frozen=True)
class Step:
    fault: str
    expected: Expected
    description: str

    def __post_init__(self):
        if self.fault not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {self.fault!r}")


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    version: int
    emulator: str
    description: str
    steps: Tuple[Step, ...]

    @property
    def device_name(self) -> str:
        """The archived device identifier, e.g. ``fault-sim:greenlee:basic-recovery:v1``."""
        return f"{SIMULATION_PREFIX}:{self.emulator}:{self.scenario_id}:v{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.scenario_id,
            "version": self.version,
            "emulator": self.emulator,
            "description": self.description,
            "steps": [{"connection": n, "fault": s.fault, "description": s.description,
                       "expected": s.expected.to_dict()} for n, s in enumerate(self.steps, start=1)],
        }


_OK = Expected(ok=True)

BASIC_RECOVERY = Scenario(
    scenario_id="basic-recovery",
    version=1,
    emulator="greenlee",
    description=("One success, three different faults, then a success that shows the client and the "
                 "sampling loop recover without retries."),
    steps=(
        Step(FAULT_OK, _OK, "real Greenlee reading"),
        Step(FAULT_MALFORMED, Expected(False, "InvalidMeasurementError", "response is not a number"),
             f"reply {MALFORMED_REPLY.decode('ascii')!r}, which is not a number"),
        Step(FAULT_CLOSE, Expected(False, "InvalidMeasurementError", "empty response"),
             "orderly close (FIN) after reading the command, without a reply; not a TCP reset"),
        Step(FAULT_DELAY, Expected(False, "AmmeterTimeoutError", None),
             "hold the reply longer than the client's timeout"),
        Step(FAULT_OK, _OK, "real Greenlee reading (recovery)"),
    ),
)


# --- Identity -----------------------------------------------------------------------------

def parse_device_name(name: Any) -> Optional[Dict[str, Any]]:
    """``{"emulator", "scenario_id", "version"}`` for a fault-simulation device name, else None.

    A name that starts with ``fault-sim:`` but does not have the full form gives
    ``{"emulator": None, "scenario_id": None, "version": None}``.
    """
    if not isinstance(name, str) or not name.startswith(SIMULATION_PREFIX + ":"):
        return None
    parts = name.split(":")
    if len(parts) == 4 and all(parts[1:3]) and parts[3].startswith("v") and parts[3][1:].isdigit():
        return {"emulator": parts[1], "scenario_id": parts[2], "version": int(parts[3][1:])}
    return {"emulator": None, "scenario_id": None, "version": None}


def is_fault_simulation(run: Dict[str, Any]) -> bool:
    """True if the run's device name marks it as a fault-simulation run."""
    return parse_device_name(run.get("ammeter")) is not None


# --- Comparison ---------------------------------------------------------------------------

def observe_attempt(attempt: Dict[str, Any]) -> Dict[str, Any]:
    """The client-side outcome of one attempt record (as stored by run_test)."""
    if attempt["ok"]:
        return {"outcome": "ok"}
    return {"outcome": "error", "type": attempt["error"]["type"], "message": attempt["error"]["message"]}


def outcome_matches(expected: Expected, observed: Dict[str, Any]) -> bool:
    if expected.ok:
        return observed["outcome"] == "ok"
    if observed["outcome"] != "error" or observed["type"] != expected.error_type:
        return False
    return expected.message_contains is None or expected.message_contains in observed["message"]


def _server_problems(step: Step, connection: Dict[str, Any]) -> List[str]:
    """What the server log shows went wrong with one planned connection (empty if nothing)."""
    problems = []
    if connection["fault"] != step.fault:
        problems.append(f"server applied {connection['fault']!r}, planned {step.fault!r}")
    if connection["command"] != COMMAND_OK:
        problems.append(f"command was {connection['command']}")
    if connection.get("error"):
        problems.append(f"server error: {connection['error']['type']}: {connection['error']['message']}")
    if not connection["finished"] or not connection["socket_closed"]:
        problems.append("handler did not finish and close its socket")
    # A delayed reply may or may not be sent (the client has given up, or cleanup interrupted
    # the delay), so it is not checked. The other faults must have done exactly what was planned.
    if connection["command"] == COMMAND_OK and step.fault in (FAULT_OK, FAULT_MALFORMED) \
            and not connection["reply_sent"]:
        problems.append("reply was not sent")
    if step.fault == FAULT_CLOSE and connection["reply_sent"]:
        problems.append("a reply was sent although the step closes without one")
    return problems


def compare_scenario(scenario: Scenario, attempts: Sequence[Dict[str, Any]],
                     connections: Sequence[Dict[str, Any]], server_errors: Sequence[Dict[str, Any]],
                     cleanup: Dict[str, Any]) -> Dict[str, Any]:
    """Compare what was expected with what the client and the server recorded.

    Returns one row per planned step and per extra attempt or connection, a ``recovered``
    flag, a list of ``problems`` and the ``verdict``: "pass" only if every planned step
    matched on both sides, there were no extra attempts or connections, no server errors,
    cleanup completed, and the final success came after at least one failure.
    """
    rows = []
    problems: List[str] = []
    total = max(len(scenario.steps), len(attempts), len(connections))
    for index in range(total):
        number = index + 1
        step = scenario.steps[index] if index < len(scenario.steps) else None
        attempt = attempts[index] if index < len(attempts) else None
        connection = connections[index] if index < len(connections) else None
        observed = observe_attempt(attempt) if attempt is not None else None
        row_problems = []
        if step is None:
            row_problems.append("not part of the scenario")
        if attempt is None:
            row_problems.append("no attempt was made")
        if connection is None:
            row_problems.append("the server received no connection")
        if step is not None and observed is not None and not outcome_matches(step.expected, observed):
            row_problems.append("client outcome differs from the expected outcome")
        if step is not None and connection is not None:
            row_problems.extend(_server_problems(step, connection))
        rows.append({
            "step": number,
            "fault": step.fault if step else None,
            "expected": step.expected.to_dict() if step else None,
            "observed": observed,
            "server": None if connection is None else {
                key: connection[key] for key in ("fault", "command", "reply_sent", "delay_interrupted")},
            "match": not row_problems,
            "problems": row_problems,
        })
        problems.extend(f"step {number}: {p}" for p in row_problems)

    for error in server_errors:
        problems.append(f"server error ({error['where']}): {error['type']}: {error['message']}")
    if not cleanup.get("complete"):
        problems.append("server cleanup did not complete")

    observed_outcomes = [observe_attempt(a)["outcome"] for a in attempts]
    recovered = (len(observed_outcomes) >= 2 and observed_outcomes[-1] == "ok"
                 and "error" in observed_outcomes[:-1])
    if not recovered:
        problems.append("no successful attempt after a failed one (no recovery)")
    return {"steps": rows, "recovered": recovered, "problems": problems,
            "verdict": VERDICT_PASS if not problems else VERDICT_FAIL}


# --- Scenario record (the sidecar file) ----------------------------------------------------

def scenario_record(scenario: Scenario, run: Dict[str, Any], server_settings: Dict[str, Any],
                    connections: Sequence[Dict[str, Any]], server_errors: Sequence[Dict[str, Any]],
                    cleanup: Dict[str, Any], comparison: Dict[str, Any]) -> Dict[str, Any]:
    """The JSON-compatible record written next to the run as ``<run_id>.scenario.json``."""
    return {
        "record_type": RECORD_TYPE,
        "format_version": RECORD_FORMAT_VERSION,
        "run_id": run["run_id"],
        "device": run["ammeter"],
        "scenario": scenario.to_dict(),
        "settings": {"sampling": run["settings"], "server": dict(server_settings)},
        "connections": [dict(c) for c in connections],
        "server_errors": [dict(e) for e in server_errors],
        "cleanup": dict(cleanup),
        "comparison": comparison,
        "verdict": comparison["verdict"],
    }


def validate_scenario_record(record: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``record`` if it is a scenario record for exactly this run, else raise ValueError."""
    if not isinstance(record, dict) or record.get("record_type") != RECORD_TYPE:
        raise ValueError("not a fault-simulation scenario record")
    if record.get("format_version") != RECORD_FORMAT_VERSION:
        raise ValueError(f"unsupported scenario record format version {record.get('format_version')!r}")
    if record.get("run_id") != run.get("run_id"):
        raise ValueError(f"the scenario record is for run {record.get('run_id')!r}, "
                         f"not for run {run.get('run_id')!r}")
    if record.get("device") != run.get("ammeter"):
        raise ValueError(f"the scenario record is for device {record.get('device')!r}, "
                         f"but the run's device is {run.get('ammeter')!r}")
    comparison = record.get("comparison")
    if not isinstance(comparison, dict) or not isinstance(comparison.get("steps"), list) \
            or record.get("verdict") not in (VERDICT_PASS, VERDICT_FAIL):
        raise ValueError("the scenario record has no valid comparison or verdict")
    return record
