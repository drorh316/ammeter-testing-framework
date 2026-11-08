"""Unit tests for Ammeters.client using a fake socket and a fake clock.

No real network connections or sleeps are used; the emulators do not need to be running.
Time "passes" only when the fake socket advances the fake clock, which lets the tests
simulate slow connects, sends and receives deterministically.
"""
import inspect
import socket

import pytest

from Ammeters import client
from Ammeters.client import (
    DEFAULT_HOST,
    AmmeterCommunicationError,
    AmmeterConnectionError,
    AmmeterTimeoutError,
    InvalidMeasurementError,
    request_current_from_ammeter,
)

PORT = 5001
COMMAND = b"MEASURE_GREENLEE -get_measurement"


class FakeClock:
    """Replaces time.monotonic in the client; advanced only by the fake socket."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Slow:
    """A scripted recv() result that takes ``seconds`` of fake time before it is returned or raised."""

    def __init__(self, seconds, result):
        self.seconds = seconds
        self.result = result


class FakeSocket:
    """Stand-in for a TCP socket.

    - ``connect_delay`` / ``send_delay``: fake seconds the operation takes (it still completes).
    - ``connect_error`` / ``send_error``: exception raised by the operation instead.
    - ``recv_script``: items returned (or raised) by successive recv() calls; an item may be
      wrapped in Slow(seconds, item). When the script runs out, recv() returns b"" (server closed).

    The socket timeout in effect at each operation is recorded so tests can check the budget.
    """

    def __init__(self, clock, recv_script=(), connect_delay=0.0, connect_error=None,
                 send_delay=0.0, send_error=None):
        self._clock = clock
        self._script = list(recv_script)
        self._connect_delay = connect_delay
        self._connect_error = connect_error
        self._send_delay = send_delay
        self._send_error = send_error
        self._timeout = None
        self.connected_to = None
        self.sent = b""
        self.connect_timeout = None
        self.send_timeout = None
        self.recv_timeouts = []
        self.recv_sizes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def settimeout(self, value):
        self._timeout = value

    def connect(self, address):
        self.connect_timeout = self._timeout
        self._clock.now += self._connect_delay
        if self._connect_error:
            raise self._connect_error
        self.connected_to = address

    def sendall(self, data):
        self.send_timeout = self._timeout
        self._clock.now += self._send_delay
        if self._send_error:
            raise self._send_error
        self.sent += data

    def recv(self, bufsize):
        self.recv_timeouts.append(self._timeout)
        self.recv_sizes.append(bufsize)
        item = self._script.pop(0) if self._script else b""
        if isinstance(item, Slow):
            self._clock.now += item.seconds
            item = item.result
        if isinstance(item, BaseException):
            raise item
        return item[:bufsize]


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(client, "monotonic", fake)
    return fake


@pytest.fixture
def use_socket(monkeypatch):
    """Make the client use the given FakeSocket; returns a list counting socket creations."""
    created = []

    def install(fake_socket):
        def fake_open():
            created.append(fake_socket)
            return fake_socket

        monkeypatch.setattr(client, "_open_ipv4_socket", fake_open)
        return created

    return install


# --- Successful requests -------------------------------------------------------

def test_returns_float_and_sends_command(clock, use_socket):
    sock = FakeSocket(clock, [b"14.412814252540933"])
    use_socket(sock)

    result = request_current_from_ammeter(PORT, COMMAND, timeout=2.0)

    assert isinstance(result, float)
    assert result == pytest.approx(14.412814252540933)
    assert sock.connected_to == ("127.0.0.1", PORT)
    assert sock.sent == COMMAND
    assert sock.closed


def test_reassembles_response_split_across_reads(clock, use_socket):
    use_socket(FakeSocket(clock, [b"0.04", b"3331", b"06383"]))

    assert request_current_from_ammeter(PORT, COMMAND) == pytest.approx(0.0433310638)


def test_accepts_negative_and_exponent_values(clock, use_socket):
    use_socket(FakeSocket(clock, [b"-1.5e-3"]))

    assert request_current_from_ammeter(PORT, COMMAND) == pytest.approx(-0.0015)


# --- Host handling -------------------------------------------------------------

def test_default_host_is_ipv4_loopback(clock, use_socket):
    assert DEFAULT_HOST == "127.0.0.1"
    assert inspect.signature(request_current_from_ammeter).parameters["host"].default == "127.0.0.1"

    sock = FakeSocket(clock, [b"1.0"])
    use_socket(sock)
    request_current_from_ammeter(PORT, COMMAND)

    assert sock.connected_to == ("127.0.0.1", PORT)


def test_accepts_other_numeric_ipv4_host(clock, use_socket):
    sock = FakeSocket(clock, [b"1.0"])
    use_socket(sock)

    request_current_from_ammeter(PORT, COMMAND, host="192.168.1.20")

    assert sock.connected_to == ("192.168.1.20", PORT)


def test_real_socket_factory_creates_ipv4_tcp_socket():
    # Creating a socket performs no network activity; it is closed immediately.
    with client._open_ipv4_socket() as sock:
        assert sock.family == socket.AF_INET
        assert sock.type == socket.SOCK_STREAM


@pytest.mark.parametrize(
    "bad_host",
    ["localhost", "::1", "example.com", "127.1", "256.0.0.1", "127.0.0.1 ", "", None, 2130706433],
)
def test_rejects_unsupported_host_before_network_activity(clock, use_socket, bad_host):
    created = use_socket(FakeSocket(clock, [b"1.0"]))

    with pytest.raises(ValueError, match="IPv4"):
        request_current_from_ammeter(PORT, COMMAND, host=bad_host)

    assert created == []  # no socket was created


# --- Invalid responses ---------------------------------------------------------

@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"", "empty"),
        (b"\xff\xfe\x00", "UTF-8"),
        (b"not-a-number", "not a number"),
        (b"12.5 A", "not a number"),
        (b"nan", "finite"),
        (b"inf", "finite"),
        (b"-Infinity", "finite"),
    ],
)
def test_rejects_invalid_responses(clock, use_socket, payload, reason):
    sock = FakeSocket(clock, [payload] if payload else [])
    use_socket(sock)

    with pytest.raises(InvalidMeasurementError, match=reason) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert excinfo.value.port == PORT
    assert f"port {PORT}" in str(excinfo.value)
    assert sock.closed


def test_rejects_response_larger_than_limit(clock, use_socket):
    # The server keeps sending 4-byte chunks and never closes; the limit is 10 bytes.
    sock = FakeSocket(clock, [b"1111", b"1111", b"1111", b"1111"])
    use_socket(sock)

    with pytest.raises(InvalidMeasurementError, match="exceeds 10 bytes"):
        request_current_from_ammeter(PORT, COMMAND, max_response_bytes=10)

    # Each read asks only for what is left up to limit + 1 bytes (11, then 7, then 3),
    # and reading stops as soon as 11 bytes (one past the limit) have arrived.
    assert sock.recv_sizes == [11, 7, 3]
    assert sock.closed


def test_accepts_response_exactly_at_limit(clock, use_socket):
    use_socket(FakeSocket(clock, [b"12345.6789"]))  # 10 bytes

    assert request_current_from_ammeter(PORT, COMMAND, max_response_bytes=10) == 12345.6789


# --- Connection failures -------------------------------------------------------

def test_connection_refused_raises_connection_error_with_cause(clock, use_socket):
    refused = ConnectionRefusedError(10061, "connection refused")
    sock = FakeSocket(clock, connect_error=refused)
    use_socket(sock)

    with pytest.raises(AmmeterConnectionError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert excinfo.value.port == PORT
    assert f"port {PORT}" in str(excinfo.value)
    assert excinfo.value.__cause__ is refused
    assert sock.closed


def test_connection_reset_during_receive_raises_connection_error(clock, use_socket):
    sock = FakeSocket(clock, [b"1.2", ConnectionResetError("reset by peer")])
    use_socket(sock)

    with pytest.raises(AmmeterConnectionError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert isinstance(excinfo.value.__cause__, ConnectionResetError)
    assert sock.closed


def test_all_errors_share_base_class():
    for error_type in (AmmeterConnectionError, AmmeterTimeoutError, InvalidMeasurementError):
        assert issubclass(error_type, AmmeterCommunicationError)


# --- Shared timeout budget -----------------------------------------------------

def test_each_phase_gets_only_the_remaining_budget(clock, use_socket):
    # Connecting takes 0.3 s and sending 0.2 s of a 1.0 s budget, leaving 0.5 s to receive.
    sock = FakeSocket(clock, [b"1.5"], connect_delay=0.3, send_delay=0.2)
    use_socket(sock)

    assert request_current_from_ammeter(PORT, COMMAND, timeout=1.0) == 1.5

    assert sock.connect_timeout == pytest.approx(1.0)
    assert sock.send_timeout == pytest.approx(0.7)
    # Both reads (the data, then the server closing) get the 0.5 s left after connect + send.
    assert sock.recv_timeouts == pytest.approx([0.5, 0.5])


def test_slow_partial_reads_share_the_same_deadline(clock, use_socket):
    # After a 0.2 s connect, each 1-byte read takes 0.25 s; the server never closes.
    sock = FakeSocket(clock, [Slow(0.25, b"1")] * 100, connect_delay=0.2)
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match="receiving the response"):
        request_current_from_ammeter(PORT, COMMAND, timeout=1.0)

    # Reads start at t=0.2, 0.45, 0.7, 0.95; the check at t=1.2 finds the budget exhausted.
    assert sock.recv_timeouts == pytest.approx([0.8, 0.55, 0.3, 0.05])
    assert sock.closed


def test_real_connect_timeout_raises_timeout_error(clock, use_socket):
    sock = FakeSocket(clock, connect_delay=0.5, connect_error=socket.timeout("timed out"))
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match="0.5 s timeout budget") as excinfo:
        request_current_from_ammeter(PORT, COMMAND, timeout=0.5)

    assert excinfo.value.port == PORT
    assert isinstance(excinfo.value.__cause__, socket.timeout)
    assert sock.closed


def test_connect_finishing_after_deadline_stops_before_sending(clock, use_socket):
    # connect() succeeds, but only after the whole 1.0 s budget has been used.
    sock = FakeSocket(clock, [b"1.5"], connect_delay=1.0)
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match="before sending the command"):
        request_current_from_ammeter(PORT, COMMAND, timeout=1.0)

    assert sock.send_timeout is None and sock.sent == b""  # sendall was never called
    assert sock.closed


def test_real_send_timeout_raises_timeout_error(clock, use_socket):
    sock = FakeSocket(clock, send_delay=0.4, send_error=socket.timeout("timed out"))
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND, timeout=0.4)

    assert isinstance(excinfo.value.__cause__, socket.timeout)
    assert sock.closed


def test_send_finishing_after_deadline_stops_before_receiving(clock, use_socket):
    # Connecting takes 0.4 s, then sendall() completes only at t=1.1 s, past the 1.0 s budget.
    sock = FakeSocket(clock, [b"1.5"], connect_delay=0.4, send_delay=0.7)
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match="before receiving the response"):
        request_current_from_ammeter(PORT, COMMAND, timeout=1.0)

    assert sock.send_timeout == pytest.approx(0.6)
    assert sock.recv_timeouts == []  # recv was never called
    assert sock.closed


def test_real_recv_timeout_raises_timeout_error(clock, use_socket):
    sock = FakeSocket(clock, [Slow(0.3, socket.timeout("timed out"))], connect_delay=0.2)
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match=f"port {PORT}"):
        request_current_from_ammeter(PORT, COMMAND, timeout=0.5)

    assert sock.recv_timeouts == pytest.approx([0.3])
    assert sock.closed


def test_completed_response_arriving_after_deadline_is_rejected(clock, use_socket):
    # The full value arrives in time, but the server's close (end of response) is only
    # seen at t=0.6 s, after the 0.5 s budget, so the response is not accepted.
    sock = FakeSocket(clock, [b"1.5", Slow(0.6, b"")])
    use_socket(sock)

    with pytest.raises(AmmeterTimeoutError, match="accepting the completed response"):
        request_current_from_ammeter(PORT, COMMAND, timeout=0.5)

    assert sock.closed


def test_response_completed_just_before_deadline_is_accepted(clock, use_socket):
    sock = FakeSocket(clock, [b"1.5", Slow(0.49, b"")])
    use_socket(sock)

    assert request_current_from_ammeter(PORT, COMMAND, timeout=0.5) == 1.5


# --- Argument validation -------------------------------------------------------

@pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf"), None, "5", True])
def test_rejects_invalid_timeout(bad_timeout):
    with pytest.raises(ValueError, match="timeout"):
        request_current_from_ammeter(PORT, COMMAND, timeout=bad_timeout)


def test_rejects_non_positive_response_limit():
    with pytest.raises(ValueError, match="max_response_bytes"):
        request_current_from_ammeter(PORT, COMMAND, max_response_bytes=0)
