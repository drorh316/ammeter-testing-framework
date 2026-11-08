"""Tests for the sampling demo's summary (no emulators are started)."""
from Ammeters.client import AmmeterTimeoutError
from examples.run_tests import summarize_run
from fakes import FakeMeasure, FakeTime
from src.testing.sampling import SamplingSettings, run_sampling


def run_result(settings, script=(), clock=None):
    fake_time = FakeTime()
    measure = FakeMeasure(fake_time, script)
    result = run_sampling(measure, settings, clock=clock or fake_time.clock, sleep=fake_time.sleep)
    return {"ammeter": "greenlee", "host": "127.0.0.1", "port": 5001,
            "command": "MEASURE_GREENLEE -get_measurement", **result.to_dict()}


def test_empty_run_is_summarized_without_timing_and_not_reported_as_success():
    # A valid run that never reached slot 0 before its deadline.
    readings = iter([0.0, 2.0, 2.0])
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, total_duration_seconds=1.0),
                        clock=lambda: next(readings))
    assert result["counts"]["attempted"] == 0

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert "attempted 0, succeeded 0, failed 0" in text
    assert "timing: unavailable (no attempts were made)" in text
    assert "NO MEASUREMENTS" in text
    assert demonstrated is False


def test_successful_run_is_reported_as_demonstrated():
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2))

    lines, demonstrated = summarize_run("greenlee", result)

    assert demonstrated is True
    assert any("start lateness vs. target" in line for line in lines)
    assert sum(1 for line in lines if line.lstrip().startswith("#")) == 2


def test_run_with_a_failed_attempt_is_not_reported_as_demonstrated():
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2),
                        script=[(0.1, 1.0), (0.1, AmmeterTimeoutError(5001, "timed out"))])

    lines, demonstrated = summarize_run("greenlee", result)

    assert demonstrated is False
    assert any("FAILED AmmeterTimeoutError" in line for line in lines)
