from enum import Enum
from typing import Dict, Any

class GenPolicy(str, Enum):
    NORMAL = "normal"               # Full PV generation used
    FORBIDDEN = "forbidden"         # PV generation ignored/blocked (0 kW)
    HOUSE_ONLY = "house_only"       # PV covers only house load, no battery charging, no export
    NO_EXPORT = "no_export"         # PV covers house and battery, but no grid export
    THROTTLE_ON_NEGATIVE = "throttle_on_negative" # Blocked if price is negative

class ModeConfig:
    def __init__(self, name: str, allow_charge: bool, allow_discharge: bool, house_on_bypass: bool, gen_policy: GenPolicy):
        self.name = name
        self.allow_charge = allow_charge
        self.allow_discharge = allow_discharge
        self.house_on_bypass = house_on_bypass
        self.gen_policy = gen_policy

MODE_CONFIGS: Dict[str, ModeConfig] = {
    "buy": ModeConfig(
        name="buy",
        allow_charge=True,
        allow_discharge=False,      # House on bypass, battery doesn't discharge to house
        house_on_bypass=True,
        gen_policy=GenPolicy.THROTTLE_ON_NEGATIVE
    ),
    "no_pv_sale_no_bat": ModeConfig(
        name="no_pv_sale_no_bat",
        allow_charge=False,
        allow_discharge=True,       # Battery can cover house
        house_on_bypass=False,
        gen_policy=GenPolicy.HOUSE_ONLY
    ),
    "sale_pv_no_bat": ModeConfig(
        name="sale_pv_no_bat",
        allow_charge=False,
        allow_discharge=False,      # Inverter sells PV, battery is idle
        house_on_bypass=False,
        gen_policy=GenPolicy.NORMAL # PV is generated and sold
    ),
    "sale_pv_bat": ModeConfig(
        name="sale_pv_bat",
        allow_charge=True,
        allow_discharge=True,
        house_on_bypass=False,
        gen_policy=GenPolicy.NORMAL
    ),
    "stop_sale": ModeConfig(
        name="stop_sale",
        allow_charge=True,
        allow_discharge=True,
        house_on_bypass=False,
        gen_policy=GenPolicy.NO_EXPORT
    ),
    "sale_pv": ModeConfig(
        name="sale_pv",
        allow_charge=True,
        allow_discharge=True,
        house_on_bypass=False,
        gen_policy=GenPolicy.NORMAL
    ),
    "bat_emergency": ModeConfig(
        name="bat_emergency",
        allow_charge=True,
        allow_discharge=False,
        house_on_bypass=True,      # Grid covers house
        gen_policy=GenPolicy.NORMAL
    ),
}

DEFAULT_MODE_CONFIG = MODE_CONFIGS["sale_pv"]

def get_mode_config(mode: str) -> ModeConfig:
    return MODE_CONFIGS.get(mode, DEFAULT_MODE_CONFIG)
