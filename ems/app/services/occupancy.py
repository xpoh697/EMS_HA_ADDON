import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class OccupancyEngine:
    """
    Adjusts energy reserve targets based on the number of people in the house.
    """
    def __init__(self, base_reserve_soc: float = 20.0, kwh_per_person: float = 1.5):
        self.base_reserve_soc = base_reserve_soc
        self.kwh_per_person = kwh_per_person

    def calculate_target_soc(self, sensors: Dict[str, Any], battery_capacity_kwh: float) -> float:
        """
        Computes the survival SOC target.
        """
        people_count = float(sensors.get("occupancy_count", 1.0))
        additional_kwh = people_count * self.kwh_per_person
        
        # Convert additional kWh to SOC %
        additional_soc = (additional_kwh / battery_capacity_kwh) * 100 if battery_capacity_kwh > 0 else 0
        
        # Use user-defined base reserve or fallback to instance default
        base_reserve = sensors.get("emergency_soc", self.base_reserve_soc)
        target_soc = base_reserve + additional_soc
        
        # Cap between 10% and 95%
        return min(max(target_soc, 10.0), 95.0)
