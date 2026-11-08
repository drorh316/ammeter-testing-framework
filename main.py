import sys
import threading
import time

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.client import AmmeterCommunicationError, request_current_from_ammeter


def run_greenlee_emulator():
    greenlee = GreenleeAmmeter(5001)
    greenlee.start_server()

def run_entes_emulator():
    entes = EntesAmmeter(5002)
    entes.start_server()

def run_circutor_emulator():
    circutor = CircutorAmmeter(5003)
    circutor.start_server()

if __name__ == "__main__":
    # Start each ammeter in a separate thread
    threading.Thread(target=run_greenlee_emulator, daemon=True).start()
    threading.Thread(target=run_entes_emulator, daemon=True).start()
    threading.Thread(target=run_circutor_emulator, daemon=True).start()

    # Fixed delay to let the servers bind and start listening before the requests are sent.
    time.sleep(5)

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

    sys.exit(1 if failures else 0)
