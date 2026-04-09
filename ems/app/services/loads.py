from app.services.base import BaseLoadHandler
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class CyclicLoadHandler(BaseLoadHandler):
    """
    Manages a single cyclic load (washing machine, dishwasher).
    Inherits from BaseLoadHandler to fit into the modular EMS architecture.
    """
    def __init__(self, name: str, entity_id: str, priority: int = 10):
        super().__init__(name, entity_id, priority)
        self.state = "idle" # idle, waiting, running
        self.nominal_power_w = 2000.0

    def decide(self, sensors: Dict[str, Any], can_use_energy: bool, available_power_w: float) -> bool:
        """
        Logic for a cyclic machine.
        """
        current_power = self.get_power_usage(sensors)
        grid_price = sensors.get("grid_price", 10.0)

        # 1. Protection: If it's already drawing power, it MUST stay ON
        if current_power > 10.0:
            self.state = "running"
            return True

        # 2. If it was running but power dropped, stay ON for the pause
        if self.state == "running" and current_power < 5.0:
            # Note: In a production system, add a timeout here
            return True

        # 3. Decision for new cycle
        if self.state == "waiting":
            # Start if price is zero/negative OR we have solar excess
            if grid_price <= 0 or can_use_energy:
                # IMPORTANT: PowerGuardian will check if available_power_w is enough
                return True
        
        return False

    def get_config_schema(self) -> Dict[str, Any]:
        schema = super().get_config_schema()
        schema.update({
            "nominal_power_w": {"type": "number", "label": "Peak cycle power (W)"},
            "state": {"type": "select", "label": "Mode", "options": ["idle", "waiting"]}
        })
        return schema

