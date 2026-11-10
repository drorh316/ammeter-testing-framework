"""A loopback TCP server that injects one planned fault per connection.

It is used only by the fault-simulation demo (``examples/fault_simulation.py``) and its
tests; the supplied emulators, ``main.py`` and the normal sampling demo never use it.

Behaviour
---------
- Listens on ``127.0.0.1`` on a port chosen by the operating system (``.port``).
- Successful replies use the real emulator logic: the emulator object's
  ``get_current_command`` and ``measure_current()`` (Greenlee by default), encoded the same
  way as ``AmmeterEmulatorBase.start_server`` does.
- Connections are numbered 1, 2, 3, ... **in the order they are accepted**, by the accepting
  thread, before a handler thread is started. Connection *n* gets step *n* of the scenario;
  any further connection is logged as unplanned and closed without a reply.
- Each connection has its own handler thread, so a delayed reply never holds up the next
  connection.

Bounded lifecycle
-----------------
- Reading the command: the handler reads until it has exactly ``len(command)`` bytes,
  across as many partial reads as TCP delivers, never more than that, within
  ``command_timeout_s``. Bytes that are not a prefix of the command end the read at once.
- Writing a reply: ``sendall`` with ``write_timeout_s``.
- The delay waits on the stop event, so cleanup interrupts it immediately. If the delay
  runs out, the reading is taken (an emulator error is recorded like any handler error) and
  the late reply is sent; only a ``ConnectionError`` from that send (the client has already
  given up and closed) is treated as expected and not recorded.
- All blocking socket calls use short timeouts and check the stop event, so cleanup is
  noticed within about POLL_INTERVAL_S.
- ``close()`` (also called by ``__exit__``, and by ``start()`` when startup fails) sets the
  stop event, joins the accepting thread and every handler thread against **one** deadline
  (``cleanup_timeout_s``), and closes the listening socket. It never waits without a limit.
  ``cleanup["complete"]`` is True only if every thread has stopped and every socket is closed.
- Unexpected exceptions in the accepting or handler threads are recorded in ``errors`` (and
  on the connection's log entry); the demo treats any of them as a failed scenario.
- Threads are daemon threads only as a last resort for the interpreter's exit; cleanup does
  not rely on that.

Readiness is an event set by the accepting thread when its loop starts; no probe connection
is made, so readiness never uses up a scenario connection.
"""
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from src.testing.fault_scenario import (
    COMMAND_INCOMPLETE,
    COMMAND_OK,
    COMMAND_WRONG,
    FAULT_CLOSE,
    FAULT_DELAY,
    FAULT_MALFORMED,
    FAULT_OK,
    MALFORMED_REPLY,
    Step,
)

HOST = "127.0.0.1"
POLL_INTERVAL_S = 0.05
DEFAULT_COMMAND_TIMEOUT_S = 2.0
DEFAULT_WRITE_TIMEOUT_S = 2.0
DEFAULT_READY_TIMEOUT_S = 5.0
DEFAULT_CLEANUP_TIMEOUT_S = 5.0
UNPLANNED = "unplanned"


class FaultServerError(Exception):
    """The fault server could not be started."""


def read_command(conn, expected: bytes, deadline: float, stop: threading.Event,
                 clock=time.monotonic) -> tuple:
    """Read exactly ``len(expected)`` bytes from ``conn``, across partial reads.

    Returns ``(status, data)``: status is COMMAND_OK, COMMAND_WRONG (the bytes so far are not
    a prefix of ``expected``) or COMMAND_INCOMPLETE (the client closed the connection, the
    deadline passed, or ``stop`` was set first). Never reads more than ``len(expected)`` bytes.
    """
    data = b""
    while len(data) < len(expected):
        if stop.is_set():
            return COMMAND_INCOMPLETE, data
        remaining = deadline - clock()
        if remaining <= 0:
            return COMMAND_INCOMPLETE, data
        conn.settimeout(min(POLL_INTERVAL_S, remaining))
        try:
            chunk = conn.recv(len(expected) - len(data))
        except socket.timeout:
            continue
        if not chunk:
            return COMMAND_INCOMPLETE, data
        data += chunk
        if not expected.startswith(data):
            return COMMAND_WRONG, data
    return COMMAND_OK, data


class FaultSimulationServer:
    """Serve ``steps`` (one per accepted connection) with the emulator's measurement logic."""

    def __init__(self, steps: Sequence[Step], *, delay_s: float, emulator=None, port: int = 0,
                 command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
                 write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
                 ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
                 cleanup_timeout_s: float = DEFAULT_CLEANUP_TIMEOUT_S):
        if not delay_s > 0:
            raise ValueError(f"delay_s must be positive, got {delay_s!r}")
        self.steps = tuple(steps)
        self.delay_s = delay_s
        self.emulator = emulator if emulator is not None else GreenleeAmmeter(0)  # port unused: never started
        self.command = self.emulator.get_current_command
        self.requested_port = port
        self.command_timeout_s = command_timeout_s
        self.write_timeout_s = write_timeout_s
        self.ready_timeout_s = ready_timeout_s
        self.cleanup_timeout_s = cleanup_timeout_s
        self.port: Optional[int] = None
        self.cleanup: Optional[Dict[str, Any]] = None

        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        self._handlers: List[threading.Thread] = []
        self._sockets: List[socket.socket] = []
        self._errors: List[Dict[str, Any]] = []
        self._started = False

    # --- Public view of the log --------------------------------------------------------

    @property
    def settings(self) -> Dict[str, Any]:
        return {"host": HOST, "port": self.port, "command": self.command.decode("ascii"),
                "delay_s": self.delay_s, "command_timeout_s": self.command_timeout_s,
                "write_timeout_s": self.write_timeout_s, "cleanup_timeout_s": self.cleanup_timeout_s}

    def connections(self) -> List[Dict[str, Any]]:
        """A snapshot of the connection log, in acceptance order."""
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def errors(self) -> List[Dict[str, Any]]:
        """Unexpected exceptions in the accepting or handler threads."""
        with self._lock:
            return [dict(error) for error in self._errors]

    # --- Lifecycle -----------------------------------------------------------------------

    def __enter__(self) -> "FaultSimulationServer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> "FaultSimulationServer":
        """Bind, listen and start accepting; wait (bounded) until the accept loop runs.

        On any failure everything started so far is cleaned up before the error is raised.
        """
        if self._started:
            raise FaultServerError("the server can only be started once")
        self._started = True
        try:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.bind((HOST, self.requested_port))
            self._listener.listen()
            self._listener.settimeout(POLL_INTERVAL_S)
            self.port = self._listener.getsockname()[1]
            thread = threading.Thread(target=self._accept_loop, name="fault-sim-accept", daemon=True)
            thread.start()
            self._accept_thread = thread  # only a started thread is joined during cleanup
            if not self._ready.wait(self.ready_timeout_s):
                raise FaultServerError(f"the accept loop did not start within {self.ready_timeout_s} s")
        except BaseException as exc:
            self.close()
            if isinstance(exc, FaultServerError):
                raise
            if isinstance(exc, Exception):
                raise FaultServerError(f"could not start the fault server: {type(exc).__name__}: {exc}") from exc
            raise
        return self

    def close(self) -> Dict[str, Any]:
        """Stop everything against one deadline and report what actually stopped (idempotent)."""
        if self.cleanup is not None:
            return self.cleanup
        started = time.monotonic()
        deadline = started + self.cleanup_timeout_s
        self._stop.set()
        if self._accept_thread is not None:
            self._accept_thread.join(max(0.0, deadline - time.monotonic()))
        accept_stopped = self._accept_thread is None or not self._accept_thread.is_alive()
        if self._listener is not None:
            self._listener.close()
        with self._lock:  # no new handler can be added once the accept thread has stopped
            handlers = list(self._handlers)
            sockets = list(self._sockets)
        for thread in handlers:
            thread.join(max(0.0, deadline - time.monotonic()))
        handlers_stopped = sum(1 for thread in handlers if not thread.is_alive())
        sockets_closed = sum(1 for sock in sockets if sock.fileno() == -1)
        listener_closed = self._listener is None or self._listener.fileno() == -1
        self.cleanup = {
            "complete": accept_stopped and listener_closed and handlers_stopped == len(handlers)
                        and sockets_closed == len(sockets),
            "accept_thread_stopped": accept_stopped,
            "listener_closed": listener_closed,
            "handlers": len(handlers),
            "handlers_stopped": handlers_stopped,
            "connection_sockets": len(sockets),
            "connection_sockets_closed": sockets_closed,
            "elapsed_s": time.monotonic() - started,
        }
        return self.cleanup

    # --- Threads -------------------------------------------------------------------------

    def _record_error(self, where: str, exc: BaseException, entry: Optional[Dict[str, Any]] = None) -> None:
        error = {"where": where, "type": type(exc).__name__, "message": str(exc)}
        with self._lock:
            self._errors.append(error)
            if entry is not None:
                entry["error"] = {"type": error["type"], "message": error["message"]}

    def _accept_loop(self) -> None:
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                self._dispatch(conn)
        except Exception as exc:  # unexpected (e.g. the listener failed): record it, stop accepting
            if not self._stop.is_set():
                self._record_error("accept loop", exc)

    def _dispatch(self, conn: socket.socket) -> None:
        """Number the connection in acceptance order, log it, and start its handler."""
        with self._lock:
            number = len(self._entries) + 1
            step = self.steps[number - 1] if number <= len(self.steps) else None
            entry = {"connection": number, "fault": step.fault if step else UNPLANNED, "command": None,
                     "reply_sent": False, "delay_interrupted": None, "finished": False,
                     "socket_closed": False, "error": None}
            self._entries.append(entry)
            self._sockets.append(conn)
            thread = threading.Thread(target=self._handle, args=(conn, entry, step),
                                      name=f"fault-sim-connection-{number}", daemon=True)
            self._handlers.append(thread)
        try:
            thread.start()
        except Exception as exc:
            conn.close()
            with self._lock:
                self._handlers.remove(thread)
                entry["finished"] = True
                entry["socket_closed"] = conn.fileno() == -1
            self._record_error(f"connection {number}", exc, entry)

    def _handle(self, conn: socket.socket, entry: Dict[str, Any], step: Optional[Step]) -> None:
        try:
            status, _ = read_command(conn, self.command, time.monotonic() + self.command_timeout_s, self._stop)
            with self._lock:
                entry["command"] = status
            if status != COMMAND_OK or step is None:
                return  # close without a reply, like the emulator does for an unknown command
            if step.fault == FAULT_OK:
                self._send(conn, entry, self._reading())
            elif step.fault == FAULT_MALFORMED:
                self._send(conn, entry, MALFORMED_REPLY)
            elif step.fault == FAULT_DELAY:
                interrupted = self._stop.wait(self.delay_s)
                with self._lock:
                    entry["delay_interrupted"] = interrupted
                if not interrupted:
                    reply = self._reading()  # emulator errors go to the normal handler-error path
                    try:
                        self._send(conn, entry, reply)
                    except ConnectionError:
                        # Expected: the client gave up after its timeout and closed, so the late
                        # reply meets a reset, aborted or broken connection. Any other error
                        # (e.g. a write timeout) is unexpected and is recorded below.
                        pass
            elif step.fault == FAULT_CLOSE:
                pass  # orderly close below: the client sees end-of-stream, not a reset
        except Exception as exc:
            self._record_error(f"connection {entry['connection']}", exc, entry)
        finally:
            conn.close()
            with self._lock:
                entry["socket_closed"] = conn.fileno() == -1
                entry["finished"] = True

    def _reading(self) -> bytes:
        return str(self.emulator.measure_current()).encode("utf-8")  # as AmmeterEmulatorBase does

    def _send(self, conn: socket.socket, entry: Dict[str, Any], reply: bytes) -> None:
        conn.settimeout(self.write_timeout_s)
        conn.sendall(reply)
        with self._lock:
            entry["reply_sent"] = True
