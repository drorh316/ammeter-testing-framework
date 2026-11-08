"""Tests for configuration loading/validation and AmmeterTestFramework.run_test (no network)."""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.client import AmmeterConnectionError
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from fakes import FakeTime
from src.testing.archive import ArchiveFormatError, ResultArchive, ResultArchiveError
from src.testing.test_framework import AmmeterTestFramework
from src.utils.config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    ConfigError,
    load_config,
    parse_result_management,
)

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
    assert result["analysis"] == {
        "status": "ok", "count": 2, "mean_a": 1.25, "median_a": 1.25, "stddev_population_a": 0.0, "min_a": 1.25, "max_a": 1.25,
    }
    json.dumps(result, allow_nan=False)  # strict JSON


def mixed_config(count):
    data = json.loads(json.dumps(VALID_CONFIG))
    data["testing"]["sampling"]["measurements_count"] = count
    return data


def test_analysis_uses_only_successful_measurements(tmp_path):
    fake_time = FakeTime()
    outcomes = [1.0, AmmeterConnectionError(5001, "refused"), 3.0, AmmeterConnectionError(5001, "refused")]
    request = RecordingRequest(fake_time, outcomes=outcomes)
    fw = framework(write_config(tmp_path, mixed_config(4)), request, fake_time)

    result = fw.run_test("greenlee")

    assert result["counts"] == {"attempted": 4, "succeeded": 2, "failed": 2, "skipped_slots": 0}
    # Mean of 1.0 and 3.0 only. Substituting 0 for the failures would give 1.0 and min 0.0.
    assert result["analysis"] == {
        "status": "ok", "count": 2, "mean_a": 2.0, "median_a": 2.0, "stddev_population_a": 1.0, "min_a": 1.0, "max_a": 3.0,
    }
    assert [a["ok"] for a in result["attempts"]] == [True, False, True, False]
    json.dumps(result, allow_nan=False)


def test_all_failed_attempts_give_empty_analysis_and_keep_failure_records(tmp_path):
    fake_time = FakeTime()
    outcomes = [AmmeterConnectionError(5001, "refused")] * 3
    request = RecordingRequest(fake_time, outcomes=outcomes)
    fw = framework(write_config(tmp_path, mixed_config(3)), request, fake_time)

    result = fw.run_test("greenlee")

    assert result["analysis"] == {
        "status": "no_data", "count": 0, "mean_a": None, "median_a": None, "stddev_population_a": None, "min_a": None, "max_a": None,
    }
    assert result["counts"] == {"attempted": 3, "succeeded": 0, "failed": 3, "skipped_slots": 0}
    assert all(a["error"]["type"] == "AmmeterConnectionError" for a in result["attempts"])
    assert result["stop_reason"] == "count_reached"
    json.loads(json.dumps(result, allow_nan=False))  # None becomes null


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


# --- Run identity, analysis failures and archiving -------------------------------

RUN_ID = "3f2b8c1e-5a4d-4e6f-9b7a-1c2d3e4f5a6b"


def archiving_config(database_path, count=2):
    data = json.loads(json.dumps(VALID_CONFIG))
    data["testing"]["sampling"]["measurements_count"] = count
    data["result_management"] = {"archive_enabled": True, "database_path": str(database_path)}
    return data


def stepping_utc_clock(start=datetime(2026, 11, 8, 17, 0, 0, 123456, tzinfo=timezone.utc)):
    moments = iter(start + timedelta(seconds=i) for i in range(1000))
    return lambda: next(moments)


def full_framework(config_path, request, fake_time, run_id=RUN_ID):
    return AmmeterTestFramework(config_path, request=request, clock=fake_time.clock, sleep=fake_time.sleep,
                                now=stepping_utc_clock(), new_run_id=lambda: run_id)


def test_result_has_run_id_and_utc_timestamps(tmp_path):
    fake_time = FakeTime()
    fw = full_framework(write_config(tmp_path, VALID_CONFIG), RecordingRequest(fake_time), fake_time)

    result = fw.run_test("greenlee")

    assert result["run_id"] == RUN_ID
    assert result["started_at_utc"] == "2026-11-08T17:00:00.123456+00:00"
    assert result["ended_at_utc"] == "2026-11-08T17:00:01.123456+00:00"
    assert fw.archive is None  # no result_management section: archiving disabled


def test_default_run_ids_are_unique_uuid4_strings(tmp_path):
    fake_time = FakeTime()
    fw = framework(write_config(tmp_path, VALID_CONFIG), RecordingRequest(fake_time), fake_time)

    ids = {fw.run_test("greenlee")["run_id"] for _ in range(3)}

    assert len(ids) == 3
    assert all(uuid.UUID(run_id).version == 4 for run_id in ids)


def test_naive_timestamps_are_rejected(tmp_path):
    fake_time = FakeTime()
    fw = AmmeterTestFramework(write_config(tmp_path, VALID_CONFIG), request=RecordingRequest(fake_time),
                              clock=fake_time.clock, sleep=fake_time.sleep, now=lambda: datetime(2026, 1, 1))

    with pytest.raises(ValueError, match="timezone-aware"):
        fw.run_test("greenlee")


def test_analysis_failure_keeps_measurements_and_reports_error_status(tmp_path):
    # Two finite values whose median, (a + b) / 2, overflows to infinity.
    fake_time = FakeTime()
    request = RecordingRequest(fake_time, outcomes=[1.7e308, 1.7e308])
    fw = framework(write_config(tmp_path, VALID_CONFIG), request, fake_time)

    result = fw.run_test("greenlee")

    assert result["analysis"]["status"] == "error"
    assert result["analysis"]["count"] == 2
    assert result["analysis"]["error"]["type"] == "AnalysisError"
    assert all(result["analysis"][key] is None for key in ("mean_a", "median_a", "stddev_population_a"))
    assert [a["current_a"] for a in result["attempts"]] == [1.7e308, 1.7e308]
    assert result["counts"] == {"attempted": 2, "succeeded": 2, "failed": 0, "skipped_slots": 0}
    json.dumps(result, allow_nan=False)


def test_unexpected_analysis_errors_are_not_swallowed(tmp_path):
    # A client returning a non-number is a programming error: the plain ValueError propagates.
    fake_time = FakeTime()
    request = RecordingRequest(fake_time, outcomes=["not a number", 1.0])
    fw = framework(write_config(tmp_path, VALID_CONFIG), request, fake_time)

    with pytest.raises(ValueError, match="must be a number"):
        fw.run_test("greenlee")


def test_run_test_archives_the_result_and_it_reloads_unchanged(tmp_path):
    database = tmp_path / "archive.sqlite3"
    fake_time = FakeTime()
    request = RecordingRequest(fake_time, outcomes=[1.5, AmmeterConnectionError(5001, "refused"), 2.5])
    fw = full_framework(write_config(tmp_path, archiving_config(database, count=3)), request, fake_time)

    result = fw.run_test("greenlee")

    assert fw.archive.path == database
    assert ResultArchive(database).load_run(RUN_ID) == result


def test_storage_failure_raises_with_the_complete_result(tmp_path):
    database = tmp_path / "foreign.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    fake_time = FakeTime()
    fw = full_framework(write_config(tmp_path, archiving_config(database)), RecordingRequest(fake_time), fake_time)

    with pytest.raises(ResultArchiveError, match=f"run {RUN_ID} \\(greenlee\\) was collected but not archived") as excinfo:
        fw.run_test("greenlee")

    assert isinstance(excinfo.value.__cause__, ArchiveFormatError)
    collected = excinfo.value.result
    assert collected["run_id"] == RUN_ID
    assert collected["counts"]["succeeded"] == 2
    assert [a["current_a"] for a in collected["attempts"]] == [1.25, 1.25]


def test_storage_directory_failure_is_reported_with_the_result(tmp_path):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")
    fake_time = FakeTime()
    config = archiving_config(blocker / "archive.sqlite3")
    fw = full_framework(write_config(tmp_path, config), RecordingRequest(fake_time), fake_time)

    with pytest.raises(ResultArchiveError) as excinfo:
        fw.run_test("greenlee")

    assert isinstance(excinfo.value.__cause__.__cause__, OSError)  # ResultArchiveError <- ArchiveError <- OSError
    assert excinfo.value.result["counts"]["attempted"] == 2


# --- result_management configuration ------------------------------------------------

def test_shipped_config_archives_to_the_project_results_database():
    settings = AmmeterTestFramework().archive_settings

    assert settings.enabled is True
    assert settings.database_path == PROJECT_ROOT / "results" / "ammeter_results.sqlite3"


@pytest.mark.parametrize("section", [None, {}, {"archive_enabled": False}])
def test_missing_or_disabled_result_management_disables_archiving(section):
    settings = parse_result_management({"result_management": section})

    assert (settings.enabled, settings.database_path) == (False, None)


def test_relative_database_path_is_resolved_against_the_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the working directory must not matter
    settings = parse_result_management(
        {"result_management": {"archive_enabled": True, "database_path": "results/x.sqlite3"}})

    assert settings.enabled is True
    assert settings.database_path == PROJECT_ROOT / "results" / "x.sqlite3"


def test_absolute_database_path_is_used_as_given(tmp_path):
    settings = parse_result_management(
        {"result_management": {"archive_enabled": True, "database_path": str(tmp_path / "a.sqlite3")}})

    assert settings.database_path == tmp_path / "a.sqlite3"


@pytest.mark.parametrize(
    "section, message",
    [
        ({"archive_enabled": "yes"}, "archive_enabled must be true or false"),
        ({"archive_enabled": True}, "database_path is required"),
        ({"archive_enabled": True, "database_path": ""}, "database_path must be a non-empty path"),
        ({"archive_enabled": True, "database_path": 5}, "database_path must be a non-empty path"),
        ({"archive_enabled": True, "database_path": "a.sqlite3", "path": "x"},
         "unknown setting(s) in result_management: path"),
        ("enabled", "result_management must be a mapping"),
    ],
)
def test_invalid_result_management_is_rejected(section, message):
    with pytest.raises(ConfigError) as excinfo:
        parse_result_management({"result_management": section})

    assert message in str(excinfo.value)


def test_tests_cannot_write_the_shipped_results_database():
    # The conftest guard stops any test from saving to the shipped database path.
    fake_time = FakeTime()
    fw = AmmeterTestFramework(request=RecordingRequest(fake_time), clock=fake_time.clock, sleep=fake_time.sleep)

    with pytest.raises(AssertionError, match="shipped results database"):
        fw.run_test("greenlee")
