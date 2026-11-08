"""Deterministic tests for the sampling scheduler (fake clock, sleep and measurement)."""
import json
import math

import pytest

from Ammeters.client import AmmeterConnectionError, AmmeterTimeoutError
from fakes import EndsAt, FakeMeasure, FakeTime
from src.testing.sampling import (
    MAX_POTENTIAL_ATTEMPTS,
    STOP_COUNT_REACHED,
    STOP_DURATION_REACHED,
    SamplingSettings,
    run_sampling,
    successful_values,
)


def sample(settings, script=(), *, start=0.0, wake_errors=(), default=(0.1, 1.0)):
    fake_time = FakeTime(start=start, wake_errors=wake_errors)
    measure = FakeMeasure(fake_time, script, default=default)
    result = run_sampling(measure, settings, clock=fake_time.clock, sleep=fake_time.sleep)
    return result, measure, fake_time


def settings(frequency=2.0, count=None, duration=None, timeout=5.0):
    return SamplingSettings(
        sampling_frequency_hz=frequency, measurements_count=count,
        total_duration_seconds=duration, request_timeout_seconds=timeout)


# --- Stopping limits -----------------------------------------------------------

def test_count_only_samples_on_schedule_and_stops_without_final_sleep():
    result, measure, fake_time = sample(settings(count=3))

    assert measure.starts == pytest.approx([0.0, 0.5, 1.0])
    assert fake_time.sleeps == pytest.approx([0.4, 0.4])  # no sleep before slot 0 or after the last attempt
    assert [a.scheduled_offset_s for a in result.attempts] == pytest.approx([0.0, 0.5, 1.0])
    assert result.stop_reason == STOP_COUNT_REACHED
    assert result.elapsed_s == pytest.approx(1.1)
    assert (len(result.attempts), result.succeeded, result.failed, result.skipped_slots) == (3, 3, 0, 0)


def test_duration_only_uses_targets_before_the_deadline():
    # D = 1.2 s at 2 Hz: targets 0, 0.5, 1.0 are in [0, D); 1.5 is not. 3 attempts, not 2.4.
    result, measure, fake_time = sample(settings(duration=1.2))

    assert [a.scheduled_offset_s for a in result.attempts] == pytest.approx([0.0, 0.5, 1.0])
    assert result.stop_reason == STOP_DURATION_REACHED
    # Returns right after the last attempt instead of sleeping until 1.2 s.
    assert result.elapsed_s == pytest.approx(1.1)
    assert fake_time.sleeps == pytest.approx([0.4, 0.4])


def test_duration_that_is_a_whole_number_of_periods_excludes_the_target_at_the_deadline():
    result, _, _ = sample(settings(duration=1.0))

    assert [a.scheduled_offset_s for a in result.attempts] == pytest.approx([0.0, 0.5])
    assert result.stop_reason == STOP_DURATION_REACHED


def test_combined_limits_duration_reached_first():
    result, _, _ = sample(settings(count=10, duration=2.0))

    assert len(result.attempts) == 4
    assert result.stop_reason == STOP_DURATION_REACHED


def test_combined_limits_count_reached_first():
    result, _, _ = sample(settings(count=2, duration=10.0))

    assert len(result.attempts) == 2
    assert result.stop_reason == STOP_COUNT_REACHED
    assert result.elapsed_s == pytest.approx(0.6)


def test_count_takes_precedence_when_both_limits_apply_after_the_same_attempt():
    # The 3rd attempt reaches the count, and the next target (1.5 s) is past D = 1.2 s.
    result, _, _ = sample(settings(count=3, duration=1.2))

    assert len(result.attempts) == 3
    assert result.stop_reason == STOP_COUNT_REACHED


# --- Errors --------------------------------------------------------------------

def test_communication_failures_are_recorded_counted_and_not_retried():
    timeout_error = AmmeterTimeoutError(5001, "request did not complete")
    script = [(0.1, 1.5), (0.1, timeout_error), (0.1, 2.5)]
    result, measure, _ = sample(settings(count=3), script)

    assert len(measure.calls) == 3  # no retry: the count includes the failure
    assert (result.succeeded, result.failed) == (2, 1)
    failed = result.attempts[1]
    assert not failed.ok and failed.current_a is None
    assert failed.error_type == "AmmeterTimeoutError"
    assert "port 5001" in failed.error_message
    assert result.successful_values == [1.5, 2.5]

    record = result.to_dict()["attempts"][1]
    assert record["ok"] is False
    assert "current_a" not in record  # a failure is never stored as a value
    assert record["error"] == {"type": "AmmeterTimeoutError", "message": str(timeout_error)}
    assert successful_values(result.to_dict()) == [1.5, 2.5]


def test_sampling_continues_at_the_next_slot_after_a_failure():
    script = [(0.1, AmmeterConnectionError(5001, "refused")), (0.1, 2.0)]
    result, measure, _ = sample(settings(count=2), script)

    assert measure.starts == pytest.approx([0.0, 0.5])
    assert [a.ok for a in result.attempts] == [False, True]


@pytest.mark.parametrize("unexpected", [RuntimeError("bug"), ValueError("bad argument"), KeyError("x")])
def test_unexpected_exceptions_propagate(unexpected):
    with pytest.raises(type(unexpected)):
        sample(settings(count=3), [(0.1, 1.0), (0.1, unexpected)])


# --- Request timeout clipping --------------------------------------------------

def test_request_timeout_is_clipped_to_the_remaining_duration():
    result, measure, _ = sample(settings(duration=1.2, timeout=0.3))

    assert measure.timeouts == pytest.approx([0.3, 0.3, 0.2])
    assert [a.effective_timeout_s for a in result.attempts] == pytest.approx([0.3, 0.3, 0.2])
    assert [a.timeout_clipped_by_deadline for a in result.attempts] == [False, False, True]


def test_count_only_run_never_clips_the_timeout():
    result, measure, _ = sample(settings(count=2, timeout=0.3))

    assert measure.timeouts == [0.3, 0.3]
    assert not any(a.timeout_clipped_by_deadline for a in result.attempts)


# --- Missed (skipped) slots ----------------------------------------------------

def test_slow_request_skips_passed_slots_without_catch_up():
    # The first request ends at 1.2 s: targets 0.5 and 1.0 are skipped; the next runs at 1.5.
    result, measure, fake_time = sample(settings(count=3), [(1.2, 1.0)])

    assert measure.starts == pytest.approx([0.0, 1.5, 2.0])
    assert [a.slot for a in result.attempts] == [0, 3, 4]
    assert result.skipped_slots == 2
    assert fake_time.sleeps[0] == pytest.approx(0.3)


def test_request_ending_exactly_on_a_target_uses_that_slot():
    result, measure, fake_time = sample(settings(count=2), [(0.5, 1.0)])

    assert measure.starts == [0.0, 0.5]
    assert result.skipped_slots == 0
    assert fake_time.sleeps == []  # already at the target: no sleep


def test_request_ending_just_after_a_target_skips_it():
    result, measure, _ = sample(settings(count=2), [(0.5001, 1.0)])

    assert measure.starts == pytest.approx([0.0, 1.0])
    assert [a.slot for a in result.attempts] == [0, 2]
    assert result.skipped_slots == 1


def test_skipped_slots_are_counted_only_inside_the_duration_window():
    # D = 1.2 s: the request ends at 2.0 s, missing targets 0.5 and 1.0. Targets 1.5 and
    # later are outside the window and are not counted.
    result, _, _ = sample(settings(duration=1.2), [(2.0, 1.0)])

    assert len(result.attempts) == 1
    assert result.skipped_slots == 2
    assert result.stop_reason == STOP_DURATION_REACHED


def test_no_skipped_slots_are_counted_after_the_count_is_reached():
    result, _, _ = sample(settings(count=1), [(2.0, 1.0)])

    assert result.skipped_slots == 0
    assert result.stop_reason == STOP_COUNT_REACHED


# --- Exact floating-point boundaries ---------------------------------------------

@pytest.mark.parametrize("start", [0.0, 1000.0, 123456.789])
def test_exact_boundary_at_10_hz_uses_the_slot_ending_on_its_target(start):
    # At 10 Hz, 0.3 * 10 == 3.0000000000000004, so a naive ceil() would skip slot 3.
    # The request ends exactly at slot 3's target, so slot 3 must be used.
    result, measure, fake_time = sample(
        settings(frequency=10.0, count=2), [EndsAt(start + 3 / 10, 1.0)], start=start)

    assert [a.slot for a in result.attempts] == [0, 3]
    assert result.skipped_slots == 2
    assert fake_time.sleeps == []
    assert measure.starts[1] == start + 3 / 10


@pytest.mark.parametrize("start", [0.0, 1000.0, 123456.789])
def test_exact_duration_boundary_at_10_hz(start):
    # D = 0.3 s at 10 Hz: targets 0, 0.1, 0.2 only; the target at 0.3 equals the deadline.
    result, _, _ = sample(settings(frequency=10.0, duration=0.3), start=start, default=(0.01, 1.0))

    assert [a.slot for a in result.attempts] == [0, 1, 2]
    assert result.stop_reason == STOP_DURATION_REACHED


# --- Late and early wake-ups ---------------------------------------------------

def test_late_wake_up_runs_the_selected_slot_without_drift():
    result, measure, _ = sample(settings(count=3), wake_errors=[0.003, 0.003])

    assert measure.starts == pytest.approx([0.0, 0.503, 1.003])
    assert [a.scheduled_offset_s for a in result.attempts] == pytest.approx([0.0, 0.5, 1.0])
    assert [a.lateness_s for a in result.attempts] == pytest.approx([0.0, 0.003, 0.003])
    assert result.skipped_slots == 0


def test_very_late_wake_up_still_runs_the_selected_slot_then_skips_passed_slots():
    # Slot 1 (0.5 s) wakes at 1.1 s, past slot 2's target. Slot 1 still runs, late;
    # it ends at 1.2 s, so slot 2 (1.0 s) is skipped and slot 3 (1.5 s) is next.
    result, measure, _ = sample(settings(count=3), wake_errors=[0.6])

    assert measure.starts == pytest.approx([0.0, 1.1, 1.5])
    assert [a.slot for a in result.attempts] == [0, 1, 3]
    assert result.attempts[1].lateness_s == pytest.approx(0.6)
    assert result.skipped_slots == 1


def test_late_wake_up_at_or_after_the_deadline_does_not_start_a_request():
    # 2 Hz, D = 1 s: the first request takes 0.1 s; the sleep for slot 1 (0.5 s) wakes at 1.1 s.
    result, measure, _ = sample(settings(duration=1.0), wake_errors=[0.6])

    assert len(measure.calls) == 1
    assert result.stop_reason == STOP_DURATION_REACHED
    assert result.skipped_slots == 1  # slot 1 (0.5 s) was eligible but missed


def test_oversleep_past_the_deadline_counts_every_eligible_missed_slot():
    # 2 Hz, D = 2 s: the sleep for slot 1 wakes at 2.3 s. Slots at 0.5, 1.0 and 1.5 s were
    # missed; targets at or after 2.0 s are outside the window and not counted.
    result, measure, _ = sample(settings(duration=2.0), wake_errors=[1.8])

    assert len(measure.calls) == 1
    assert result.skipped_slots == 3
    assert result.stop_reason == STOP_DURATION_REACHED


def test_oversleep_landing_exactly_on_the_deadline_counts_the_selected_slot():
    result, measure, _ = sample(settings(duration=1.0), wake_errors=[0.5])  # wakes at exactly 1.0 s

    assert len(measure.calls) == 1
    assert result.skipped_slots == 1


def test_window_that_expires_before_the_first_attempt_returns_zero_attempts():
    # A valid run whose execution is delayed: the clock reads 0.0 at the start, then 1.5 s,
    # past the 1 s deadline, before slot 0 can start.
    fake_time = FakeTime()
    measure = FakeMeasure(fake_time)
    readings = iter([0.0])

    def delayed_clock():
        reading = next(readings, None)
        if reading is not None:
            return reading
        fake_time.now = max(fake_time.now, 1.5)
        return fake_time.now

    result = run_sampling(measure, settings(duration=1.0), clock=delayed_clock, sleep=fake_time.sleep)

    assert measure.calls == []
    assert result.attempts == []
    assert (result.succeeded, result.failed) == (0, 0)
    assert result.skipped_slots == 2  # slots 0 and 0.5 s, both before the deadline
    assert result.stop_reason == STOP_DURATION_REACHED
    assert result.to_dict()["counts"] == {"attempted": 0, "succeeded": 0, "failed": 0, "skipped_slots": 2}


# --- Unrepresentable schedules ---------------------------------------------------

def test_duration_that_rounds_away_on_the_clock_is_rejected_before_measuring():
    # At 1e16 the spacing between floats is 2.0, so 1e16 + 1.0 == 1e16: the deadline would
    # not be later than the start. The sampling interval (4 s at 0.25 Hz) still advances.
    with pytest.raises(ValueError, match="total_duration_seconds=1.0 does not give a finite deadline"):
        sample(settings(frequency=0.25, duration=1.0), start=1e16)


def test_interval_that_does_not_advance_the_clock_is_rejected_before_measuring():
    # At 1e14 the spacing between floats is 1/64 s, so a 1 ms interval does not advance it.
    fake_time = FakeTime(start=1e14)
    measure = FakeMeasure(fake_time)

    with pytest.raises(ValueError, match="sampling interval .* does not advance the clock"):
        run_sampling(measure, settings(frequency=1000.0, count=3), clock=fake_time.clock, sleep=fake_time.sleep)

    assert measure.calls == []


@pytest.mark.parametrize("bad_start", [math.nan, math.inf])
def test_non_finite_clock_reading_is_rejected_before_measuring(bad_start):
    fake_time = FakeTime(start=bad_start)
    measure = FakeMeasure(fake_time)

    with pytest.raises(ValueError, match="does not advance the clock"):
        run_sampling(measure, settings(count=3), clock=fake_time.clock, sleep=fake_time.sleep)

    assert measure.calls == []


def test_early_wake_up_sleeps_again_instead_of_sampling_early():
    result, measure, fake_time = sample(settings(count=2), wake_errors=[-0.2])

    assert fake_time.sleeps == pytest.approx([0.4, 0.2])  # woke at 0.3 s, slept the rest
    assert measure.starts == pytest.approx([0.0, 0.5])
    assert result.attempts[1].lateness_s == pytest.approx(0.0)


# --- Result format -------------------------------------------------------------

def test_result_dict_is_json_serializable_and_complete():
    result, _, _ = sample(settings(count=2, duration=5.0, timeout=1.0))
    data = json.loads(json.dumps(result.to_dict()))

    assert data["settings"] == {
        "sampling_frequency_hz": 2.0, "measurements_count": 2,
        "total_duration_seconds": 5.0, "request_timeout_seconds": 1.0,
    }
    assert data["counts"] == {"attempted": 2, "succeeded": 2, "failed": 0, "skipped_slots": 0}
    assert data["stop_reason"] == STOP_COUNT_REACHED
    assert set(data["attempts"][0]) == {
        "attempt", "slot", "scheduled_offset_s", "start_offset_s", "lateness_s", "elapsed_s",
        "effective_timeout_s", "timeout_clipped_by_deadline", "ok", "current_a",
    }
    assert "measurements" not in data and "successful_values" not in data  # values stored once


# --- Settings validation -------------------------------------------------------

@pytest.mark.parametrize(
    "field, value, message",
    [
        ("sampling_frequency_hz", 0, "positive"),
        ("sampling_frequency_hz", -1, "positive"),
        ("sampling_frequency_hz", math.nan, "finite"),
        ("sampling_frequency_hz", math.inf, "finite"),
        ("sampling_frequency_hz", "5", "must be a number"),
        ("sampling_frequency_hz", True, "must be a number"),
        ("sampling_frequency_hz", None, "must be a number"),
        ("sampling_frequency_hz", 1e-9, "between"),
        ("sampling_frequency_hz", 1e6, "between"),
        ("measurements_count", 0, "at least 1"),
        ("measurements_count", -1, "at least 1"),
        ("measurements_count", 2.5, "whole number"),
        ("measurements_count", 10.0, "whole number"),
        ("measurements_count", "10", "whole number"),
        ("measurements_count", True, "whole number"),
        ("total_duration_seconds", 0, "positive"),
        ("total_duration_seconds", math.nan, "finite"),
        ("total_duration_seconds", math.inf, "finite"),
        ("total_duration_seconds", "3", "must be a number"),
        ("total_duration_seconds", 1e12, "between"),
        ("request_timeout_seconds", 0, "positive"),
        ("request_timeout_seconds", math.inf, "finite"),
        ("request_timeout_seconds", False, "must be a number"),
        ("request_timeout_seconds", 1e9, "between"),
    ],
)
def test_invalid_settings_are_rejected_with_the_field_name(field, value, message):
    kwargs = {"sampling_frequency_hz": 2.0, "measurements_count": 5, "total_duration_seconds": 3.0}
    kwargs[field] = value

    with pytest.raises(ValueError, match=message) as excinfo:
        SamplingSettings(**kwargs)

    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    "frequency, duration, expected",
    [
        (2.0, 1.0, 2),    # targets 0, 0.5
        (2.0, 1.2, 3),    # targets 0, 0.5, 1.0 (D*f = 2.4 is not a whole number)
        (3.0, 1.0, 3),    # targets 0, 1/3, 2/3
        (10.0, 0.3, 3),   # 0.3 * 10 = 3.0000000000000004 must not add a fourth slot
        (1000.0, 0.0015, 2),
    ],
)
def test_duration_bound_counts_targets_inside_the_window(frequency, duration, expected):
    assert settings(frequency=frequency, duration=duration).max_potential_attempts == expected


@pytest.mark.parametrize("frequency, duration", [(2.0, 1.0), (2.0, 1.2), (3.0, 1.0), (10.0, 0.3), (7.0, 2.5)])
@pytest.mark.parametrize("start", [0.0, 1000.0, 123456.789])
def test_duration_bound_matches_the_attempts_a_run_actually_makes(frequency, duration, start):
    s = settings(frequency=frequency, duration=duration)
    result, _, _ = sample(s, start=start, default=(0.0, 1.0))  # instant requests: every slot is used

    assert len(result.attempts) == s.max_potential_attempts


def test_duration_only_settings_above_the_attempt_limit_are_rejected():
    # 1000 Hz for one week would allow 604,800,000 attempt records.
    with pytest.raises(ValueError, match="allow up to 604800000 attempts"):
        settings(frequency=1000.0, duration=604800.0)


def test_duration_bound_exactly_at_the_limit_is_accepted_and_one_more_is_rejected():
    assert settings(frequency=2.0, duration=500_000.0).max_potential_attempts == MAX_POTENTIAL_ATTEMPTS

    with pytest.raises(ValueError, match=f"allow up to {MAX_POTENTIAL_ATTEMPTS + 1} attempts"):
        settings(frequency=2.0, duration=500_000.5)


def test_count_only_limit_is_bounded():
    assert settings(count=MAX_POTENTIAL_ATTEMPTS).max_potential_attempts == MAX_POTENTIAL_ATTEMPTS

    with pytest.raises(ValueError, match="at most 1000000 are allowed"):
        settings(count=MAX_POTENTIAL_ATTEMPTS + 1)


def test_tighter_limit_applies_when_both_are_configured():
    # A large count is fine when the duration allows only 10 attempts, and vice versa.
    assert settings(frequency=1.0, count=2_000_000, duration=10.0).max_potential_attempts == 10
    assert settings(frequency=1000.0, count=5, duration=604800.0).max_potential_attempts == 5

    with pytest.raises(ValueError, match="allow up to"):
        settings(frequency=1000.0, count=2_000_000, duration=604800.0)


def test_at_least_one_stopping_limit_is_required():
    with pytest.raises(ValueError, match="at least one stopping limit"):
        SamplingSettings(sampling_frequency_hz=2.0)


def test_valid_numeric_settings_are_normalized_to_float():
    s = SamplingSettings(sampling_frequency_hz=2, measurements_count=3, total_duration_seconds=1,
                         request_timeout_seconds=1)

    assert (s.sampling_frequency_hz, s.total_duration_seconds, s.request_timeout_seconds) == (2.0, 1.0, 1.0)
    assert all(isinstance(v, float) for v in (s.sampling_frequency_hz, s.total_duration_seconds,
                                               s.request_timeout_seconds))
