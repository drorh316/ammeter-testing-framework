"""Unified entry point for sampling any configured ammeter."""
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Union

from Ammeters.client import request_current_from_ammeter
from src.testing.analysis import AnalysisError, failed_analysis, summarize_measurements
from src.testing.archive import ArchiveError, ResultArchive, ResultArchiveError, utc_timestamp
from src.testing.sampling import run_sampling
from ..utils.config import load_config, parse_ammeters, parse_result_management, parse_sampling_settings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    return str(uuid.uuid4())


class AmmeterTestFramework:
    """Samples one configured ammeter per ``run_test`` call.

    The whole configuration (sampling settings, every ammeter entry and result
    management) is validated in the constructor, before any network activity. If
    archiving is enabled, every result is saved to the SQLite archive (``self.archive``).

    ``request``, ``clock``, ``sleep``, ``now`` and ``new_run_id`` can be replaced in tests;
    by default the real client, ``time.perf_counter`` (a monotonic, high-resolution clock),
    ``time.sleep``, the current UTC time and random UUID4 run IDs are used.
    """

    def __init__(
        self,
        config_path: Union[str, Path, None] = None,
        *,
        request: Callable[..., float] = request_current_from_ammeter,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
        new_run_id: Callable[[], str] = _new_run_id,
    ):
        self.config = load_config(config_path)
        self.sampling = parse_sampling_settings(self.config)
        self.ammeters = parse_ammeters(self.config)
        self.archive_settings = parse_result_management(self.config)
        self.archive = ResultArchive(self.archive_settings.database_path) if self.archive_settings.enabled else None
        self._request = request
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._new_run_id = new_run_id

    def run_test(self, ammeter_type: str) -> Dict[str, Any]:
        """Sample the ammeter named ``ammeter_type`` (a key of the ``ammeters`` config section).

        Returns a JSON-compatible dict: the run ID, UTC start and end times, the device,
        the effective sampling settings, counts, elapsed time, stop reason, one record per
        attempt, and an ``analysis`` section with statistics of the successful measurements
        only. If the statistics cannot be calculated (AnalysisError), the analysis status
        is "error" and every measurement is kept.

        If archiving is enabled and the result cannot be saved, ResultArchiveError is raised;
        its ``result`` attribute holds the complete result.
        """
        device = self.ammeters.get(ammeter_type)
        if device is None:
            raise ValueError(
                f"unknown ammeter type {ammeter_type!r}; configured types: {', '.join(self.ammeters)}")

        def measure(timeout: float) -> float:
            return self._request(device.port, device.command, timeout=timeout, host=device.host)

        run_id = self._new_run_id()
        started_at = utc_timestamp(self._now())
        sampling = run_sampling(measure, self.sampling, clock=self._clock, sleep=self._sleep)
        ended_at = utc_timestamp(self._now())

        # Failed attempts have no value and are excluded, never counted as 0 A.
        values = sampling.successful_values
        try:
            analysis = summarize_measurements(values)
        except AnalysisError as exc:  # only the expected calculation failure; other errors propagate
            analysis = failed_analysis(len(values), exc)

        result = {
            "run_id": run_id,
            "ammeter": device.name,
            "host": device.host,
            "port": device.port,
            "command": device.command.decode("ascii"),
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            **sampling.to_dict(),
            "analysis": analysis,
        }

        if self.archive is not None:
            try:
                self.archive.save_run(result)
            except ArchiveError as exc:
                raise ResultArchiveError(
                    f"run {run_id} ({device.name}) was collected but not archived in {self.archive.path}: {exc}",
                    result,
                ) from exc
        return result
