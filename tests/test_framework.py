"""Tests for configuration loading/validation and AmmeterTestFramework.run_test (no network)."""
import json

import pytest
import yaml

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.client import AmmeterConnectionError
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from fakes import FakeTime
from src.testing.test_framework import AmmeterTestFramework
from src.utils.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

VALID_CONFIG = {
    "testing": {
        "sampling": {
            "sampling_frequency_hz": 2.0,
            "measurements_count": 2,
            "total_duration_seconds": None,
            "request_timeout_seconds": 1.0,
        }
    },
    "ammeters": {
        "greenlee": {"host": "127.0.0.1", "port": 5001, "command": "MEASURE_GREENLEE -get_measurement"},
        "entes": {"port": 5002, "command": "MEASURE_ENTES -get_data"},
    },
}


class RecordingRequest:
    """Fake client: records each call and returns (or raises) scripted outcomes."""

    def __init__(self, fake_time=None, outcomes=(), duration=0.01):
        self._time = fake_time
        self._outcomes = list(outcomes)
        self._duration = duration
        self.calls = []

    def __call__(self, port, command, *, timeout, host):
        self.calls.append({"port": port, "command": command, "timeout": timeout, "host": host})
        if self._time is not None:
            self._time.now += self._duration
        outcome = self._outcomes.pop(0) if self._outcomes else 1.25
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def write_config(tmp_path, data=None, text=None):
    path = tmp_path / "config.yaml"
    path.write_text(text if text is not None else yaml.safe_dump(data), encoding="utf-8")
    return path


def config_with(section, key, value, *, remove=False):
    data = json.loads(json.dumps(VALID_CONFIG))  # deep copy
    target = data["testing"]["sampling"] if section == "sampling" else data["ammeters"][section]
    if remove:
        del target[key]
    else:
        target[key] = value
    return data


def framework(path, request=None, fake_time=None):
    fake_time = fake_time or FakeTime()
    return AmmeterTestFramework(
        path, request=request or RecordingRequest(), clock=fake_time.clock, sleep=fake_time.sleep)


# --- Default configuration -----------------------------------------------------

def test_default_config_path_is_absolute_and_independent_of_working_directory(tmp_path, monkeypatch):
    assert DEFAULT_CONFIG_PATH.is_absolute() and DEFAULT_CONFIG_PATH.is_file()
    monkeypatch.chdir(tmp_path)  # a directory without config/config.yaml

    fw = AmmeterTestFramework()

    assert set(fw.ammeters) == {"greenlee", "entes", "circutor"}


def test_shipped_config_matches_the_working_emulators():
    devices = AmmeterTestFramework().ammeters
    expected = {"greenlee": (GreenleeAmmeter, 5001), "entes": (EntesAmmeter, 5002),
                "circutor": (CircutorAmmeter, 5003)}

    for name, (emulator_class, port) in expected.items():
        assert devices[name].port == port  # the ports main.py starts the emulators on
        assert devices[name].command == emulator_class(port).get_current_command
        assert devices[name].host == "127.0.0.1"


def test_shipped_sampling_settings_are_valid():
    s = AmmeterTestFramework().sampling
    assert s.sampling_frequency_hz > 0
    assert s.measurements_count is not None or s.total_duration_seconds is not None


# --- Invalid configuration (rejected before any request) ----------------------

@pytest.mark.parametrize(
    "section, key, value, remove, message",
    [
        ("sampling", "sampling_frequency_hz", None, False, "testing.sampling.sampling_frequency_hz is required"),
        ("sampling", "sampling_frequency_hz", None, True, "testing.sampling.sampling_frequency_hz is required"),
        ("sampling", "sampling_frequency_hz", "5", False, "sampling_frequency_hz must be a number"),
        ("sampling", "sampling_frequency_hz", True, False, "sampling_frequency_hz must be a number"),
        ("sampling", "sampling_frequency_hz", 1e9, False, "sampling_frequency_hz must be between"),
        ("sampling", "measurements_count", 2.5, False, "measurements_count must be a whole number"),
        ("sampling", "measurements_count", None, False, "at least one stopping limit"),
        ("sampling", "total_duration_seconds", 0, False, "total_duration_seconds must be positive"),
        ("sampling", "request_timeout_seconds", -1, False, "request_timeout_seconds must be positive"),
        ("sampling", "sampling_frequency", 2.0, False, "unknown setting(s) in testing.sampling: sampling_frequency"),
        ("greenlee", "port", "5001", False, "ammeters.greenlee.port must be a whole number"),
        ("greenlee", "port", True, False, "ammeters.greenlee.port must be a whole number"),
        ("greenlee", "port", 70000, False, "ammeters.greenlee.port must be a whole number from 1 to 65535"),
        ("greenlee", "port", None, True, "ammeters.greenlee.port must be a whole number"),
        ("greenlee", "command", "", False, "ammeters.greenlee.command must be a non-empty string"),
        ("greenlee", "command", 123, False, "ammeters.greenlee.command must be a non-empty string"),
        ("greenlee", "command", "MEASURE_Ω", False, "ammeters.greenlee.command must contain only ASCII"),
        ("greenlee", "host", "localhost", False, "ammeters.greenlee.host must be a numeric IPv4 address"),
        ("greenlee", "host", "::1", False, "ammeters.greenlee.host must be a numeric IPv4 address"),
        ("greenlee", "adress", "127.0.0.1", False, "unknown setting(s) in ammeters.greenlee: adress"),
    ],
)
def test_invalid_settings_raise_field_specific_errors(tmp_path, section, key, value, remove, message):
    request = RecordingRequest()
    path = write_config(tmp_path, config_with(section, key, value, remove=remove))

    with pytest.raises(ConfigError) as excinfo:
        framework(path, request)

    assert message in str(excinfo.value)
    assert request.calls == []


def test_yaml_exponent_without_decimal_point_is_reported_as_a_string(tmp_path):
    # PyYAML (YAML 1.1) reads 1e-3 as the string "1e-3", not as a number.
    text = yaml.safe_dump(VALID_CONFIG).replace("sampling_frequency_hz: 2.0", "sampling_frequency_hz: 1e-3")
    assert "1e-3" in text

    with pytest.raises(ConfigError, match="sampling_frequency_hz must be a number.*decimal point"):
        framework(write_config(tmp_path, text=text))


def test_null_sampling_values_from_the_supplied_template_are_rejected(tmp_path):
    text = ("testing:\n  sampling:\n    measurements_count: NULL\n    total_duration_seconds: NULL\n"
            "    sampling_frequency_hz: NULL\nammeters:\n  greenlee: {port: 5001, command: X}\n")

    with pytest.raises(ConfigError, match="sampling_frequency_hz is required"):
        framework(write_config(tmp_path, text=text))


@pytest.mark.parametrize(
    "data, message",
    [
        ({"testing": {"sampling": {"sampling_frequency_hz": 1, "measurements_count": 1}}}, "missing configuration section: ammeters"),
        ({**VALID_CONFIG, "ammeters": {}}, "at least one ammeter"),
        ({**VALID_CONFIG, "ammeters": {"greenlee": 5001}}, "ammeters.greenlee must be a mapping"),
        ({"ammeters": VALID_CONFIG["ammeters"]}, "missing configuration section: testing"),
        ({**VALID_CONFIG, "testing": {"sampling": None}}, "testing.sampling must be a mapping"),
    ],
)
def test_invalid_structure_is_rejected(tmp_path, data, message):
    with pytest.raises(ConfigError, match=message):
        framework(write_config(tmp_path, data))


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "must contain a mapping"),
        ("- just\n- a list\n", "must contain a mapping"),
        ("testing: [unclosed\n", "not valid YAML"),
    ],
)
def test_unusable_files_are_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text=text))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "missing.yaml")


# --- run_test ------------------------------------------------------------------

def test_run_test_samples_the_selected_ammeter_through_the_client(tmp_path):
    fake_time = FakeTime()
    request = RecordingRequest(fake_time)
    fw = framework(write_config(tmp_path, VALID_CONFIG), request, fake_time)

    result = fw.run_test("entes")

    assert request.calls == [
        {"port": 5002, "command": b"MEASURE_ENTES -get_data", "timeout": 1.0, "host": "127.0.0.1"},
    ] * 2
    assert result["ammeter"] == "entes"
    assert (result["host"], result["port"], result["command"]) == ("127.0.0.1", 5002, "MEASURE_ENTES -get_data")
    assert result["counts"] == {"attempted": 2, "succeeded": 2, "failed": 0, "skipped_slots": 0}
    assert result["stop_reason"] == "count_reached"
    assert [a["current_a"] for a in result["attempts"]] == [1.25, 1.25]
    assert [a["start_offset_s"] for a in result["attempts"]] == pytest.approx([0.0, 0.5])
    json.dumps(result)  # JSON-compatible


def test_run_test_records_client_failures(tmp_path):
    fake_time = FakeTime()
    request = RecordingRequest(fake_time, outcomes=[AmmeterConnectionError(5001, "refused"), 2.0])
    fw = framework(write_config(tmp_path, VALID_CONFIG), request, fake_time)

    result = fw.run_test("greenlee")

    assert result["counts"] == {"attempted": 2, "succeeded": 1, "failed": 1, "skipped_slots": 0}
    assert result["attempts"][0]["error"]["type"] == "AmmeterConnectionError"
    assert "current_a" not in result["attempts"][0]


def test_run_test_rejects_unknown_ammeter_type_before_any_request(tmp_path):
    request = RecordingRequest()
    fw = framework(write_config(tmp_path, VALID_CONFIG), request)

    with pytest.raises(ValueError, match="unknown ammeter type 'fluke'; configured types:") as excinfo:
        fw.run_test("fluke")

    assert "greenlee" in str(excinfo.value) and "entes" in str(excinfo.value)
    assert request.calls == []
