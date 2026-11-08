"""Deterministic fakes for scheduling tests: a manual clock, sleep and measurement."""


class FakeTime:
    """A clock that only moves when sleep() or a fake measurement advances it.

    ``wake_errors`` scripts how each sleep() call wakes: +0.003 wakes 3 ms late,
    -0.2 returns 0.2 s early. Unscripted sleeps wake exactly on time.
    """

    def __init__(self, start=0.0, wake_errors=()):
        self.now = start
        self.sleeps = []  # requested sleep durations, in call order
        self._wake_errors = list(wake_errors)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0, f"scheduler asked for a non-positive sleep: {seconds!r}"
        self.sleeps.append(seconds)
        self.now += seconds + (self._wake_errors.pop(0) if self._wake_errors else 0.0)


class EndsAt:
    """Scripted measurement outcome that finishes at an absolute clock time."""

    def __init__(self, at, outcome):
        self.at = at
        self.outcome = outcome


class FakeMeasure:
    """measure(timeout) stand-in that spends fake time and returns or raises.

    Each script item is ``(duration_s, outcome)`` or ``EndsAt(time, outcome)``; an
    outcome that is an exception is raised, anything else is returned. When the script
    runs out, ``default`` is used.
    """

    def __init__(self, fake_time, script=(), default=(0.1, 1.0)):
        self._time = fake_time
        self._script = list(script)
        self._default = default
        self.calls = []  # (start_time, timeout)

    def __call__(self, timeout):
        self.calls.append((self._time.now, timeout))
        item = self._script.pop(0) if self._script else self._default
        if isinstance(item, EndsAt):
            self._time.now = item.at
            outcome = item.outcome
        else:
            duration, outcome = item
            self._time.now += duration
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def starts(self):
        return [start for start, _ in self.calls]

    @property
    def timeouts(self):
        return [timeout for _, timeout in self.calls]
