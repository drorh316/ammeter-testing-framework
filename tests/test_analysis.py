"""Tests for the statistical summary of successful measurements."""
import json
import math

import pytest

from src.testing.analysis import METRIC_KEYS, AnalysisError, failed_analysis, summarize_measurements


def test_known_values():
    assert summarize_measurements([2, 4]) == {
        "status": "ok", "count": 2, "mean_a": 3.0, "median_a": 3.0, "stddev_population_a": 1.0, "min_a": 2.0, "max_a": 4.0,
    }


def test_unsorted_values_with_an_even_count_use_the_middle_pair_for_the_median():
    result = summarize_measurements([4.0, 1.0, 3.0, 2.0])

    assert result["median_a"] == 2.5  # mean of the two middle values, 2 and 3
    assert result["mean_a"] == 2.5
    assert (result["min_a"], result["max_a"]) == (1.0, 4.0)
    assert result["stddev_population_a"] == pytest.approx(math.sqrt(1.25))


def test_odd_count_median_is_the_middle_value():
    assert summarize_measurements([9.0, 1.0, 5.0])["median_a"] == 5.0


def test_population_not_sample_standard_deviation():
    # Population: sqrt(((1-2)^2 + (2-2)^2 + (3-2)^2) / 3) = sqrt(2/3); a sample stddev would be 1.0.
    assert summarize_measurements([1.0, 2.0, 3.0])["stddev_population_a"] == pytest.approx(math.sqrt(2 / 3))


def test_empty_input_has_count_zero_and_no_metrics():
    assert summarize_measurements([]) == {"status": "no_data", "count": 0, **{key: None for key in METRIC_KEYS}}


def test_single_value():
    assert summarize_measurements([0.0147]) == {
        "status": "ok", "count": 1, "mean_a": 0.0147, "median_a": 0.0147, "stddev_population_a": 0.0,
        "min_a": 0.0147, "max_a": 0.0147,
    }


def test_repeated_values_have_zero_spread():
    result = summarize_measurements([5.0, 5.0, 5.0])

    assert result["stddev_population_a"] == 0.0
    assert result["mean_a"] == result["median_a"] == result["min_a"] == result["max_a"] == 5.0


def test_zero_and_negative_values_are_valid():
    result = summarize_measurements([-1.0, 0.0, 1.0])

    assert (result["count"], result["mean_a"], result["median_a"]) == (3, 0.0, 0.0)
    assert (result["min_a"], result["max_a"]) == (-1.0, 1.0)
    assert result["stddev_population_a"] == pytest.approx(math.sqrt(2 / 3))


def test_full_precision_is_kept():
    value = 0.014791290164367787
    assert summarize_measurements([value])["mean_a"] == value


def test_all_metrics_are_floats():
    result = summarize_measurements([2, 4])

    assert all(type(result[key]) is float for key in METRIC_KEYS)


@pytest.mark.parametrize(
    "bad_value, message",
    [
        (True, "must be a number"),
        (False, "must be a number"),
        ("1.0", "must be a number"),
        (None, "must be a number"),
        ([1.0], "must be a number"),
        (1j, "must be a number"),
        (math.nan, "must be finite"),
        (math.inf, "must be finite"),
        (-math.inf, "must be finite"),
        pytest.param(10 ** 400, "is too large", id="int-10**400"),
    ],
)
def test_invalid_values_are_rejected_not_skipped(bad_value, message):
    with pytest.raises(ValueError, match=f"measurement 1 {message}") as excinfo:
        summarize_measurements([1.0, bad_value, 2.0])

    # Invalid input is a programming error, not the expected AnalysisError.
    assert not isinstance(excinfo.value, AnalysisError)


def test_unrepresentable_statistic_is_an_error_not_infinity():
    # Both values are finite, but their median, (a + b) / 2 in floating point, overflows.
    with pytest.raises(AnalysisError, match="cannot be represented as finite floats: median_a"):
        summarize_measurements([1.7e308, 1.7e308])


def test_extreme_but_representable_values_are_summarized():
    result = summarize_measurements([-1.7e308, 1.7e308])

    assert result["mean_a"] == 0.0
    assert result["stddev_population_a"] == pytest.approx(1.7e308)
    assert all(math.isfinite(result[key]) for key in METRIC_KEYS)


def test_accepts_any_iterable():
    assert summarize_measurements(v for v in (2.0, 4.0))["count"] == 2


@pytest.mark.parametrize("values", [[], [1.5], [2, 4], [-1.0, 0.0, 1.0]])
def test_result_is_strict_json(values):
    json.dumps(summarize_measurements(values), allow_nan=False)


def test_failed_analysis_keeps_the_count_and_reports_the_error():
    error = AnalysisError("median_a overflowed")

    assert failed_analysis(2, error) == {
        "status": "error", "count": 2, **{key: None for key in METRIC_KEYS},
        "error": {"type": "AnalysisError", "message": "median_a overflowed"},
    }
    json.dumps(failed_analysis(2, error), allow_nan=False)


def test_analysis_error_is_a_value_error_for_existing_callers():
    assert issubclass(AnalysisError, ValueError)
