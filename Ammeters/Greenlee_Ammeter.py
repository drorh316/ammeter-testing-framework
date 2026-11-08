from Ammeters.base_ammeter import AmmeterEmulatorBase
from src.utils.Utils import generate_random_float


class GreenleeAmmeter(AmmeterEmulatorBase):
    @property
    def get_current_command(self) -> bytes:
        # Define the command to get the current from Greenlee
        return b'MEASURE_GREENLEE -get_measurement'

    def measure_current(self) -> float:
        voltage = generate_random_float(1.0, 10.0)  # Random voltage (1V - 10V)
        resistance = generate_random_float(0.1, 100.0)  # Random resistance (0.1Ω - 100Ω)
        current = voltage / resistance
        print(f"Greenlee Ammeter - Voltage: {voltage}V, Resistance: {resistance}Ω, Current: {current}A")
        return current
