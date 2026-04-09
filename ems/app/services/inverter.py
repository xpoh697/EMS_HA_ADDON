from typing import Dict, Any
from app.models.enums import InverterState
import logging

logger = logging.getLogger(__name__)

class InverterController:
    """
    Handles inverter state transitions based on specified priorities (1-7).
    """
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.current_state = InverterState.SALE_PV  # Default fallback state

    def update_state(self, sensors: Dict[str, Any]) -> InverterState:
        """
        Determines the next state by checking conditions in order of priority.
        :param sensors: Dictionary of current sensor values (SOC, Price, Load, etc.)
        :return: Chosen InverterState
        """
        
        # Priority 1: BUY
        if self._should_buy(sensors):
            new_state = InverterState.BUY
            
        # Priority 2: BAT_EMERGENCY
        elif self._is_emergency_soc(sensors):
            new_state = InverterState.BAT_EMERGENCY
            
        # Priority 3: STOP_SALE
        elif self._is_sale_prohibited(sensors):
            new_state = InverterState.STOP_SALE
            
        # Priority 4: NO_PV_SALE_NO_BAT
        elif self._is_waiting_for_dip(sensors):
            new_state = InverterState.NO_PV_SALE_NO_BAT
            
        # Priority 5: SALE_PV (Default)
        elif self._is_standard_sale(sensors):
            new_state = InverterState.SALE_PV
            
        # Priority 6: SALE_PV_NO_BAT
        elif self._is_high_price_no_charge(sensors):
            new_state = InverterState.SALE_PV_NO_BAT
            
        # Priority 7: SALE_PV_BAT (Active sale)
        elif self._should_sell_battery(sensors):
            new_state = InverterState.SALE_PV_BAT
            
        else:
            new_state = InverterState.SALE_PV  # Default fallback

        if new_state != self.current_state:
            logger.info(f"Transitioning from {self.current_state} to {new_state}")
            self.current_state = new_state
            
        return new_state

    # Condition placeholders (to be expanded with actual math and logic)
    def _should_buy(self, s): 
        # Price is negative or very low AND battery not full
        return s.get("grid_price", 0) <= 0 and s.get("battery_soc", 100) < 95

    def _is_emergency_soc(self, s):
        # Battery below survival threshold
        return s.get("battery_soc", 100) < s.get("survival_soc", 20)

    def _is_sale_prohibited(self, s):
        # User blocked sale or price is bad
        return s.get("stop_sale_flag", False)

    def _is_waiting_for_dip(self, s):
        return s.get("waiting_for_price_dip", False)

    def _is_standard_sale(self, s):
        return True # Default condition

    def _is_high_price_no_charge(self, s):
        return s.get("grid_price", 0) > s.get("high_price_threshold", 50)

    def _should_sell_battery(self, s):
        # Price is peak AND battery is above target AND forecast is good
        return s.get("grid_price", 0) >= s.get("peak_price_threshold", 100)
