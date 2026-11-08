"""TCP client for requesting a single current measurement from an ammeter emulator.

The emulators reply with the measurement as UTF-8 text and then close the
connection, so the client reads until the server closes and treats that as the
end of the response.
"""
import math
import socket
from socket import create_connection
from time import monotonic

DEFAULT_HOST = "localhost"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RESPONSE_BYTES = 1024
_RECV_CHUNK_BYTES = 256


class AmmeterCommunicationError(Exception):
    """Base error for a failed measurement request. ``port`` identifies the ammeter."""

    def __init__(self, port: int, message: str):
        super().__init__(f"Ammeter on port {port}: {message}")
        self.port = port


class AmmeterConnectionError(AmmeterCommunicationError):
    """The connection could not be opened or failed during the exchange."""


class AmmeterTimeoutError(AmmeterCommunicationError):
    """Connecting, sending, or receiving the full response took longer than the timeout."""


class InvalidMeasurementError(AmmeterCommunicationError):
    """The response was empty, too large, not valid text, or not a finite number."""


def request_current_from_ammeter(
    port: int,
    command: bytes,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    host: str = DEFAULT_HOST,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> float:
    """Send ``command`` to the ammeter on ``port`` and return the measured current in amperes.

    ``timeout`` (seconds) applies to connecting and sending, and is also the overall
    deadline for receiving the complete response, however many reads it takes.

    Raises AmmeterConnectionError, AmmeterTimeoutError or InvalidMeasurementError
    (all subclasses of AmmeterCommunicationError). The original exception, if any,
    is kept as ``__cause__``.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive, finite number of seconds, got {timeout!r}")
    if max_response_bytes <= 0:
        raise ValueError(f"max_response_bytes must be positive, got {max_response_bytes!r}")

    try:
        with create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(command)
            raw = _receive_until_closed(sock, port, timeout, max_response_bytes)
    except socket.timeout as exc:  # TimeoutError on Python 3.10+
        raise AmmeterTimeoutError(port, f"no complete response within {timeout} s") from exc
    except OSError as exc:
        raise AmmeterConnectionError(port, f"communication failed: {exc}") from exc

    return _parse_measurement(raw, port)


def _receive_until_closed(sock, port: int, timeout: float, max_response_bytes: int) -> bytes:
    """Read until the server closes the connection, within one overall deadline."""
    deadline = monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise socket.timeout("overall receive deadline exceeded")
        sock.settimeout(remaining)
        # Ask for at most one byte beyond the limit, so an oversized response is detected.
        chunk = sock.recv(min(_RECV_CHUNK_BYTES, max_response_bytes + 1 - len(data)))
        if not chunk:  # server closed the connection: response complete
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_response_bytes:
            raise InvalidMeasurementError(port, f"response exceeds {max_response_bytes} bytes")


def _parse_measurement(raw: bytes, port: int) -> float:
    if not raw:
        raise InvalidMeasurementError(port, "empty response (command may not be recognised)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidMeasurementError(port, "response is not valid UTF-8 text") from exc
    try:
        value = float(text)
    except ValueError as exc:
        raise InvalidMeasurementError(port, f"response is not a number: {text!r}") from exc
    if not math.isfinite(value):
        raise InvalidMeasurementError(port, f"response is not a finite number: {text!r}")
    return value
