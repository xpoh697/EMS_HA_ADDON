from enum import IntEnum

class InverterState(IntEnum):
    """
    Inverter states ordered by priority (1 is highest).
    """
    BUY = 1             # Import from grid, charge battery from grid
    BAT_EMERGENCY = 2   # Protect SOC, no discharge, charge from sun only
    STOP_SALE = 3       # No export, battery charges only from PV excess
    NO_PV_SALE_NO_BAT = 4 # No export, no charge, home fed by sun
    SALE_PV = 5         # Default: Home -> Battery -> Grid
    SALE_PV_NO_BAT = 6  # No battery charge, excess sun to grid
    SALE_PV_BAT = 7     # Active sale of sun and battery energy

    def __str__(self):
        return self.name.lower()
