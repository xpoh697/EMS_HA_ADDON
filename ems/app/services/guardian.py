import logging
from typing import List, Dict, Any
from app.services.base import BaseLoadHandler

logger = logging.getLogger(__name__)

class PowerGuardian:
    """
    Arbiter that ensures home power consumption does not exceed Grid Limits.
    Manages load requests in order of their priority.
    """
    def __init__(self, max_grid_power_w: float = 11000.0):
        self.max_grid_power_w = max_grid_power_w

    def coordinate(self, handlers: List[BaseLoadHandler], sensors: Dict[str, Any], can_use_energy: bool) -> Dict[str, bool]:
        """
        Calculates which loads can be turned ON based on available power budget.
        """
        commands = {}
        # Current house load without managed loads (roughly)
        total_house_load = float(sensors.get("house_power", 0.0))
        
        # Calculate base load (house load minus already running managed loads)
        managed_power = sum(h.get_power_usage(sensors) for h in handlers)
        base_load = max(0, total_house_load - managed_power)
        
        available_power = self.max_grid_power_w - base_load
        logger.debug(f"Guardian: Base Load: {base_load}W, Available: {available_power}W")

        # Sort handlers by priority (1 is highest, but here we assume ascending order)
        for handler in sorted(handlers, key=lambda x: x.priority):
            # Ask handler if it WANTS to run
            wants_to_run = handler.decide(sensors, can_use_energy, available_power)
            
            if wants_to_run:
                # Check if we have enough power budget
                required_power = handler.nominal_power_w
                if available_power >= required_power:
                    commands[handler.entity_id] = True
                    available_power -= required_power
                    logger.info(f"Guardian: ALLOWED {handler.name} ({required_power}W). Remaining budget: {available_power}W")
                else:
                    logger.warning(f"Guardian: DENIED {handler.name}. Needs {required_power}W, but only {available_power}W available.")
                    commands[handler.entity_id] = False
            else:
                commands[handler.entity_id] = False

        return commands
