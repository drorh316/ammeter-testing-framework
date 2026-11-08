"""Statistical summary of successful current measurements (amperes).

Only successful measurements should be passed in; failed attempts have no value and
must not be counted as 0 A.

The standard deviation is the *population* standard deviation (statistics.pstdev):
it describes the spread of the measurements collected in this run, rather than
estimating the spread of a larger population from a sample (statistics.stdev).
"""
import math
import statistics
from typing import Any, Dict, Iterable, Optional

METRIC_KEYS = ("mean_a", "median_a", "stddev_population_a", "min_a", "max_a")


def summarize_measurements(values: Iterable[float]) -> Dict[str, Any]:
    """Return count, mean, median, population standard deviation, minimum and maximum.

    All metrics are in amperes and returned at full float precision. With no values,
    ``count`` is 0 and every metric is None. With one value, the mean, median, minimum
    and maximum are that value and the standard deviation is 0.0.

    Raises ValueError if a value is not a finite real number (bool, str, None, NaN and
    infinity are rejected, not skipped), or if a statistic of finite but extreme values
    cannot be represented as a finite float. Zero and negative values are valid.
    """
    measurements = [_finite_float(index, value) for index, value in enumerate(values)]
    if not measurements:
        return {"count": 0, **{key: None for key in METRIC_KEYS}}

    try:
        metrics: Dict[str, Optional[float]] = {
            "mean_a": float(statistics.mean(measurements)),
            "median_a": float(statistics.median(measurements)),
            "stddev_population_a": float(statistics.pstdev(measurements)),
            "min_a": min(measurements),
            "max_a": max(measurements),
        }
    except OverflowError as exc:
        raise ValueError(f"statistics of these measurements cannot be represented as finite floats: {exc}") from exc

    not_finite = [key for key, value in metrics.items() if not math.isfinite(value)]
    if not_finite:
        raise ValueError(
            f"statistics of these measurements cannot be represented as finite floats: {', '.join(not_finite)}")
    return {"count": len(measurements), **metrics}


def _finite_float(index: int, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"measurement {index} must be a number, got {value!r} ({type(value).__name__})")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"measurement {index} is too large to represent as a float") from exc
    if not math.isfinite(number):
        raise ValueError(f"measurement {index} must be finite, got {value!r}")
    return number
