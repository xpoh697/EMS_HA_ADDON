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
        1. BUY
        2. BAT_EMERGENCY
        3. SALE_PV_BAT
        4. SALE_PV_NO_BAT
        5. STOP_SALE
        6. SALE_PV
        """
        
        # Priority 1: BUY (Cheapest energy)
        if self._should_buy(sensors):
            new_state = InverterState.BUY
            
        # Priority 2: BAT_EMERGENCY (Safety)
        elif self._is_emergency_soc(sensors):
            new_state = InverterState.BAT_EMERGENCY
            
        # Priority 3: SALE_PV_BAT (Active sale of sun and battery)
        elif self._should_sell_battery(sensors):
            new_state = InverterState.SALE_PV_BAT
            
        # Priority 4: SALE_PV_NO_BAT (Sale sun, keep battery)
        elif self._is_high_price_no_charge(sensors):
            new_state = InverterState.SALE_PV_NO_BAT

        # Priority 5: STOP_SALE (Forbidden or too cheap to sell)
        elif self._is_sale_prohibited(sensors):
            new_state = InverterState.STOP_SALE
            
        # Priority 6: SALE_PV (Default/Standard sale)
        elif self._is_standard_sale(sensors):
            new_state = InverterState.SALE_PV
            
        else:
            new_state = InverterState.SALE_PV  # Default fallback

        if new_state != self.current_state:
            logger.info(f"Transitioning from {self.current_state} to {new_state}")
            self.current_state = new_state
            
        return new_state

    def _should_buy(self, s): 
        # Price is below buy_max_price AND battery not full
        max_price = s.get("buy_max_price", 0.0)
        return s.get("buy_price", 0) <= max_price and s.get("battery_soc", 100) < 95

    def _is_emergency_soc(self, s):
        # Battery below survival threshold
        return s.get("battery_soc", 100) < s.get("survival_soc", 20)

    def _is_sale_prohibited(self, s):
        # Manual stop OR price below threshold
        if s.get("stop_sale_flag", False):
            return True
        min_price = s.get("stop_sale_min_price", 0.001)
        return s.get("sell_price", 0) < min_price

    def _is_waiting_for_dip(self, s):
        return s.get("waiting_for_price_dip", False)

    def _is_standard_sale(self, s):
        # Default sale only if price > min threshold
        min_price = s.get("sale_pv_min_price", 0.0)
        return s.get("sell_price", 0) >= min_price

    def _is_high_price_no_charge(self, s):
        # Sale PV but don't charge battery from it if price is good and current_hour < max_hour
        min_price = s.get("sale_pv_no_bat_min_price", 0.1)
        max_hour = s.get("sale_pv_no_bat_max_hour", 10)
        current_hour = s.get("current_hour", 0)
        
        return s.get("sell_price", 0) >= min_price and current_hour < max_hour

    def _should_sell_battery(self, s):
        # Price is peak AND battery is above target
        min_price = s.get("sale_pv_bat_min_price", 0.5)
        min_soc = s.get("sale_pv_bat_min_soc", 50)
        
        return s.get("sell_price", 0) >= min_price and s.get("battery_soc", 0) >= min_soc

