from app.services.base import BaseLoadHandler
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class BoilerManager(BaseLoadHandler):
    """
    Manages the boiler load based on water temperature, solar production, and grid limit.
    """
    def __init__(self, name: str, entity_id: str, priority: int = 5):
        super().__init__(name, entity_id, priority)
        self.target_temp = 60.0
        self.min_temp = 40.0
        self.nominal_power_w = 2500.0  # Common for boilers

    def decide(self, sensors: Dict[str, Any], can_use_energy: bool, available_power_w: float) -> bool:
        current_temp = sensors.get("boiler_temp", 0.0)
        grid_price = sensors.get("grid_price", 10.0)
        
        # 1. Absolute Emergency: Water is too cold
        if current_temp < self.min_temp:
            return True

        # 2. Maximum reached: Turn OFF
        if current_temp >= self.target_temp:
            return False

        # 3. Opportunistic Heating: Negative price
        if grid_price <= 0:
            return True

        # 4. Standard Optimization: Use solar if allowed
        if can_use_energy:
            return current_temp < self.target_temp

        return False

    def get_config_schema(self) -> Dict[str, Any]:
        schema = super().get_config_schema()
        schema.update({
            "target_temp": {"type": "number", "label": "Target Temperature (°C)"},
            "min_temp": {"type": "number", "label": "Emergency Min Temp (°C)"},
            "nominal_power_w": {"type": "number", "label": "Heating Element power (W)"}
        })
        return schema

