"""Loading and validating config/config.yaml."""
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Union

import yaml

from src.testing.sampling import SamplingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_AMMETER_HOST = "127.0.0.1"

_SAMPLING_KEYS = {
    "sampling_frequency_hz", "measurements_count", "total_duration_seconds", "request_timeout_seconds",
}
_AMMETER_KEYS = {"host", "port", "command"}


class ConfigError(ValueError):
    """The configuration file is missing, unreadable, or contains invalid settings."""


@dataclass(frozen=True)
class AmmeterDevice:
    name: str
    host: str
    port: int
    command: bytes


def load_config(config_path: Union[str, Path, None] = None) -> Dict[str, Any]:
    """Read the YAML configuration file and return it as a dictionary.

    Without ``config_path``, the project's config/config.yaml is used, independent of
    the current working directory. An explicit relative path is resolved as usual,
    against the current working directory.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"configuration file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"configuration file must contain a mapping at the top level: {path}")
    return data


def _section(data: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    current: Any = data
    for depth, key in enumerate(keys):
        name = ".".join(keys[:depth + 1])
        if not isinstance(current, dict) or key not in current:
            raise ConfigError(f"missing configuration section: {name}")
        current = current[key]
        if not isinstance(current, dict):
            raise ConfigError(f"configuration section {name} must be a mapping, got {current!r}")
    return current


def _reject_unknown_keys(section_name: str, section: Dict[str, Any], allowed: set) -> None:
    unknown = sorted(str(k) for k in section if k not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown setting(s) in {section_name}: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})")


def parse_sampling_settings(data: Dict[str, Any]) -> SamplingSettings:
    """Build validated SamplingSettings from the ``testing.sampling`` section."""
    section = _section(data, "testing", "sampling")
    _reject_unknown_keys("testing.sampling", section, _SAMPLING_KEYS)
    if section.get("sampling_frequency_hz") is None:
        raise ConfigError("testing.sampling.sampling_frequency_hz is required")
    try:
        return SamplingSettings(**{k: v for k, v in section.items() if v is not None})
    except ValueError as exc:
        raise ConfigError(f"testing.sampling: {exc}") from exc


def parse_ammeters(data: Dict[str, Any]) -> Dict[str, AmmeterDevice]:
    """Build validated AmmeterDevice entries from the ``ammeters`` section."""
    section = _section(data, "ammeters")
    if not section:
        raise ConfigError("ammeters must define at least one ammeter")

    devices = {}
    for name, entry in section.items():
        prefix = f"ammeters.{name}"
        if not isinstance(name, str) or not name:
            raise ConfigError(f"ammeter names must be non-empty strings, got {name!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"{prefix} must be a mapping with port and command, got {entry!r}")
        _reject_unknown_keys(prefix, entry, _AMMETER_KEYS)

        port = entry.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{prefix}.port must be a whole number from 1 to 65535, got {port!r}")

        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"{prefix}.command must be a non-empty string, got {command!r}")
        try:
            command_bytes = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConfigError(f"{prefix}.command must contain only ASCII characters, got {command!r}") from exc

        host = entry.get("host", DEFAULT_AMMETER_HOST)
        try:
            if not isinstance(host, str):
                raise ValueError
            ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise ConfigError(
                f"{prefix}.host must be a numeric IPv4 address such as '127.0.0.1', got {host!r}") from exc

        devices[name] = AmmeterDevice(name=name, host=host, port=port, command=command_bytes)
    return devices
