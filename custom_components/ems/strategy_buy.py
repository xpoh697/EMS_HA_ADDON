import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from homeassistant.util import dt as dt_util
from .utils import normalize_float, round_f
from .const import (
    CONF_PRICE_BUY_LIMIT, 
    CONF_DYNAMIC_SOC_BUY, 
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_BATTERY_MAX_POWER
)

_LOGGER = logging.getLogger(__name__)

class BuyStrategyEngine:
    """Specialized engine for grid-buying and arbitrage charging logic."""
    def __init__(self, manager):
        self.manager = manager

    def calculate(self, planner, now: datetime):
        """Main entry point to calculate buy strategy and propose to planner."""
        # 1. Fetch data
        cur_hour = int(now.hour)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        
        try:
            p_st = dict(self.manager.data.get("prices_buy", {}))
            today_prices = dict(p_st.get(today_str, {}))
            tomorrow_prices = dict(p_st.get(tomorrow_str, {}))
        except Exception:
            return {}

        if not today_prices:
            return {}

        all_buy_prices = {}
        for h, p in today_prices.items(): all_buy_prices[int(h)] = float(normalize_float(p))
        for h, p in tomorrow_prices.items(): all_buy_prices[int(h) + 24] = float(normalize_float(p))

        # Absolute hour mapping
        cur_h_abs = int(now.timestamp() // 3600)
        
        # Cycle Isolation (v11.6.527)
        _sorted_h = sorted(all_buy_prices.keys())
        _final_all = {}
        for h in _sorted_h:
            if h < cur_hour: continue
            if _final_all and (h - max(_final_all.keys()) > 12):
                break # Night gap detected
            _final_all[h] = all_buy_prices[h]
        all_buy_prices_filtered = _final_all

        # 2. Logic parameters
        buy_limit = float(self.manager.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
        dynamic_buy_ai = bool(self.manager.get_setting(CONF_DYNAMIC_SOC_BUY, True))
        deg_cost = float(self.manager.strategy_engine.get_battery_degradation_cost() or 0.0)
        min_p_v = self.manager.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0)
        threshold = float(max(float(min_p_v or 0.0), 2.0 * deg_cost))
        eff_coeff = float(self.manager.strategy_engine.get_efficiency_coefficient() or 1.0)
        
        # Prices for arbitrage selling
        s_p_today = dict(self.manager.data.get("prices_sell", {}).get(today_str, {}))
        s_p_tom = dict(self.manager.data.get("prices_sell", {}).get(tomorrow_str, {}))
        all_sell_prices = {}
        for h, p in s_p_today.items(): all_sell_prices[int(h)] = float(normalize_float(p))
        for h, p in s_p_tom.items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))

        # 3. Decision functions
        def is_buy_profitable(buy_p, hour):
            first_neg_h = min([h for h, p in all_buy_prices_filtered.items() if p <= 0] or [999])
            future_sell_options = {h_s: p_s for h_s, p_s in all_sell_prices.items() if h_s > hour}
            if not future_sell_options: return False
            best_s_h = max(future_sell_options, key=lambda k: future_sell_options[k])
            best_s = future_sell_options[best_s_h]
            if buy_p > 0.0 and best_s_h >= first_neg_h: return False
            gain = float(best_s * eff_coeff - buy_p - deg_cost)
            return gain >= threshold

        # 4. Find active hours
        target_hours = []
        for h, p in all_buy_prices_filtered.items():
            if p <= buy_limit or (dynamic_buy_ai and is_buy_profitable(p, h)):
                target_hours.append(h)

        # 5. Propose to planner
        # We need to map relative hours (h) back to absolute hours
        h_offset = cur_h_abs - cur_hour
        
        # Determine target SOC and Power
        # In buy mode, we usually charge to CONF_AI_CHARGE_LIMIT
        from .const import CONF_AI_CHARGE_LIMIT
        target_soc = float(self.manager.get_setting(CONF_AI_CHARGE_LIMIT, 100.0))
        max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))

        # Check if we should wait for negative prices (v11.6.22)
        negative_hours = [h for h, p in all_buy_prices_filtered.items() if p <= 0]
        first_neg_h = min(negative_hours) if negative_hours else None
        
        # v11.6.29 wait logic
        can_wait = False
        if first_neg_h is not None:
             # Logic from strategy.py would go here to set is_waiting_for_neg
             pass

        # Calculate amps (v11.6.530)
        batt_v = 51.2 # Default fallback
        if self.manager.battery_voltage_sensor:
             v_now = self.manager.get_sensor_float(self.manager.battery_voltage_sensor)
             if v_now and v_now > 10.0: batt_v = v_now
        
        target_amps = round_f((max_p * 1000.0) / batt_v, 1)

        active_count = 0
        is_active_now = False
        for h_rel in target_hours:
            h_abs_target = h_offset + h_rel
            planner.propose(
                hour_abs=h_abs_target,
                mode="buy",
                power=max_p,
                target_soc=target_soc,
                amps=target_amps,
                source="system"
            )
            active_count += 1
            if h_rel == cur_hour:
                is_active_now = True

        state = "idle"
        decision = "Ожидание окна"
        if is_active_now:
            state = "buying"
            decision = "Активная покупка"
        elif active_count > 0:
            state = "scheduled"
            decision = f"Запланировано {active_count}ч"

        # Calculate actual analyzed window (v11.6.535)
        last_h = max(all_buy_prices_filtered.keys()) if all_buy_prices_filtered else cur_hour
        window_str = self.manager.strategy_engine._format_h(h_offset + last_h)

        return {
            "state": state,
            "active_hours": target_hours,
            "first_negative_hour": first_neg_h,
            "target_soc": target_soc,
            "today_prices": today_prices,
            "tomorrow_prices": tomorrow_prices,
            "recommended_power_kw": max_p if is_active_now else 0.0,
            "recommended_amps": target_amps if is_active_now else 0.0,
            "arbitrage_decision": decision,
            "strategy_candidates": [self.manager.strategy_engine._format_h(h_offset + h) for h in target_hours],
            "analyzed_window": window_str
        }
