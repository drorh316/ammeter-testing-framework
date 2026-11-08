"""Test-wide safeguards."""
import pytest

from src.testing import archive
from src.utils.config import load_config, parse_result_management

_shipped = parse_result_management(load_config())
SHIPPED_DATABASE = _shipped.database_path.resolve() if _shipped.database_path is not None else None


@pytest.fixture(autouse=True)
def protect_shipped_results_database(monkeypatch):
    """Fail any test that would save to, or open, the results database of the shipped config.

    The shipped config enables archiving, so a test that ran AmmeterTestFramework() with the
    real config would otherwise write to results/. Tests must use databases in tmp_path.
    """
    if SHIPPED_DATABASE is None:
        return

    def is_shipped(path):
        return path.resolve() == SHIPPED_DATABASE

    original_save = archive.ResultArchive.save_run
    original_connect = archive._connect

    def guarded_save(self, result):
        assert not is_shipped(self.path), f"a test tried to write the shipped results database {SHIPPED_DATABASE}"
        return original_save(self, result)

    def guarded_connect(path, mode, timeout):
        assert not is_shipped(path), f"a test tried to open the shipped results database {SHIPPED_DATABASE}"
        return original_connect(path, mode, timeout)

    monkeypatch.setattr(archive.ResultArchive, "save_run", guarded_save)
    monkeypatch.setattr(archive, "_connect", guarded_connect)
