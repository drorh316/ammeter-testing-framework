"""Measurement sampling: when to request measurements and when to stop.

This module knows nothing about sockets or configuration files. It calls a
``measure(timeout)`` function on a fixed schedule and records every attempt.

Schedule
--------
Slot ``k`` has the target time ``t0 + k / f``, where ``t0`` is the clock reading at the
start of the run and ``f`` the sampling frequency. Slot 0 starts immediately. Targets
are always computed from ``t0``, so lateness never accumulates.

- A selected slot is never cancelled because the wake-up from sleep was late; its
  actual start and lateness are recorded. If ``sleep`` returns early, the remaining
  time is slept again, so a slot never starts before its target.
- After an attempt finishes, the next slot is the earliest later slot whose target is
  at or after the finish time. Earlier slots are skipped (no catch-up requests) and
  counted in ``skipped_slots``.
- No attempt starts at or after the duration deadline ``t0 + D``. Each request's
  timeout is clipped to the time remaining before the deadline.

Stopping
--------
- ``count_reached``: ``measurements_count`` attempts (successes plus failures) were made.
- ``duration_reached``: the duration limit prevents any further attempt, either because
  the next target is at or after the deadline (the run returns immediately, without
  sleeping until the deadline, so the elapsed time can be less than D) or because the
  deadline passed while waiting for a slot.
- If both apply after the same attempt, ``count_reached`` takes precedence.
"""
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from Ammeters.client import AmmeterCommunicationError

# Sanity limits that keep the schedule representable and the run bounded.
MIN_SAMPLING_FREQUENCY_HZ = 1.0 / 86400  # one sample per day
MAX_SAMPLING_FREQUENCY_HZ = 1000.0       # each sample is a separate TCP request
MAX_TOTAL_DURATION_SECONDS = 7 * 86400.0  # one week
MAX_MEASUREMENTS_COUNT = 1_000_000
MAX_REQUEST_TIMEOUT_SECONDS = 3600.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0

STOP_COUNT_REACHED = "count_reached"
STOP_DURATION_REACHED = "duration_reached"


def _positive_finite_number(name: str, value, maximum: float, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        hint = ""
        if isinstance(value, str):
            hint = " (write numbers without quotes; in YAML, exponents need a decimal point, e.g. 1.0e-3)"
        raise ValueError(f"{name} must be a number, got {value!r} ({type(value).__name__}){hint}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite, got {value!r}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class SamplingSettings:
    """Validated sampling settings. At least one of the two limits is required."""

    sampling_frequency_hz: float
    measurements_count: Optional[int] = None
    total_duration_seconds: Optional[float] = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def __post_init__(self):
        frequency = _positive_finite_number(
            "sampling_frequency_hz", self.sampling_frequency_hz,
            minimum=MIN_SAMPLING_FREQUENCY_HZ, maximum=MAX_SAMPLING_FREQUENCY_HZ)
        object.__setattr__(self, "sampling_frequency_hz", frequency)

        if self.measurements_count is not None:
            count = self.measurements_count
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError(
                    f"measurements_count must be a whole number or null, got {count!r} ({type(count).__name__})")
            if not 1 <= count <= MAX_MEASUREMENTS_COUNT:
                raise ValueError(f"measurements_count must be between 1 and {MAX_MEASUREMENTS_COUNT}, got {count!r}")

        if self.total_duration_seconds is not None:
            duration = _positive_finite_number(
                "total_duration_seconds", self.total_duration_seconds, maximum=MAX_TOTAL_DURATION_SECONDS)
            object.__setattr__(self, "total_duration_seconds", duration)

        if self.measurements_count is None and self.total_duration_seconds is None:
            raise ValueError("at least one stopping limit is required: measurements_count or total_duration_seconds")

        timeout = _positive_finite_number(
            "request_timeout_seconds", self.request_timeout_seconds, maximum=MAX_REQUEST_TIMEOUT_SECONDS)
        object.__setattr__(self, "request_timeout_seconds", timeout)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sampling_frequency_hz": self.sampling_frequency_hz,
            "measurements_count": self.measurements_count,
            "total_duration_seconds": self.total_duration_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
        }


@dataclass(frozen=True)
class AttemptRecord:
    """One measurement attempt. Times are offsets in seconds from the start of the run."""

    attempt: int                  # 1-based attempt number
    slot: int                     # schedule slot index (0-based)
    scheduled_offset_s: float     # slot target
    start_offset_s: float         # when the request actually started
    elapsed_s: float              # how long the request took
    effective_timeout_s: float    # timeout actually given to the request
    timeout_clipped_by_deadline: bool  # True if the run deadline shortened the configured timeout
    current_a: Optional[float] = None    # set only for a successful attempt
    error_type: Optional[str] = None     # set only for a failed attempt
    error_message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error_type is None

    @property
    def lateness_s(self) -> float:
        return self.start_offset_s - self.scheduled_offset_s

    def to_dict(self) -> Dict[str, Any]:
        record = {
            "attempt": self.attempt,
            "slot": self.slot,
            "scheduled_offset_s": self.scheduled_offset_s,
            "start_offset_s": self.start_offset_s,
            "lateness_s": self.lateness_s,
            "elapsed_s": self.elapsed_s,
            "effective_timeout_s": self.effective_timeout_s,
            "timeout_clipped_by_deadline": self.timeout_clipped_by_deadline,
            "ok": self.ok,
        }
        # A failed attempt has no value at all, so it can never be mistaken for 0 A.
        if self.ok:
            record["current_a"] = self.current_a
        else:
            record["error"] = {"type": self.error_type, "message": self.error_message}
        return record


@dataclass(frozen=True)
class SamplingResult:
    settings: SamplingSettings
    attempts: List[AttemptRecord]
    skipped_slots: int
    elapsed_s: float
    stop_reason: str

    @property
    def succeeded(self) -> int:
        return sum(1 for a in self.attempts if a.ok)

    @property
    def failed(self) -> int:
        return len(self.attempts) - self.succeeded

    @property
    def successful_values(self) -> List[float]:
        """Measured currents of the successful attempts only (derived, not stored separately)."""
        return [a.current_a for a in self.attempts if a.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "settings": self.settings.to_dict(),
            "counts": {
                "attempted": len(self.attempts),
                "succeeded": self.succeeded,
                "failed": self.failed,
                "skipped_slots": self.skipped_slots,
            },
            "elapsed_s": self.elapsed_s,
            "stop_reason": self.stop_reason,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def successful_values(result: Dict[str, Any]) -> List[float]:
    """Measured currents of the successful attempts in a ``SamplingResult.to_dict()`` result."""
    return [a["current_a"] for a in result["attempts"] if a["ok"]]


def run_sampling(
    measure: Callable[[float], float],
    settings: SamplingSettings,
    *,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> SamplingResult:
    """Call ``measure(timeout)`` on the schedule described in the module docstring.

    ``clock`` must be monotonic. The default, ``time.perf_counter``, is monotonic and has
    a much finer resolution than ``time.monotonic`` on some platforms (on Windows,
    ``time.monotonic`` can advance in steps of about 15.6 ms).

    ``measure`` returns a current in amperes or raises AmmeterCommunicationError, which
    is recorded as a failed attempt (no retry). Any other exception propagates.
    """
    frequency = settings.sampling_frequency_hz
    t0 = clock()

    def target(slot: int) -> float:
        return t0 + slot / frequency

    def first_slot_at_or_after(moment: float, after: int) -> int:
        """Smallest slot index greater than ``after`` whose target is >= ``moment``.

        The estimate from multiplication is corrected using target() itself, so the
        decision is made with exactly the same arithmetic as the targets (no
        floating-point boundary errors such as 0.3 * 10 == 3.0000000000000004).
        """
        slot = max(after + 1, math.ceil((moment - t0) * frequency))
        while target(slot) < moment:
            slot += 1
        while slot - 1 > after and target(slot - 1) >= moment:
            slot -= 1
        return slot

    deadline = None
    end_slot = None  # first slot whose target is at or after the deadline
    if settings.total_duration_seconds is not None:
        deadline = t0 + settings.total_duration_seconds
        end_slot = first_slot_at_or_after(deadline, after=-1)

    attempts: List[AttemptRecord] = []
    skipped = 0
    slot = 0

    def finish(reason: str) -> SamplingResult:
        return SamplingResult(settings, attempts, skipped, clock() - t0, reason)

    while True:
        # Wait for the selected slot. A late wake-up still runs it; an early one waits again.
        slot_target = target(slot)
        while True:
            now = clock()
            if deadline is not None and now >= deadline:
                return finish(STOP_DURATION_REACHED)
            if now >= slot_target:
                break
            sleep(slot_target - now)
        start = now

        timeout = settings.request_timeout_seconds
        clipped = False
        if deadline is not None and deadline - start < timeout:
            timeout = deadline - start  # positive, because start < deadline
            clipped = True

        value = None
        error: Optional[AmmeterCommunicationError] = None
        try:
            value = measure(timeout)
        except AmmeterCommunicationError as exc:
            error = exc
        end = clock()

        attempts.append(AttemptRecord(
            attempt=len(attempts) + 1,
            slot=slot,
            scheduled_offset_s=slot / frequency,
            start_offset_s=start - t0,
            elapsed_s=end - start,
            effective_timeout_s=timeout,
            timeout_clipped_by_deadline=clipped,
            current_a=value if error is None else None,
            error_type=None if error is None else type(error).__name__,
            error_message=None if error is None else str(error),
        ))

        # Stopping conditions first; count_reached takes precedence.
        if settings.measurements_count is not None and len(attempts) >= settings.measurements_count:
            return finish(STOP_COUNT_REACHED)

        next_slot = first_slot_at_or_after(end, after=slot)
        if end_slot is not None and next_slot >= end_slot:
            # Only slots inside the duration window count as skipped.
            skipped += max(0, end_slot - slot - 1)
            return finish(STOP_DURATION_REACHED)
        skipped += next_slot - slot - 1
        slot = next_slot
