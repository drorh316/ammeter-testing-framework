"""Accuracy of readings against an explicitly supplied, constant reference current.

Accuracy needs a known reference: the true current that was applied while the readings
were taken. This module never infers one. The caller supplies the reference value (in
amperes) and a non-empty description of where it came from, and both are repeated in
every result.

The supplied emulators generate independent random values and are not driven by any
applied current, so these metrics cannot establish their accuracy, and must not be used
to rank Greenlee, ENTES and CIRCUTOR. The demo (``examples/accuracy_demo.py``) uses small
synthetic datasets instead.

Metrics (amperes), for readings x_1..x_n and reference r, with errors e_i = x_i - r:

- ``bias_a``: mean(e), the signed mean error. Positive means the readings are high on average.
- ``mean_absolute_error_a``: mean(|e|), the typical size of an individual error.
- ``rmse_a``: sqrt(mean(e^2)), root-mean-square error. RMSE^2 = bias^2 + stddev^2, so it
  combines a systematic offset and the spread.
- ``stddev_population_a``: the population standard deviation of the readings, i.e. their
  spread around their own mean. It does not use the reference and says nothing about
  accuracy: precise readings can all be wrong by the same amount. Reading it as
  repeatability requires controlled measurement conditions.

No relative (percentage) metrics: they are undefined for a zero reference and add little
for a constant one.

Numerics: sums use ``math.fsum``. RMSE is computed by scaling by the largest error, so
squaring never overflows; for example readings [1e200] against a reference of 0 give an
RMSE of exactly 1e200. If a difference from the reference itself overflows, bias, MAE and
RMSE are recomputed on values scaled by 1/4. The spread is computed separately from the
readings themselves with ``statistics.pstdev`` (exact fractions), so it does not depend on
the reference and keeps small variation next to large values. Only a result that cannot be
represented as a finite float gives status "error" (with an explanatory ``error`` field);
invalid inputs raise ValueError. Every result is strict-JSON compatible.
"""
import math
import numbers
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.testing.analysis import STATUS_ERROR, STATUS_NO_DATA, STATUS_OK

METRIC_KEYS = ("bias_a", "mean_absolute_error_a", "rmse_a", "stddev_population_a")

INTERPRETATION = ("Errors are relative to the supplied reference. They describe accuracy only if that current "
                  "was actually applied while the readings were taken; supplying a reference does not establish "
                  "that. stddev_population_a is the spread of the readings, not accuracy.")
RUN_INTERPRETATION = ("Errors are relative to the supplied reference, which is not stored with the run. Supplying it "
                      "does not establish that this current was applied during the run; the errors describe accuracy "
                      "only if it was. stddev_population_a is the spread of the readings, not accuracy. The supplied "
                      "emulators return random values that do not depend on any applied current, so for their runs "
                      "these numbers only describe the distance from the supplied value, not device accuracy.")


# --- Input validation ------------------------------------------------------------------------

def _finite_real(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} must be a real number, got {value!r} ({type(value).__name__})")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is too large to represent as a float") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _source(reference_source: Any) -> str:
    if not isinstance(reference_source, str) or not reference_source.strip():
        raise ValueError("reference_source must be a non-empty description of where the reference comes from")
    return reference_source


# --- Calculation ---------------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> float:
    try:
        return math.fsum(values) / len(values)
    except OverflowError:  # the exact sum exceeds the float range; the mean itself does not
        return math.fsum(v / len(values) for v in values)


def _rms(values: Sequence[float]) -> float:
    """sqrt(mean(v^2)) without squaring large numbers: every scaled term is at most 1."""
    largest = max(abs(v) for v in values)
    if largest == 0:
        return 0.0
    return largest * math.sqrt(math.fsum((v / largest) ** 2 for v in values) / len(values))


def _error_metrics(readings: Sequence[float], reference: float, scale: float) -> Optional[Tuple[float, ...]]:
    """(bias, MAE, RMSE) of ``scale``-times the errors, or None if a scaled error overflows."""
    errors = [x * scale - reference * scale for x in readings]
    if not all(math.isfinite(e) for e in errors):
        return None
    return _mean(errors), _mean([abs(e) for e in errors]), _rms(errors)


def _spread(readings: Sequence[float]) -> float:
    """Population standard deviation of the readings themselves, independent of the reference.

    statistics.pstdev works with exact fractions, so a small variation next to a large value
    (e.g. [1e16, 1e16 + 2] -> 1.0) is not lost, and the result for finite floats is at most
    half their range, so it is always finite.
    """
    return float(statistics.pstdev(readings))


def assess_accuracy(values: Iterable[Any], reference_a: Any, *, reference_source: Any) -> Dict[str, Any]:
    """Bias, mean absolute error, RMSE and spread of ``values`` against a constant reference.

    ``reference_a`` is any finite real number in amperes (zero and negative are valid);
    ``reference_source`` is a non-empty description of its origin. Raises ValueError for an
    invalid reference, source or value (bool, non-numbers, NaN, infinity, and integers too
    large for a float are rejected, not skipped).

    Status "no_data" (count 0, metrics None) when there are no values; "error" (metrics
    None, ``error`` explains) when a result cannot be represented as a finite float.
    """
    reference = _finite_real("reference_a", reference_a)
    source = _source(reference_source)
    readings = [_finite_real(f"value {index}", value) for index, value in enumerate(values)]
    result: Dict[str, Any] = {
        "status": STATUS_OK,
        "reference": {"current_a": reference, "source": source},
        "count": len(readings),
        **{key: None for key in METRIC_KEYS},
        "interpretation": INTERPRETATION,
    }
    if not readings:
        result["status"] = STATUS_NO_DATA
        return result

    scale = 1.0
    scaled = _error_metrics(readings, reference, scale)
    if scaled is None:
        scale = 0.25  # exact power of two: |x/4 - r/4| is at most half the float range, so nothing overflows
        scaled = _error_metrics(readings, reference, scale)
    metrics = dict(zip(METRIC_KEYS[:3], (value / scale for value in scaled)))
    try:
        metrics["stddev_population_a"] = _spread(readings)
    except OverflowError:  # not expected for finite floats; reported rather than leaked
        metrics["stddev_population_a"] = math.inf
    too_large = [key for key, value in metrics.items() if not math.isfinite(value)]
    if too_large:
        result["status"] = STATUS_ERROR
        result["error"] = {"type": "AnalysisError",
                           "message": f"{', '.join(too_large)} cannot be represented as finite floats: the "
                                      "differences from the reference exceed the float range"}
        return result
    result.update(metrics)
    return result


def assess_run(run: Dict[str, Any], reference_a: Any, *, reference_source: Any) -> Dict[str, Any]:
    """``assess_accuracy`` of a run's successful readings, with the run's identity and counts.

    Failed attempts have no reading: they are excluded and reported in ``counts``, never
    counted as 0 A. The run is not modified.
    """
    values = [a["current_a"] for a in run["attempts"] if a["ok"]]
    assessment = assess_accuracy(values, reference_a, reference_source=reference_source)
    attempted = len(run["attempts"])
    return {
        "run_id": run["run_id"],
        "ammeter": run["ammeter"],
        "counts": {"attempted": attempted, "succeeded": len(values), "failed": attempted - len(values),
                   "failed_excluded": True},
        **assessment,
        "interpretation": RUN_INTERPRETATION,
    }


# --- Ordering datasets by RMSE -----------------------------------------------------------------

def rank_by_rmse(assessments: Mapping[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order named assessments by their observed RMSE against one shared reference.

    This orders datasets by observed RMSE only. It does not identify the most reliable
    device and makes no claim of statistical significance. Every assessment must have
    status "ok" and the same reference value and source (ValueError otherwise), but a
    shared reference is necessary, not sufficient: whether the datasets were measured under
    comparable conditions is the caller's responsibility.

    Exactly equal RMSE values share a rank (competition ranking: 1, 1, 3); names only
    order the display within a tie.
    """
    references = set()
    for name, assessment in assessments.items():
        if assessment.get("status") != STATUS_OK:
            raise ValueError(f"{name!r} cannot be ranked: its status is {assessment.get('status')!r}, not 'ok'")
        reference = assessment["reference"]
        references.add((reference["current_a"], reference["source"]))
    if len(references) > 1:
        raise ValueError("only assessments against the same reference value and source can be ranked; got "
                         + "; ".join(f"{value!r} A ({source})" for value, source in sorted(references)))

    ordered = sorted(assessments.items(), key=lambda item: (item[1]["rmse_a"], item[0]))
    ranking = []
    for position, (name, assessment) in enumerate(ordered, start=1):
        tied_with_previous = bool(ranking) and assessment["rmse_a"] == ranking[-1]["rmse_a"]
        ranking.append({"rank": ranking[-1]["rank"] if tied_with_previous else position, "name": name,
                        "rmse_a": assessment["rmse_a"], "count": assessment["count"]})
    return ranking
