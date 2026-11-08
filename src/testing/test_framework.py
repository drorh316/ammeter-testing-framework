"""Unified entry point for sampling any configured ammeter."""
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from Ammeters.client import request_current_from_ammeter
from src.testing.analysis import summarize_measurements
from src.testing.sampling import run_sampling
from ..utils.config import load_config, parse_ammeters, parse_sampling_settings


class AmmeterTestFramework:
    """Samples one configured ammeter per ``run_test`` call.

    The whole configuration (sampling settings and every ammeter entry) is validated in
    the constructor, before any network activity. ``request``, ``clock`` and ``sleep``
    can be replaced in tests; by default the real client, ``time.perf_counter`` (a
    monotonic, high-resolution clock) and ``time.sleep`` are used.
    """

    def __init__(
        self,
        config_path: Union[str, Path, None] = None,
        *,
        request: Callable[..., float] = request_current_from_ammeter,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = load_config(config_path)
        self.sampling = parse_sampling_settings(self.config)
        self.ammeters = parse_ammeters(self.config)
        self._request = request
        self._clock = clock
        self._sleep = sleep

    def run_test(self, ammeter_type: str) -> Dict[str, Any]:
        """Sample the ammeter named ``ammeter_type`` (a key of the ``ammeters`` config section).

        Returns a JSON-compatible dict: the device, the effective sampling settings,
        counts, elapsed time, stop reason, one record per attempt, and an ``analysis``
        section with statistics of the successful measurements only.
        """
        device = self.ammeters.get(ammeter_type)
        if device is None:
            raise ValueError(
                f"unknown ammeter type {ammeter_type!r}; configured types: {', '.join(self.ammeters)}")

        def measure(timeout: float) -> float:
            return self._request(device.port, device.command, timeout=timeout, host=device.host)

        result = run_sampling(measure, self.sampling, clock=self._clock, sleep=self._sleep)
        return {
            "ammeter": device.name,
            "host": device.host,
            "port": device.port,
            "command": device.command.decode("ascii"),
            **result.to_dict(),
            # Failed attempts have no value and are excluded, never counted as 0 A.
            "analysis": summarize_measurements(result.successful_values),
        }
