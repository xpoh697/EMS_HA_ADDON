import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class BoilerManager:
    """
    Manages the boiler load based on water temperature, solar production, and price.
    """
    def __init__(self, target_temp: float = 60.0, min_temp: float = 40.0):
        self.target_temp = target_temp
        self.min_temp = min_temp
        self.state = "idle"

    def decide(self, sensors: Dict[str, Any], can_use_energy: bool) -> bool:
        """
        Decision logic for the boiler.
        :param sensors: Current HA sensor data.
        :param can_use_energy: Boolean from InverterController indicating if excess energy is available.
        :return: True to turn ON, False to turn OFF.
        """
        current_temp = sensors.get("boiler_temp", 0.0)
        grid_price = sensors.get("grid_price", 10.0)
        
        # 1. Absolute Emergency: Water is too cold
        if current_temp < self.min_temp:
            logger.warning(f"Boiler temperature ({current_temp}) below minimum ({self.min_temp}). Forcing ON.")
            return True

        # 2. Maximum reached: Turn OFF
        if current_temp >= self.target_temp:
            return False

        # 3. Opportunistic Heating: Negative price
        if grid_price <= 0:
            logger.info("Negative price detected. Opportunistic boiler heating.")
            return True

        # 4. Standard Optimization: Use solar if allowed by main engine
        if can_use_energy:
            # We heat if we haven't reached target yet
            return current_temp < self.target_temp

        return False
