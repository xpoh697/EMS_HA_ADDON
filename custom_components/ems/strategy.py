import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from .utils import normalize_float, round_f
from .simulation import SimulationEngine
from .strategy_buy import BuyStrategyEngine
from .strategy_sell import SellStrategyEngine
from .const import (
    DOMAIN, VERSION,
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_BATTERY_MAX_POWER,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_MIN_SOC_BAT,
    CONF_BATTERY_CYCLE_COST,
    CONF_BATTERY_MAX_CYCLES,
    CONF_BATTERY_CAPACITY,
)

_LOGGER = logging.getLogger(__name__)

class StrategyEngine:
    """Coordinator for all EMS strategies and simulations."""
    def __init__(self, manager):
        self.manager = manager
        self.simulation_engine = SimulationEngine(manager)
        self.buy_engine = BuyStrategyEngine(manager)
        self.sell_engine = SellStrategyEngine(manager)
        self._strategy_cache = {}
        self._calculating_strategy = False

    def get_efficiency_coefficient(self) -> float:
        return self.simulation_engine.get_efficiency_coefficient()

    def run_soc_simulation(self, *args, **kwargs):
        """Wrapper for SimulationEngine.run_soc_simulation for backward compatibility."""
        return self.simulation_engine.run_soc_simulation(*args, **kwargs)

    def get_battery_degradation_cost(self) -> float:
        """Calculates the wear-and-tear cost per kWh based on battery specs."""
        try:
            cost = float(self.manager.get_setting(CONF_BATTERY_CYCLE_COST, 5000.0))
            cycles = float(self.manager.get_setting(CONF_BATTERY_MAX_CYCLES, 6000.0))
            cap = float(self.manager.get_setting(CONF_BATTERY_CAPACITY, 10.0))
            if cycles < 1 or cap < 0.1: return 0.02
            # Total energy through life = Capacity * Cycles * Efficiency (approx 0.9)
            return float(cost / (cycles * cap * 0.9))
        except Exception:
            return 0.02

    def get_hourly_accuracy_coeff(self, hour: int) -> tuple[float, float]:
        """Provides historical accuracy coefficient for a specific hour."""
        return 1.0, 1.0

    def _format_h(self, h_abs: Optional[int]) -> str:
        if h_abs is None: return "N/A"
        is_tom = h_abs >= 24
        return f"{h_abs % 24:0>2}:00" + (" (Завтра)" if is_tom else "")

    def update_strategies(self):
        """Main update loop called periodically to recalculate plan."""
        now = datetime.now(self.manager.tz)
        planner = self.manager.planner
        
        # 1. Cleanup past plan
        planner.cleanup()
        
        # 2. Run strategies and propose to planner
        buy_res = self.buy_engine.calculate(planner, now)
        sell_res = self.sell_engine.calculate(planner, now)
        
        # 3. Store results for sensors
        self._strategy_cache["market_strategy_buy"] = {"res": buy_res, "ts": now}
        self._strategy_cache["market_strategy_sell"] = {"res": sell_res, "ts": now}

    def get_market_strategy(self, mode: str) -> Dict[str, Any]:
        """Legacy access for sensors to get pre-calculated strategy results."""
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached:
             return cached["res"]
        return {}

    def get_budget_and_permissions(self, *args, **kwargs) -> Dict[str, Any]:
        """Calculates available energy budgets and survival parameters."""
        # Simplified for now to keep the coordination logic clean
        res = {"can_sell": True, "survival_soc": 15.0}
        return res
