import sys
import threading
import time
from typing import Callable, Dict, List, Sequence, Tuple

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.client import AmmeterCommunicationError, request_current_from_ammeter

# Fixed delay to let the servers bind and start listening before the requests are sent.
STARTUP_DELAY_SECONDS = 5


def run_greenlee_emulator():
    greenlee = GreenleeAmmeter(5001)
    greenlee.start_server()

def run_entes_emulator():
    entes = EntesAmmeter(5002)
    entes.start_server()

def run_circutor_emulator():
    circutor = CircutorAmmeter(5003)
    circutor.start_server()


EMULATORS: Tuple[Tuple[str, Callable[[], None]], ...] = (
    ("Greenlee", run_greenlee_emulator),
    ("ENTES", run_entes_emulator),
    ("CIRCUTOR", run_circutor_emulator),
)


def start_emulators(emulators: Sequence[Tuple[str, Callable[[], None]]] = EMULATORS,
                    delay_s: float = STARTUP_DELAY_SECONDS, *,
                    sleep: Callable[[float], None] = time.sleep) -> Dict[str, threading.Thread]:
    """Start each emulator in its own daemon thread, then wait ``delay_s`` seconds."""
    threads = {name: threading.Thread(target=target, name=f"{name} emulator", daemon=True)
               for name, target in emulators}
    for thread in threads.values():
        thread.start()
    sleep(delay_s)
    return threads


def stopped_emulators(threads: Dict[str, threading.Thread]) -> List[str]:
    """Names of emulators whose thread has already ended.

    An emulator's server loop never returns, so an ended thread means its startup or
    request handling crashed, for example because its port was in use (the thread's
    traceback is printed to stderr). A thread that is still alive is not proof that its
    server is ready to accept connections.
    """
    return [name for name, thread in threads.items() if not thread.is_alive()]


def main() -> int:
    # Start each ammeter in a separate thread
    stopped = stopped_emulators(start_emulators())
    if stopped:
        print(f"emulator(s) failed to start: {', '.join(stopped)} (see the traceback above)", file=sys.stderr)
        return 1

    # Each emulator only responds when the request matches its full command exactly
    # (see get_current_command in the Ammeters/*_Ammeter.py classes).
    requests = [
        ("Greenlee", 5001, b'MEASURE_GREENLEE -get_measurement'),
        ("ENTES", 5002, b'MEASURE_ENTES -get_data'),
        ("CIRCUTOR", 5003, b'MEASURE_CIRCUTOR -get_measurement -current'),
    ]

    failures = 0
    for name, port, command in requests:
        try:
            current = request_current_from_ammeter(port, command)
        except AmmeterCommunicationError as error:
            failures += 1
            print(f"{name} measurement FAILED: {error}", file=sys.stderr)
        else:
            print(f"Received current measurement from {name} (port {port}): {current} A")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
