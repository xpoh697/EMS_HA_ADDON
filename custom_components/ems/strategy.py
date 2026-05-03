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
    CONF_BATTERY_COST,
    CONF_BATTERY_RATED_CYCLES,
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
            # Using defaults from TS if not configured
            cost = float(self.manager.get_setting(CONF_BATTERY_COST, 5000.0) or 5000.0)
            cycles = float(self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000.0) or 6000.0)
            cap = float(self.manager.get_setting(CONF_BATTERY_CAPACITY, 10.0) or 10.0)
            
            if cycles < 1 or cap < 0.1: 
                return 0.02
            
            # Total energy through life = Capacity * Cycles * Efficiency/DOD (approx 0.9)
            # Formula: TotalCost / (Total kWh through life)
            return float(cost / (cycles * cap * 0.9))
        except Exception as e:
            _LOGGER.warning("Error calculating battery degradation cost: %s. Using default 0.02", e)
            return 0.02

    def get_hourly_accuracy_coeff(self, hour: int) -> tuple[float, float]:
        """Provides historical accuracy coefficient for a specific hour."""
        return 1.0, 1.0

    def _format_h(self, h_abs: Optional[int]) -> str:
        if h_abs is None: return "N/A"
        is_tom = h_abs >= 24
        return f"{h_abs % 24:0>2}:00" + (" (Завтра)" if is_tom else "")

    def _calculate_amps(self, power_kw: float) -> float:
        """Helper to convert kW to Amps based on current battery voltage."""
        batt_v = 51.2 # Default
        if self.manager.battery_voltage_sensor:
             v_now = self.manager.get_sensor_float(self.manager.battery_voltage_sensor)
             if v_now and v_now > 10.0: batt_v = v_now
        return round_f((float(power_kw) * 1000.0) / batt_v, 1)

    def update_strategies(self):
        """Main update loop called periodically to recalculate plan."""
        now = datetime.now(self.manager.tz)
        planner = self.manager.planner
        
        # 1. Cleanup past plan (system entries only)
        planner.cleanup()
        
        # 2. Baseline Planning (Filling the 48h grid with defaults)
        self._fill_baseline_plan(planner, now)
        
        # 3. Strategy Overlays (Buy/Sell specific windows)
        # These engines now "propose" modes to the planner
        buy_res = self.buy_engine.calculate(planner, now)
        sell_res = self.sell_engine.calculate(planner, now)
        
        # 4. Arbitration & Finalization (Security, sunrise guard, emergency)
        self._arbitrate_and_finalize(planner, now)
        
        # 5. Enrich results for sensors
        common_data = {
            "deg_cost": self.get_battery_degradation_cost(),
            "profit_threshold": float(self.manager.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0)),
        }
        
        buy_res.update(common_data)
        sell_res.update(common_data)
        
        self._strategy_cache["market_strategy_buy"] = {"res": buy_res, "ts": now}
        self._strategy_cache["market_strategy_sell"] = {"res": sell_res, "ts": now}

    def _fill_baseline_plan(self, planner, now):
        """Fills all 48 hours with a 'Natural' mode based on price floors and daytime."""
        from .const import CONF_PRICE_STOP_SELL, CONF_SALE_PV_NO_BAT_MAX_HOUR
        price_stop_sell = float(self.manager.get_setting(CONF_PRICE_STOP_SELL, 0.0))
        max_morning_hour = int(self.manager.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13))
        
        h_abs_start = int(now.timestamp() // 3600)
        
        for i in range(48):
            h_abs = h_abs_start + i
            dt = datetime.fromtimestamp(h_abs * 3600, self.manager.tz)
            
            # Use sell price for baseline decisions
            price = self.manager.get_price("sell", dt.strftime("%Y-%m-%d"), dt.hour)
            
            # Default Baseline
            mode = "sale_pv"
            
            # 1. Stop Sale Floor
            if price is not None and price < price_stop_sell:
                mode = "stop_sale"
            
            # 2. Morning PV Sale (Saving space for negative prices if expected)
            # This logic should ideally check if a 'minus' is coming later today
            if dt.hour < max_morning_hour:
                # Simple heuristic: if we expect buy opportunity later, save space
                # For now, keep it as 'sale_pv' but we can upgrade to 'sale_pv_no_bat'
                pass
                
            planner.set_mode(mode, hour=h_abs, source="system")

    def _arbitrate_and_finalize(self, planner, now):
        """Final pass to enforce high-priority constraints and waiting logic."""
        from .const import CONF_MIN_SOC_BAT, CONF_SALE_PV_NO_BAT_MAX_HOUR
        min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
        max_morning_h = int(self.manager.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13))
        
        h_abs_now = int(now.timestamp() // 3600)
        soc_now, _, _ = self.manager.get_battery_state(soc_default=100.0)
        
        # 0. Get info from Buy Strategy about future negative prices
        buy_info = self._strategy_cache.get("market_strategy_buy", {}).get("res", {})
        first_neg_h_rel = buy_info.get("first_negative_hour") # Relative to today 00:00 (0..47)
        h_abs_today_00 = int(datetime(now.year, now.month, now.day).timestamp() // 3600)
        
        # 1. Run a predictive simulation using the current (proposed) plan
        full_schedule = {}
        for i in range(48):
            full_schedule[h_abs_now + i] = planner.get_plan(h_abs_now + i)

        _, sim_log, _ = self.simulation_engine.run_soc_simulation(
            soc_now, range(h_abs_now, h_abs_now + 48), now, planner_schedule=full_schedule
        )
        
        for i in range(48):
            h_abs = h_abs_now + i
            current_plan = planner.get_plan(h_abs)
            
            # USER override is sacred
            if current_plan.get("source") == "user":
                continue
                
            dt = datetime.fromtimestamp(h_abs * 3600, self.manager.tz)
            
            # Projected SOC at the START of this hour
            prev_h_abs = h_abs - 1
            if i == 0:
                proj_soc = soc_now
            else:
                prev_h = int(prev_h_abs % 24)
                prev_is_tom = prev_h_abs >= (h_abs_today_00 + 24)
                prev_key = f"{prev_h:02d}:59" + (" (Завтра)" if prev_is_tom else "")
                proj_soc = sim_log.get(prev_key, {}).get("soc", soc_now)
            
            # --- Arbitration Logic ---
            
            # Priority 1: Emergency SOC Protection
            if proj_soc <= min_soc:
                if current_plan.get("mode") in ["sale_pv_bat", "sale_pv"]:
                    planner.set_mode("bat_emergency", hour=h_abs, source="system")
                    continue # Emergency overrides everything else
            
            # Priority 2: Wait for Negative Price Logic (TZ 4.2.2 & 5)
            if first_neg_h_rel is not None:
                h_rel = h_abs - h_abs_today_00
                if h_rel < first_neg_h_rel:
                    # If this is a morning hour and we have a 'minus' coming later
                    if dt.hour < max_morning_h:
                        # TZ 5: sale_pv_no_bat (sell PV, but don't charge battery)
                        if current_plan.get("mode") == "sale_pv":
                             planner.set_mode("sale_pv_no_bat", hour=h_abs, source="system")
                    
                    # TZ 4.2.2: If it's the hour just before negative price, we might want no_pv_sale_no_bat
                    # to ensure we have maximum space for the negative price window.
                    if h_rel == first_neg_h_rel - 1:
                        planner.set_mode("no_pv_sale_no_bat", hour=h_abs, source="system")

    def get_market_strategy(self, mode: str) -> Dict[str, Any]:
        """Legacy access for sensors to get pre-calculated strategy results."""
        cache_key = f"market_strategy_{mode}"
        cached = self._strategy_cache.get(cache_key)
        if cached:
             return cached["res"]
        
        # Default empty structure to prevent KeyErrors in sensors
        return {
            "state": "initializing",
            "today_prices": {},
            "tomorrow_prices": {},
            "allocated_kwh": 0.0,
            "allocated_hours": [],
            "schedule": [],
            "debug": {}
        }

    def get_budget_and_permissions(self, *args, **kwargs) -> Dict[str, Any]:
        """Calculates available energy budgets and survival parameters."""
        # This will be refined as we move Sunrise Guard here
        res = {"can_sell": True, "survival_soc": 15.0}
        return res

    def get_gen_forecast_coefficient(self, forecast_value: float, prof_gen: Dict[str, Any], hour_start: int, hour_end: int) -> float:
        """Calculates scaling factor between historical profile and today's forecast."""
        try:
            sum_hist = 0.0
            for h in range(hour_start, hour_end):
                sum_hist += float(normalize_float(prof_gen.get(str(h), 0.0)))
            
            if sum_hist < 0.1:
                return 1.0
            return float(forecast_value / sum_hist)
        except Exception:
            return 1.0

    def run_investment_simulation(self, extra_batt_kwh: float = 0.0, pv_multiplier: float = 1.0) -> Dict[str, Any]:
        """Analyzes historical data to estimate savings with hardware upgrades."""
        man = self.manager
        savings_store = man.data.get("savings", {})
        
        # Calculate current average monthly savings
        total_30d = 0.0
        count = 0
        now = datetime.now(man.tz)
        for d, v in savings_store.items():
            try:
                dt_d = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=man.tz)
                if (now - dt_d).days <= 30:
                    if isinstance(v, dict):
                        total_30d += v.get("total", 0.0)
                        count += 1
            except: continue
        
        avg_monthly = total_30d if count >= 28 else (total_30d / count * 30 if count > 0 else 0.0)
        
        # Heuristic for upgrades based on system scaling
        benefit_factor = 1.0
        if extra_batt_kwh > 0:
            _, current_cap, _ = man.get_battery_state()
            if current_cap > 0:
                # Diminishing returns on extra battery
                benefit_factor += min(0.35, (extra_batt_kwh / current_cap) * 0.15)
        
        benefit_factor *= pv_multiplier
        extra_monthly = round_f(float(avg_monthly * (benefit_factor - 1.0)), 2)
        
        return {
            "monthly_estimate": extra_monthly,
            "days_simulated": count,
            "benefit_factor": round_f(benefit_factor, 2)
        }
