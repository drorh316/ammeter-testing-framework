"""Unit tests for Ammeters.client using a fake socket and a fake clock.

No real network connections or sleeps are used; the emulators do not need to be running.
"""
import socket

import pytest

from Ammeters import client
from Ammeters.client import (
    AmmeterCommunicationError,
    AmmeterConnectionError,
    AmmeterTimeoutError,
    InvalidMeasurementError,
    request_current_from_ammeter,
)

PORT = 5001
COMMAND = b"MEASURE_GREENLEE -get_measurement"


class FakeSocket:
    """Socket stand-in: each recv() returns (or raises) the next scripted item.

    When the script runs out, recv() returns b"" (the server closed the connection).
    """

    def __init__(self, recv_script, on_recv=None):
        self._script = list(recv_script)
        self._on_recv = on_recv
        self.sent = b""
        self.timeouts = []
        self.recv_sizes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def sendall(self, data):
        self.sent += data

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, bufsize):
        self.recv_sizes.append(bufsize)
        if self._on_recv:
            self._on_recv()
        if not self._script:
            return b""
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item[:bufsize]


class FakeClock:
    """Replaces time.monotonic in the client; advanced explicitly by the test."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(client, "monotonic", fake)
    return fake


@pytest.fixture
def connect_to(monkeypatch):
    """Make create_connection return the given FakeSocket and record call arguments."""
    calls = []

    def install(fake_socket):
        def fake_create_connection(address, timeout=None):
            calls.append((address, timeout))
            return fake_socket

        monkeypatch.setattr(client, "create_connection", fake_create_connection)
        return calls

    return install


# --- Successful requests -------------------------------------------------------

def test_returns_float_and_sends_command(clock, connect_to):
    sock = FakeSocket([b"14.412814252540933"])
    calls = connect_to(sock)

    result = request_current_from_ammeter(PORT, COMMAND, timeout=2.0)

    assert isinstance(result, float)
    assert result == pytest.approx(14.412814252540933)
    assert sock.sent == COMMAND
    assert calls == [(("localhost", PORT), 2.0)]
    assert sock.closed


def test_reassembles_response_split_across_reads(clock, connect_to):
    sock = FakeSocket([b"0.04", b"3331", b"06383"])
    connect_to(sock)

    assert request_current_from_ammeter(PORT, COMMAND) == pytest.approx(0.0433310638)


def test_accepts_negative_and_exponent_values(clock, connect_to):
    connect_to(FakeSocket([b"-1.5e-3"]))

    assert request_current_from_ammeter(PORT, COMMAND) == pytest.approx(-0.0015)


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
def test_rejects_invalid_responses(clock, connect_to, payload, reason):
    connect_to(FakeSocket([payload] if payload else []))

    with pytest.raises(InvalidMeasurementError, match=reason) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert excinfo.value.port == PORT
    assert f"port {PORT}" in str(excinfo.value)


def test_rejects_response_larger_than_limit(clock, connect_to):
    # The server keeps sending 4-byte chunks and never closes; the limit is 10 bytes.
    sock = FakeSocket([b"1111", b"1111", b"1111", b"1111"])
    connect_to(sock)

    with pytest.raises(InvalidMeasurementError, match="exceeds 10 bytes"):
        request_current_from_ammeter(PORT, COMMAND, max_response_bytes=10)

    # Each read asks only for what is left up to limit + 1 bytes (11, then 7, then 3),
    # and reading stops as soon as 11 bytes (one past the limit) have arrived.
    assert sock.recv_sizes == [11, 7, 3]


def test_accepts_response_exactly_at_limit(clock, connect_to):
    connect_to(FakeSocket([b"12345.6789"]))  # 10 bytes

    assert request_current_from_ammeter(PORT, COMMAND, max_response_bytes=10) == 12345.6789


# --- Connection failures and timeouts ------------------------------------------

def test_connection_refused_raises_connection_error_with_cause(clock, monkeypatch):
    refused = ConnectionRefusedError(10061, "connection refused")

    def fake_create_connection(address, timeout=None):
        raise refused

    monkeypatch.setattr(client, "create_connection", fake_create_connection)

    with pytest.raises(AmmeterConnectionError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert excinfo.value.port == PORT
    assert f"port {PORT}" in str(excinfo.value)
    assert excinfo.value.__cause__ is refused


def test_connection_reset_during_receive_raises_connection_error(clock, connect_to):
    connect_to(FakeSocket([b"1.2", ConnectionResetError("reset by peer")]))

    with pytest.raises(AmmeterConnectionError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND)

    assert isinstance(excinfo.value.__cause__, ConnectionResetError)


def test_connect_timeout_raises_timeout_error(clock, monkeypatch):
    def fake_create_connection(address, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(client, "create_connection", fake_create_connection)

    with pytest.raises(AmmeterTimeoutError) as excinfo:
        request_current_from_ammeter(PORT, COMMAND, timeout=0.5)

    assert excinfo.value.port == PORT
    assert isinstance(excinfo.value.__cause__, socket.timeout)


def test_recv_timeout_raises_timeout_error(clock, connect_to):
    connect_to(FakeSocket([socket.timeout("timed out")]))

    with pytest.raises(AmmeterTimeoutError, match=f"port {PORT}"):
        request_current_from_ammeter(PORT, COMMAND)


def test_overall_deadline_stops_slow_partial_reads(clock, connect_to):
    # Each read returns one byte and takes 0.4 s of (fake) time; the server never closes.
    def advance_clock():
        clock.now += 0.4

    sock = FakeSocket([b"1"] * 100, on_recv=advance_clock)
    connect_to(sock)

    with pytest.raises(AmmeterTimeoutError):
        request_current_from_ammeter(PORT, COMMAND, timeout=1.0)

    # Reads at t=0, 0.4, 0.8 fit in the 1 s deadline; the next check at t=1.2 fails.
    assert len(sock.recv_sizes) == 3
    # Each read is given only the time remaining before the deadline.
    assert sock.timeouts == pytest.approx([1.0, 0.6, 0.2])


def test_all_errors_share_base_class():
    for error_type in (AmmeterConnectionError, AmmeterTimeoutError, InvalidMeasurementError):
        assert issubclass(error_type, AmmeterCommunicationError)


# --- Argument validation -------------------------------------------------------

@pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf"), None, "5", True])
def test_rejects_invalid_timeout(bad_timeout):
    with pytest.raises(ValueError, match="timeout"):
        request_current_from_ammeter(PORT, COMMAND, timeout=bad_timeout)


def test_rejects_non_positive_response_limit():
    with pytest.raises(ValueError, match="max_response_bytes"):
        request_current_from_ammeter(PORT, COMMAND, max_response_bytes=0)
