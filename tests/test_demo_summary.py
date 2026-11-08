"""Tests for the sampling demo's summary (no emulators are started)."""
from Ammeters.client import AmmeterTimeoutError
from examples.run_tests import check_archived, summarize_run
from fakes import FakeMeasure, FakeTime
from src.testing.analysis import AnalysisError, failed_analysis, summarize_measurements
from src.testing.archive import ResultArchive
from src.testing.sampling import SamplingSettings, run_sampling

RUN_ID = "0b6e3a6c-1d2e-4f3a-8b4c-5d6e7f8a9b0c"


def run_result(settings, script=(), clock=None):
    """A run_test()-shaped result built from a fake sampling run."""
    fake_time = FakeTime()
    measure = FakeMeasure(fake_time, script)
    result = run_sampling(measure, settings, clock=clock or fake_time.clock, sleep=fake_time.sleep)
    return {"run_id": RUN_ID, "ammeter": "greenlee", "host": "127.0.0.1", "port": 5001,
            "command": "MEASURE_GREENLEE -get_measurement",
            "started_at_utc": "2026-11-08T17:00:00.000000+00:00",
            "ended_at_utc": "2026-11-08T17:00:01.000000+00:00",
            **result.to_dict(), "analysis": summarize_measurements(result.successful_values)}


def test_empty_run_is_summarized_without_timing_and_not_reported_as_success():
    # A valid run that never reached slot 0 before its deadline.
    readings = iter([0.0, 2.0, 2.0])
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, total_duration_seconds=1.0),
                        clock=lambda: next(readings))
    assert result["counts"]["attempted"] == 0

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert "attempted 0, succeeded 0, failed 0" in text
    assert "statistics of 0 successful measurement(s): mean N/A, median N/A, population std dev N/A" in text
    assert "min N/A, max N/A" in text
    assert "timing: unavailable (no attempts were made)" in text
    assert "NO MEASUREMENTS" in text
    assert demonstrated is False


def test_successful_run_is_reported_as_demonstrated_with_statistics_in_amperes():
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2),
                        script=[(0.1, 2.0), (0.1, 4.0)])

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert demonstrated is True
    assert "statistics of 2 successful measurement(s): mean 3 A, median 3 A, population std dev 1 A" in text
    assert "min 2 A, max 4 A" in text
    assert "start lateness vs. target" in text
    assert sum(1 for line in lines if line.lstrip().startswith("#")) == 2


def test_display_is_rounded_but_returned_data_keeps_full_precision():
    value = 0.014791290164367787
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=1), script=[(0.1, value)])

    lines, _ = summarize_run("greenlee", result)

    assert "mean 0.0147913 A" in "\n".join(lines)
    assert result["analysis"]["mean_a"] == value


def test_run_with_a_failed_attempt_is_not_reported_as_demonstrated():
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2),
                        script=[(0.1, 1.0), (0.1, AmmeterTimeoutError(5001, "timed out"))])

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert demonstrated is False
    assert "FAILED AmmeterTimeoutError" in text
    assert "statistics of 1 successful measurement(s): mean 1 A" in text


def test_run_where_every_attempt_failed_shows_unavailable_statistics():
    error = AmmeterTimeoutError(5001, "timed out")
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2),
                        script=[(0.1, error), (0.1, error)])

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert demonstrated is False
    assert "statistics of 0 successful measurement(s): mean N/A" in text
    assert text.count("FAILED AmmeterTimeoutError") == 2


def test_failed_analysis_is_shown_and_not_reported_as_demonstrated():
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2),
                        script=[(0.1, 1.5), (0.1, 2.5)])
    result["analysis"] = failed_analysis(2, AnalysisError("median_a overflowed"))  # injected failure

    lines, demonstrated = summarize_run("greenlee", result)

    text = "\n".join(lines)
    assert demonstrated is False
    assert "UNAVAILABLE, analysis failed (AnalysisError: median_a overflowed)" in text
    assert "1.5 A" in text and "2.5 A" in text  # the collected measurements are still shown


def test_archive_check_reports_disabled_saved_and_failed_archiving(tmp_path):
    result = run_result(SamplingSettings(sampling_frequency_hz=2.0, measurements_count=2))
    archive = ResultArchive(tmp_path / "demo.sqlite3")
    archive.save_run(result)

    assert check_archived(None, result, None) == ("  archive: disabled", True)

    line, ok = check_archived(archive, result, None)
    assert ok is True and "saved and reloaded unchanged" in line

    line, ok = check_archived(archive, result, "ArchiveLockedError: database is locked")
    assert ok is False and "NOT SAVED (ArchiveLockedError: database is locked)" in line

    changed = dict(result, elapsed_s=result["elapsed_s"] + 1)
    line, ok = check_archived(archive, changed, None)
    assert ok is False and "DIFFERS" in line

    other = dict(result, run_id="11111111-2222-4333-8444-555555555555")
    line, ok = check_archived(archive, other, None)
    assert ok is False and "reloading FAILED (RunNotFoundError" in line
