"""Sampling demo: sample every configured ammeter from the running emulators.

Run from the project root:

    python -m examples.run_tests

It validates config/config.yaml, starts the three emulators with the same functions and
fixed startup delay as main.py, runs AmmeterTestFramework.run_test() for each configured
ammeter, and prints a summary per run. The emulators print their own lines while sampling.
"""
import sys
import threading
import time

from main import run_circutor_emulator, run_entes_emulator, run_greenlee_emulator
from src.testing.test_framework import AmmeterTestFramework

STARTUP_DELAY_SECONDS = 5  # same fixed delay as main.py


def main() -> int:
    # Load and validate the configuration before starting anything.
    framework = AmmeterTestFramework()

    for start_emulator in (run_greenlee_emulator, run_entes_emulator, run_circutor_emulator):
        threading.Thread(target=start_emulator, daemon=True).start()
    time.sleep(STARTUP_DELAY_SECONDS)

    results = {name: framework.run_test(name) for name in framework.ammeters}

    settings = framework.sampling
    print("\n=== Sampling summary ===")
    print(f"frequency {settings.sampling_frequency_hz} Hz, count limit {settings.measurements_count}, "
          f"duration limit {settings.total_duration_seconds} s, request timeout {settings.request_timeout_seconds} s")
    any_failed = False
    for name, result in results.items():
        counts = result["counts"]
        any_failed = any_failed or counts["failed"] > 0
        lateness_ms = [a["lateness_s"] * 1000 for a in result["attempts"]]
        print(f"\n{name} (port {result['port']}): stop reason {result['stop_reason']}, "
              f"elapsed {result['elapsed_s']:.3f} s")
        print(f"  attempted {counts['attempted']}, succeeded {counts['succeeded']}, "
              f"failed {counts['failed']}, skipped slots {counts['skipped_slots']}")
        print(f"  start lateness vs. target: min {min(lateness_ms):.3f} ms, max {max(lateness_ms):.3f} ms")
        for a in result["attempts"]:
            outcome = f"{a['current_a']:.6g} A" if a["ok"] else f"FAILED {a['error']['type']}: {a['error']['message']}"
            print(f"  #{a['attempt']:>2} slot {a['slot']:>2}  target {a['scheduled_offset_s']:.3f} s  "
                  f"start {a['start_offset_s']:.4f} s  took {a['elapsed_s'] * 1000:.2f} ms  {outcome}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
