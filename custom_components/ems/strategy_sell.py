import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from homeassistant.util import dt as dt_util
from .utils import normalize_float, round_f
from .const import (
    CONF_PRICE_SELL_LIMIT,
    CONF_DYNAMIC_SOC_SELL,
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_BATTERY_MAX_POWER,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_PRICE_BUY_LIMIT
)

_LOGGER = logging.getLogger(__name__)

class SellStrategyEngine:
    """Specialized engine for solar-selling and arbitrage discharging logic."""
    def __init__(self, manager):
        self.manager = manager

    def calculate(self, planner, now: datetime):
        """Main entry point to calculate sell strategy and propose to planner."""
        cur_hour = int(now.hour)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            p_st = dict(self.manager.data.get("prices_sell", {}))
            today_prices = dict(p_st.get(today_str, {}))
            tomorrow_prices = dict(p_st.get(tomorrow_str, {}))
        except Exception:
            return {}

        if not today_prices:
            return {}

        all_sell_prices = {}
        for h, p in today_prices.items(): all_sell_prices[int(h)] = float(normalize_float(p))
        for h, p in tomorrow_prices.items(): all_sell_prices[int(h) + 24] = float(normalize_float(p))

        # 1. Logic parameters
        sell_limit = float(self.manager.get_setting(CONF_PRICE_SELL_LIMIT, 5.0))
        buy_limit = float(self.manager.get_setting(CONF_PRICE_BUY_LIMIT, 2.0))
        dynamic_sell_ai = bool(self.manager.get_setting(CONF_DYNAMIC_SOC_SELL, True))
        deg_cost = float(self.manager.strategy_engine.get_battery_degradation_cost() or 0.0)
        min_p_v = self.manager.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0)
        threshold = float(max(float(min_p_v or 0.0), 2.0 * deg_cost))
        eff_coeff = float(self.manager.strategy_engine.get_efficiency_coefficient() or 1.0)
        
        batt_soc, batt_cap, _ = self.manager.get_battery_state()
        b_soc = float(batt_soc)
        b_cap = float(batt_cap)

        # 2. Peak identification
        def get_peaks(window, is_sell, limit):
            if not window: return []
            w_vals = [float(v) for v in window.values()]
            if not w_vals: return []
            target = max(w_vals) if is_sell else min(w_vals)
            if (is_sell and target < limit) or (not is_sell and target > limit):
                return []
            peak_hours = [int(h) for h, p in window.items() if float(p) == target]
            peaks = set()
            for peak_h in peak_hours:
                h = peak_h
                while str(h) in window:
                    p = float(window[str(h)])
                    if (is_sell and p >= limit) or (not is_sell and p <= limit):
                        peaks.add((h, p))
                        h -= 1
                    else: break
                h = peak_h + 1
                while str(h) in window:
                    p = float(window[str(h)])
                    if (is_sell and p >= limit) or (not is_sell and p <= limit):
                        peaks.add((h, p))
                        h += 1
                    else: break
            return sorted(list(peaks), key=lambda x: x[0])

        today_morn = {h: p for h, p in today_prices.items() if cur_hour <= int(h) < 13}
        today_eve = {h: p for h, p in today_prices.items() if cur_hour <= int(h) >= 13}
        tom_morn = {h: p for h, p in tomorrow_prices.items() if int(h) < 13}
        tom_eve = {h: p for h, p in tomorrow_prices.items() if int(h) >= 13}

        raw_peaks_today = get_peaks(today_morn, True, sell_limit) + get_peaks(today_eve, True, sell_limit)
        raw_peaks_tom = get_peaks(tom_morn, True, sell_limit) + get_peaks(tom_eve, True, sell_limit)
        
        active_hours = []
        if dynamic_sell_ai:
            # Combined peak strategy logic from strategy.py
            # For simplicity, we use the raw peaks as candidates
            tech_peaks_all = [h for h, p in raw_peaks_today] + [h + 24 for h, p in raw_peaks_tom]
            active_hours = tech_peaks_all
        else:
            active_hours = [int(h) for h, p in today_prices.items() if float(normalize_float(p)) >= sell_limit]
            active_hours += [int(h) + 24 for h, p in tomorrow_prices.items() if float(normalize_float(p)) >= sell_limit]

        # 3. Propose to planner
        cur_h_abs = int(now.timestamp() // 3600)
        h_offset = cur_h_abs - cur_hour
        
        target_soc = float(self.manager.get_setting(CONF_AI_DISCHARGE_LIMIT, 20.0))
        max_p = float(self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0))

        for h_rel in active_hours:
            h_abs_target = h_offset + h_rel
            planner.propose(
                hour_abs=h_abs_target,
                mode="sale_pv_bat",
                power=max_p,
                target_soc=target_soc,
                source="system"
            )

        return {
            "active_hours": active_hours,
            "target_soc": target_soc,
            "today_prices": today_prices,
            "tomorrow_prices": tomorrow_prices
        }
