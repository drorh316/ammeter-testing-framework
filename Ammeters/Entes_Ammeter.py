from Ammeters.base_ammeter import AmmeterEmulatorBase
from src.utils.Utils import generate_random_float


class EntesAmmeter(AmmeterEmulatorBase):
    @property
    def get_current_command(self) -> bytes:
        # Define the command to get the current from ENTES
        return b'MEASURE_ENTES -get_data'

    def measure_current(self) -> float:
        magnetic_field = generate_random_float(0.01, 0.1)  # Magnetic field strength (0.01T - 0.1T)
        calibration_factor = generate_random_float(500, 2000)  # Calibration factor (500 - 2000)
        current = magnetic_field * calibration_factor
        print(f"ENTES Ammeter - Magnetic Field: {magnetic_field}T, Calibration Factor: {calibration_factor}, Current: {current}A")
        return current
