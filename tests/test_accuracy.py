"""Accuracy against a supplied reference (src/testing/accuracy.py), the ``accuracy`` command and the demo."""
import json
import math
import sys
from fractions import Fraction

import pytest

from Ammeters.client import AmmeterConnectionError, AmmeterTimeoutError
from examples import accuracy_demo
from src import archive_cli
from src.testing.accuracy import METRIC_KEYS, assess_accuracy, assess_run, rank_by_rmse
from src.utils.config import PROJECT_ROOT
from test_archive import snapshot
from test_archive_queries import archive_with, run_id, variant

SOURCE = "test reference"
BIASED = [2.10, 2.11, 2.09, 2.10, 2.10]
NOISY = [1.80, 2.20, 1.90, 2.10, 2.00]
ACCURATE = [2.01, 1.99, 2.00, 2.02, 1.98]


def assess(values, reference=2.0, source=SOURCE):
    return assess_accuracy(values, reference, reference_source=source)


def metrics(result):
    return {key: result[key] for key in METRIC_KEYS}


# --- Metrics -------------------------------------------------------------------------------------

@pytest.mark.parametrize("values, bias, mae, spread", [
    (BIASED, 0.1, 0.1, math.sqrt(0.00004)),
    (NOISY, 0.0, 0.12, math.sqrt(0.02)),
    (ACCURATE, 0.0, 0.012, math.sqrt(0.0002)),
])
def test_profiles_match_hand_calculated_values(values, bias, mae, spread):
    result = assess(values)
    assert result["status"] == "ok" and result["count"] == 5
    assert result["bias_a"] == pytest.approx(bias, abs=1e-12)
    assert result["mean_absolute_error_a"] == pytest.approx(mae, rel=1e-12)
    assert result["stddev_population_a"] == pytest.approx(spread, rel=1e-9)
    assert result["rmse_a"] == pytest.approx(math.sqrt(bias ** 2 + spread ** 2), rel=1e-9)


def test_low_spread_is_not_accuracy():
    biased, noisy = assess(BIASED), assess(NOISY)
    assert biased["stddev_population_a"] < noisy["stddev_population_a"]  # the more precise readings ...
    assert abs(biased["bias_a"]) > abs(noisy["bias_a"])                     # ... are the more biased ones


@pytest.mark.parametrize("values", [BIASED, NOISY, [5.0, -3.0, 0.25], [1e-300, 3e-300]])
def test_rmse_squared_is_bias_squared_plus_spread_squared(values):
    result = assess(values, reference=0.5)
    assert result["rmse_a"] ** 2 == pytest.approx(result["bias_a"] ** 2 + result["stddev_population_a"] ** 2,
                                                  rel=1e-9)


def test_metrics_agree_with_exact_rational_arithmetic():
    values, reference = [0.1, 0.7, -0.3, 2.5], 0.2
    errors = [Fraction(v) - Fraction(reference) for v in values]
    bias = sum(errors) / len(errors)
    result = assess(values, reference)
    assert result["bias_a"] == pytest.approx(float(bias), rel=1e-15)
    assert result["mean_absolute_error_a"] == pytest.approx(float(sum(abs(e) for e in errors) / 4), rel=1e-15)
    assert result["rmse_a"] == pytest.approx(math.sqrt(sum(e * e for e in errors) / 4), rel=1e-15)


def test_a_single_reading_has_zero_spread_and_rmse_equal_to_the_absolute_bias():
    result = assess([1.75])
    assert metrics(result) == {"bias_a": -0.25, "mean_absolute_error_a": 0.25, "rmse_a": 0.25,
                               "stddev_population_a": 0.0}


def test_exact_readings_give_zero_errors():
    assert metrics(assess([2.0, 2.0, 2.0])) == dict.fromkeys(METRIC_KEYS, 0.0)


def test_a_zero_reference_is_valid():
    result = assess([0.1, -0.1, 0.2], reference=0)
    assert result["status"] == "ok" and result["reference"]["current_a"] == 0.0
    assert result["bias_a"] == pytest.approx(0.2 / 3) and result["mean_absolute_error_a"] == pytest.approx(0.4 / 3)
    assert not any("percent" in key or "relative" in key for key in result)


def test_negative_readings_and_reference_are_valid():
    result = assess([-2.1, -1.9, -2.3], reference=-2.0)
    assert result["status"] == "ok"
    assert result["bias_a"] == pytest.approx(-0.1) and result["mean_absolute_error_a"] == pytest.approx(0.5 / 3)


def test_integer_inputs_are_accepted():
    assert metrics(assess([3, 5], reference=4)) == {"bias_a": 0.0, "mean_absolute_error_a": 1.0, "rmse_a": 1.0,
                                                    "stddev_population_a": 1.0}


def test_the_reference_and_its_source_are_in_every_result():
    result = assess(BIASED, reference=2, source="calibrator X, certificate 7")
    assert result["reference"] == {"current_a": 2.0, "source": "calibrator X, certificate 7"}
    assert "does not establish" in result["interpretation"]
    assert "spread of the readings, not accuracy" in result["interpretation"]


def test_no_readings_give_no_data_with_no_metrics():
    result = assess(iter([]))
    assert result["status"] == "no_data" and result["count"] == 0
    assert metrics(result) == dict.fromkeys(METRIC_KEYS)
    assert "error" not in result


# --- Extreme values ---------------------------------------------------------------------------------

def test_a_representable_extreme_rmse_does_not_overflow():
    result = assess([1e200], reference=0)  # (1e200)**2 overflows, but the RMSE is 1e200
    assert result["status"] == "ok"
    assert metrics(result) == {"bias_a": 1e200, "mean_absolute_error_a": 1e200, "rmse_a": 1e200,
                               "stddev_population_a": 0.0}


def test_large_readings_with_huge_spread_are_still_representable():
    result = assess([1.7e308, -1.7e308], reference=0)  # the readings' deviations overflow before scaling
    assert result["status"] == "ok"
    assert metrics(result) == {"bias_a": 0.0, "mean_absolute_error_a": 1.7e308, "rmse_a": 1.7e308,
                               "stddev_population_a": 1.7e308}


def test_a_sum_beyond_the_float_range_still_gives_a_representable_mean():
    result = assess([1.5e308, 1.5e308, 1.5e308], reference=0)
    assert result["status"] == "ok" and result["bias_a"] == 1.5e308 and result["rmse_a"] == pytest.approx(1.5e308)


def test_tiny_values_keep_their_precision():
    result = assess([5e-324, 1e-323], reference=0)
    assert result["status"] == "ok" and result["bias_a"] == pytest.approx(7.5e-324, abs=5e-324)
    assert result["mean_absolute_error_a"] > 0 and result["rmse_a"] > 0


@pytest.mark.parametrize("values, reference", [([1.7e308], -1.7e308), ([1e308, 1e308], -1e308)])
def test_errors_beyond_the_float_range_give_status_error_with_an_explanation(values, reference):
    result = assess(values, reference=reference)
    assert result["status"] == "error" and result["count"] == len(values)
    assert metrics(result) == dict.fromkeys(METRIC_KEYS)
    assert result["error"]["type"] == "AnalysisError"
    assert "cannot be represented as finite floats" in result["error"]["message"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("values", [BIASED, [1e200], [1.7e308, -1.7e308], [], [1.7e308]])
def test_results_are_strict_json(values):
    result = assess(values, reference=-1.7e308 if values == [1.7e308] else 0)
    assert json.loads(json.dumps(result, allow_nan=False)) == result


# --- Spread depends only on the readings ------------------------------------------------------------------

def exact_spread(values):
    """Independent oracle: population standard deviation in exact rational arithmetic, rounded once at the end."""
    mean = sum(Fraction(v) for v in values) / len(values)
    variance = sum((Fraction(v) - mean) ** 2 for v in values) / len(values)
    # Integer square root at high scale, so only the final conversion rounds.
    scale = 10 ** 40
    return math.isqrt(variance.numerator * scale ** 2 // variance.denominator) / scale


@pytest.mark.parametrize("values, expected", [([1.0, 2.0], 0.5), ([1e16, 1e16 + 2], 1.0)])
@pytest.mark.parametrize("reference", [0, 1e16, 1e20, -1e20])
def test_spread_does_not_depend_on_the_reference(values, expected, reference):
    result = assess(values, reference=reference)
    assert result["status"] == "ok"
    assert result["stddev_population_a"] == expected == exact_spread(values)


def test_small_variation_next_to_a_large_bias_is_kept():
    result = assess([1e16, 1e16 + 2], reference=0)
    assert result["stddev_population_a"] == 1.0  # not sqrt(2), which rounded errors and mean would give
    assert result["bias_a"] == 1e16 + 1 and result["rmse_a"] == pytest.approx(1e16 + 1, rel=1e-15)


@pytest.mark.parametrize("values", [BIASED, NOISY, ACCURATE, [0.1, 0.7, -0.3, 2.5], [3.0, 3.0, 3.0 + 2 ** -50],
                                    [123456.789, 123456.791, 123456.7905]])
@pytest.mark.parametrize("reference", [0.0, 2.0, -7.5e3, 1e12])
def test_spread_matches_an_exact_oracle(values, reference):
    assert assess(values, reference=reference)["stddev_population_a"] == pytest.approx(exact_spread(values),
                                                                                       rel=1e-15, abs=0)


@pytest.mark.parametrize("values, expected", [
    ([1.7e308, -1.7e308], 1.7e308),
    ([1.7976931348623157e308, -1.7976931348623157e308], 1.7976931348623157e308),  # the largest float
    ([1.5e308, 1.5e308, 1.5e308], 0.0),
    ([1e200], 0.0),
])
def test_spread_of_extreme_readings_is_finite_and_exact(values, expected):
    result = assess(values, reference=0)
    assert result["status"] == "ok" and result["stddev_population_a"] == expected


# --- Invalid input ------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "1.0", None, 10 ** 400, 1j])
def test_invalid_readings_are_rejected(bad):
    with pytest.raises(ValueError, match="value 1"):
        assess([1.0, bad])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), False, "2", None, 10 ** 400, -10 ** 400])
def test_invalid_references_are_rejected(bad):
    with pytest.raises(ValueError, match="reference_a"):
        assess([1.0], reference=bad)


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 5])
def test_a_missing_or_blank_reference_source_is_rejected(bad):
    with pytest.raises(ValueError, match="reference_source"):
        assess([1.0], source=bad)


def test_a_huge_integer_does_not_leak_overflow_error():
    with pytest.raises(ValueError, match="too large to represent as a float"):
        assess([10 ** 400])


# --- Runs -----------------------------------------------------------------------------------------------

def run_with(outcomes):
    return {"run_id": run_id(1), "ammeter": "greenlee",
            "attempts": [{"attempt": n, "ok": not isinstance(o, Exception),
                          **({"error": {"type": type(o).__name__, "message": str(o)}} if isinstance(o, Exception)
                             else {"current_a": o})}
                         for n, o in enumerate(outcomes, start=1)]}


def test_failed_attempts_are_excluded_not_counted_as_zero():
    run = run_with([2.1, AmmeterTimeoutError(5001, "slow"), 1.9])
    original = json.loads(json.dumps(run))
    result = assess_run(run, 2.0, reference_source=SOURCE)
    assert result["counts"] == {"attempted": 3, "succeeded": 2, "failed": 1, "failed_excluded": True}
    assert result["count"] == 2 and result["bias_a"] == pytest.approx(0.0, abs=1e-15)
    assert result["mean_absolute_error_a"] == pytest.approx(0.1)  # with a 0 A reading it would be 0.8
    assert result["run_id"] == run_id(1) and result["ammeter"] == "greenlee"
    assert "not stored with the run" in result["interpretation"]
    assert "random values" in result["interpretation"]
    assert run == original


def test_a_run_without_successful_readings_gives_no_data():
    result = assess_run(run_with([AmmeterConnectionError(5001, "refused")]), 1.0, reference_source=SOURCE)
    assert result["status"] == "no_data" and result["counts"]["failed"] == 1 and result["rmse_a"] is None


# --- Ordering by RMSE ---------------------------------------------------------------------------------

def test_ordering_is_by_observed_rmse():
    ranking = rank_by_rmse({"b": assess(NOISY), "a": assess(BIASED), "c": assess(ACCURATE)})
    assert [(entry["rank"], entry["name"]) for entry in ranking] == [(1, "c"), (2, "a"), (3, "b")]
    assert ranking[0]["rmse_a"] == assess(ACCURATE)["rmse_a"] and ranking[0]["count"] == 5


def test_exact_rmse_ties_share_a_rank():
    ranking = rank_by_rmse({"z": assess([2.1]), "y": assess([1.9]), "x": assess([2.5]), "w": assess([2.0])})
    assert [(entry["rank"], entry["name"]) for entry in ranking] == [(1, "w"), (2, "y"), (2, "z"), (4, "x")]


def test_all_tied_datasets_share_rank_one():
    ranking = rank_by_rmse({"b": assess([2.5]), "a": assess([1.5])})
    assert [entry["rank"] for entry in ranking] == [1, 1]


def test_an_empty_mapping_gives_an_empty_ordering():
    assert rank_by_rmse({}) == []


@pytest.mark.parametrize("other", [assess(BIASED, reference=2.5), assess(BIASED, source="another source")])
def test_different_references_cannot_be_ranked(other):
    with pytest.raises(ValueError, match="same reference value and source"):
        rank_by_rmse({"a": assess(NOISY), "b": other})


@pytest.mark.parametrize("other", [assess([]), assess([1.7e308], reference=-1.7e308)])
def test_assessments_without_metrics_cannot_be_ranked(other):
    with pytest.raises(ValueError, match="cannot be ranked"):
        rank_by_rmse({"a": assess(NOISY), "b": other})


# --- CLI ------------------------------------------------------------------------------------------------

@pytest.fixture
def archive(tmp_path):
    return archive_with(
        tmp_path,
        variant(tmp_path, 1, outcomes=(2.1, AmmeterConnectionError(5001, "refused"), 1.9)),
        variant(tmp_path, 2, outcomes=(AmmeterConnectionError(5001, "refused"),)),
        variant(tmp_path, 3, outcomes=(1.7e308,)))


def cli(archive, *args):
    return archive_cli.main(["--db", str(archive.path), "accuracy", *args])


def test_cli_prints_the_assessment_and_leaves_the_database_unchanged(capsys, archive):
    before, rows = archive.path.read_bytes(), snapshot(archive.path)
    code = cli(archive, run_id(1), "--reference-a", "2.0", "--reference-source", "calibrator X")
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert (code, err) == (0, "")
    assert report["reference"] == {"current_a": 2.0, "source": "calibrator X"}
    assert report["counts"]["failed"] == 1 and report["count"] == 2 and report["status"] == "ok"
    assert report == assess_run(archive.load_run(run_id(1)), 2.0, reference_source="calibrator X")
    assert "does not establish that this current was applied during the run" in report["interpretation"]
    assert archive.path.read_bytes() == before and snapshot(archive.path) == rows


def test_cli_no_data_exits_0(capsys, archive):
    assert cli(archive, run_id(2), "--reference-a", "0", "--reference-source", "x") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_data"


def test_cli_status_error_prints_the_report_and_exits_1(capsys, archive):
    before = archive.path.read_bytes()
    # "=" form: argparse does not treat "-1.7e308" (exponent notation) as a negative number.
    code = cli(archive, run_id(3), "--reference-a=-1.7e308", "--reference-source", "x")
    out, err = capsys.readouterr()
    assert code == 1 and json.loads(out)["status"] == "error"
    assert err.startswith("accuracy: ") and "cannot be represented" in err
    assert archive.path.read_bytes() == before


def test_cli_negative_reference_is_accepted(capsys, archive):
    assert cli(archive, run_id(1), "--reference-a", "-2", "--reference-source", "x") == 0
    assert json.loads(capsys.readouterr().out)["bias_a"] == pytest.approx(4.0)


def test_cli_missing_run_exits_1(capsys, archive):
    assert cli(archive, run_id(9), "--reference-a", "1", "--reference-source", "x") == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("error: ")


def test_cli_missing_database_exits_1(capsys, tmp_path):
    code = archive_cli.main(["--db", str(tmp_path / "none.sqlite3"), "accuracy", run_id(1),
                             "--reference-a", "1", "--reference-source", "x"])
    assert code == 1 and not (tmp_path / "none.sqlite3").exists()


@pytest.mark.parametrize("args", [
    [],                                                        # both options missing
    ["--reference-a", "1"],                                    # source missing
    ["--reference-source", "x"],                               # reference missing
    ["--reference-a", "nan", "--reference-source", "x"],
    ["--reference-a", "inf", "--reference-source", "x"],
    ["--reference-a", "1e999", "--reference-source", "x"],
    ["--reference-a", "two", "--reference-source", "x"],
    ["--reference-a", "1", "--reference-source", ""],
    ["--reference-a", "1", "--reference-source", "   "],
])
def test_cli_invalid_arguments_exit_2(capsys, archive, args):
    with pytest.raises(SystemExit) as exc:
        cli(archive, run_id(1), *args)
    assert exc.value.code == 2


def test_cli_invalid_run_id_exits_2(capsys, archive):
    with pytest.raises(SystemExit) as exc:
        cli(archive, "not-a-run", "--reference-a", "1", "--reference-source", "x")
    assert exc.value.code == 2


# --- Synthetic demo and sample ------------------------------------------------------------------------

def test_committed_sample_matches_the_demo_exactly():
    committed = (PROJECT_ROOT / "sample_results" / "accuracy_synthetic.json").read_text(encoding="utf-8")
    assert json.loads(committed) == accuracy_demo.build_demo()
    assert committed == json.dumps(accuracy_demo.build_demo(), indent=2, allow_nan=False) + "\n"


def test_demo_profiles_are_labelled_synthetic_and_never_emulator_names():
    demo = accuracy_demo.build_demo()
    assert demo["synthetic"] is True and demo["reference"]["source"].startswith("synthetic:")
    for name in demo["profiles"]:
        assert name.startswith("synthetic-")
        assert not any(device in name.lower() for device in ("greenlee", "entes", "circutor"))
    assert [entry["name"] for entry in demo["ordering_by_rmse"]] == [
        "synthetic-accurate", "synthetic-biased-precise", "synthetic-unbiased-noisy"]
    assert "does not identify a most reliable device" in demo["ordering_note"]


def test_demo_prints_the_table_and_writes_a_new_file_only(capsys, tmp_path):
    output = tmp_path / "accuracy.json"
    assert accuracy_demo.main(["--output", str(output)]) == 0
    printed = capsys.readouterr().out
    assert "SYNTHETIC data, reference 2.0 A" in printed and "1. synthetic-accurate (RMSE 0.0141 A)" in printed
    assert "synthetic-unbiased-noisy       0.0000" in printed  # float residue is not shown as -0.0000
    assert json.loads(output.read_text(encoding="utf-8")) == accuracy_demo.build_demo()
    assert accuracy_demo.main(["--output", str(output)]) == 1  # never overwritten
    assert "error:" in capsys.readouterr().err


def test_demo_runs_as_a_module(tmp_path):
    import subprocess
    completed = subprocess.run([sys.executable, "-m", "examples.accuracy_demo"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0 and "ordering by observed RMSE" in completed.stdout
