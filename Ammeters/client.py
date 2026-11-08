"""TCP client for requesting a single current measurement from an ammeter emulator.

The emulators reply with the measurement as UTF-8 text and then close the
connection, so the client reads until the server closes and treats that as the
end of the response.

Only numeric IPv4 addresses are supported (the supplied emulators listen on IPv4
only). Hostnames, IPv6 and DNS resolution are deliberately not handled.
"""
import ipaddress
import math
import socket
from time import monotonic

DEFAULT_HOST = "127.0.0.1"
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
    """Connecting, sending and receiving together did not finish within the timeout budget."""


class InvalidMeasurementError(AmmeterCommunicationError):
    """The response was empty, too large, not valid text, or not a finite number."""


def request_current_from_ammeter(
    port: int,
    command: bytes,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    host: str = DEFAULT_HOST,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> float:
    """Send ``command`` to the ammeter at ``host``:``port`` and return the measured current in amperes.

    ``timeout`` is one shared budget, in seconds, for the whole network exchange:
    connecting, sending the command, and receiving the complete response. A single
    monotonic deadline is set before connecting, and each blocking socket operation
    is given only the time remaining. The total can still overrun slightly because of
    normal operating-system scheduling; it is not a hard real-time guarantee.

    ``host`` must be a numeric IPv4 address such as "127.0.0.1"; hostnames and IPv6
    are rejected with ValueError before any network activity.

    Raises AmmeterConnectionError, AmmeterTimeoutError or InvalidMeasurementError
    (all subclasses of AmmeterCommunicationError). The original exception, if any,
    is kept as ``__cause__``.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive, finite number of seconds, got {timeout!r}")
    if max_response_bytes <= 0:
        raise ValueError(f"max_response_bytes must be positive, got {max_response_bytes!r}")
    _validate_ipv4_host(host)

    deadline = monotonic() + timeout
    try:
        with _open_ipv4_socket() as sock:
            sock.settimeout(_remaining(deadline, "connecting"))
            sock.connect((host, port))
            sock.settimeout(_remaining(deadline, "sending the command"))
            sock.sendall(command)
            raw = _receive_until_closed(sock, port, deadline, max_response_bytes)
    except socket.timeout as exc:  # TimeoutError on Python 3.10+
        raise AmmeterTimeoutError(
            port, f"request did not complete within the {timeout} s timeout budget ({exc})"
        ) from exc
    except OSError as exc:
        raise AmmeterConnectionError(port, f"communication failed: {exc}") from exc

    return _parse_measurement(raw, port)


def _validate_ipv4_host(host) -> None:
    if not isinstance(host, str):
        raise ValueError(f"host must be a numeric IPv4 address string, got {host!r}")
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError(
            f"host must be a numeric IPv4 address such as '127.0.0.1' "
            f"(hostnames and IPv6 are not supported), got {host!r}"
        ) from exc


def _open_ipv4_socket() -> socket.socket:
    """Create the TCP/IPv4 socket. Kept separate so tests can substitute a fake socket."""
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def _remaining(deadline: float, operation: str) -> float:
    """Return the seconds left before ``deadline``, or raise socket.timeout if none are left."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise socket.timeout(f"timeout budget exhausted before {operation}")
    return remaining


def _receive_until_closed(sock, port: int, deadline: float, max_response_bytes: int) -> bytes:
    """Read until the server closes the connection, within the request's shared deadline."""
    data = bytearray()
    while True:
        sock.settimeout(_remaining(deadline, "receiving the response"))
        # Ask for at most one byte beyond the limit, so an oversized response is detected.
        chunk = sock.recv(min(_RECV_CHUNK_BYTES, max_response_bytes + 1 - len(data)))
        if not chunk:  # server closed the connection: response complete
            # The final read may have returned only after the deadline had passed.
            _remaining(deadline, "accepting the completed response")
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
