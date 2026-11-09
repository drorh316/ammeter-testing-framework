"""Performance consistency report for one completed run (a pure function; no I/O).

The report describes how consistently a run was carried out: how many attempts failed,
how many schedule slots were skipped, and how late and how long the requests were. It
says nothing about the measured current values or about device accuracy, and it does
not try to explain the cause of a delay.

Definitions
-----------
- Failure rate: ``failed / attempted``; null when there were no attempts.
- Skipped-slot rate: ``skipped_slots / (attempted + skipped_slots)``, the share of
  schedule slots inside the run that were passed over instead of used; null when both
  are zero.
- Start lateness: each attempt's actual start minus its scheduled start (``lateness_s``).
- Request duration: each attempt's ``elapsed_s``, from the start of the request until it
  returned or failed.
- Timing summaries use every attempt, successful or failed, and are in seconds: count,
  mean, median, population standard deviation, p95 and maximum. With no attempts the
  count is 0 and the other values are null.
- p95 uses the nearest-rank method: ``sorted_values[ceil(0.95 * n) - 1]``, computed with
  integer arithmetic, so it is always one of the observed values.

Thresholds are optional and user-defined; there are no built-in acceptance limits.
"""
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_NOT_EVALUATED = "not_evaluated"

TIMING_KEYS = ("mean_s", "median_s", "stddev_population_s", "p95_s", "max_s")


def _finite(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class PerformanceLimits:
    """Optional, user-defined limits. A value equal to its limit passes."""

    max_start_lateness_s: Optional[float] = None
    max_failure_rate: Optional[float] = None
    max_skipped_slot_rate: Optional[float] = None

    def __post_init__(self):
        if self.max_start_lateness_s is not None:
            value = _finite("max_start_lateness_s", self.max_start_lateness_s)
            if value < 0:
                raise ValueError(f"max_start_lateness_s must be at least 0, got {value!r}")
            object.__setattr__(self, "max_start_lateness_s", value)
        for name in ("max_failure_rate", "max_skipped_slot_rate"):
            raw = getattr(self, name)
            if raw is not None:
                value = _finite(name, raw)
                if not 0 <= value <= 1:
                    raise ValueError(f"{name} must be between 0 and 1, got {value!r}")
                object.__setattr__(self, name, value)

    @property
    def configured(self) -> bool:
        return any(value is not None for value in
                   (self.max_start_lateness_s, self.max_failure_rate, self.max_skipped_slot_rate))


def p95_nearest_rank(values: Sequence[float]) -> float:
    """The nearest-rank 95th percentile: ``sorted(values)[ceil(0.95 * n) - 1]``."""
    if not values:
        raise ValueError("p95 needs at least one value")
    ordered = sorted(values)
    rank = (95 * len(ordered) + 99) // 100  # ceil(0.95 * n) without floating-point rounding
    return ordered[rank - 1]


def summarize_timing(values: Sequence[float]) -> Dict[str, Any]:
    """count, mean, median, population std dev, p95 and maximum of ``values`` (seconds)."""
    checked = [_finite("timing value", value) for value in values]
    if not checked:
        return {"count": 0, **{key: None for key in TIMING_KEYS}}
    return {
        "count": len(checked),
        "mean_s": statistics.mean(checked),
        "median_s": statistics.median(checked),
        "stddev_population_s": statistics.pstdev(checked),
        "p95_s": p95_nearest_rank(checked),
        "max_s": max(checked),
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return None if denominator == 0 else numerator / denominator


def _check(name: str, observed: Optional[float], limit: Optional[float]) -> Optional[Dict[str, Any]]:
    if limit is None:
        return None
    if observed is None:
        status = STATUS_INSUFFICIENT_DATA
    else:
        status = STATUS_PASS if observed <= limit else STATUS_FAIL
    return {"name": name, "status": status, "observed": observed, "limit": limit}


def overall_status(checks: List[Dict[str, Any]]) -> str:
    """not_evaluated without checks; else fail > insufficient_data > pass."""
    statuses = {check["status"] for check in checks}
    if not checks:
        return STATUS_NOT_EVALUATED
    if STATUS_FAIL in statuses:
        return STATUS_FAIL
    if STATUS_INSUFFICIENT_DATA in statuses:
        return STATUS_INSUFFICIENT_DATA
    return STATUS_PASS


def build_performance_report(run: Dict[str, Any], limits: Optional[PerformanceLimits] = None) -> Dict[str, Any]:
    """Performance consistency report for a complete run (as returned by run_test or load_run)."""
    limits = limits or PerformanceLimits()
    counts = run["counts"]
    attempts = run["attempts"]
    attempted, failed, skipped = counts["attempted"], counts["failed"], counts["skipped_slots"]
    if attempted != len(attempts):
        raise ValueError(f"counts.attempted is {attempted} but the run has {len(attempts)} attempt records")

    lateness = summarize_timing([attempt["lateness_s"] for attempt in attempts])
    duration = summarize_timing([attempt["elapsed_s"] for attempt in attempts])
    failure_rate = _rate(failed, attempted)
    skipped_slot_rate = _rate(skipped, attempted + skipped)

    checks = [check for check in (
        _check("max_start_lateness_s", lateness["max_s"], limits.max_start_lateness_s),
        _check("failure_rate", failure_rate, limits.max_failure_rate),
        _check("skipped_slot_rate", skipped_slot_rate, limits.max_skipped_slot_rate),
    ) if check is not None]

    return {
        "run_id": run["run_id"],
        "ammeter": run["ammeter"],
        "started_at_utc": run["started_at_utc"],
        "settings": dict(run["settings"]),
        "counts": {"attempted": attempted, "succeeded": counts["succeeded"],
                   "failed": failed, "skipped_slots": skipped},
        "rates": {"failure_rate": failure_rate, "skipped_slot_rate": skipped_slot_rate},
        "start_lateness_s": lateness,
        "request_duration_s": duration,
        "thresholds": {"status": overall_status(checks), "checks": checks},
    }
