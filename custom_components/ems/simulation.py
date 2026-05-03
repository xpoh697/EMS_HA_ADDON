import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from homeassistant.util import dt as dt_util
from .utils import normalize_float, round_f
from .modes import get_mode_config, GenPolicy
from .const import CONF_BATTERY_MAX_POWER

_LOGGER = logging.getLogger(__name__)

def get_cc_cv_ratio(soc: float) -> float:
    """Strict CC/CV ratio based on user-provided table (v6.11)."""
    if soc >= 100: return 0.0
    if soc >= 98: return 0.125
    if soc >= 95: return 0.40
    return 1.0

def get_soc_from_log(log: dict, key: Any, default: Optional[float]) -> Optional[float]:
    """Safely extract SOC float from simulation log."""
    if not log: return default
    val = log.get(key)
    if val is None and isinstance(key, (int, float)):
        h_abs = int(key)
        h_rel = h_abs % 24
        is_tom = h_abs >= 24
        str_key = f"{h_rel:02d}:59" + (" (Завтра)" if is_tom else "")
        val = log.get(str_key)
    if val is None and isinstance(key, str) and key.isdigit():
        val = log.get(int(key))
    if isinstance(val, dict):
        res = val.get("soc", default)
    else:
        res = val if val is not None else default
    return float(res) if res is not None else default

class SimulationEngine:
    def __init__(self, manager):
        self.manager = manager

    def get_efficiency_coefficient(self) -> float:
        """Calculates historical inverter efficiency (Smart filtering for High Power)."""
        man = self.manager
        d_store = getattr(man, "data", {})
        if not isinstance(d_store, dict): return 0.95
        l_map = d_store.get("losses", {})
        if not isinstance(l_map, dict): return 0.95
        sum_g = 0.0
        sum_l = 0.0
        smp_count = 0
        for h_idx in range(24):
            recs = l_map.get(str(h_idx), [])
            if not isinstance(recs, list): continue
            for item in recs[-14:]:
                if not isinstance(item, dict): continue
                g_val = float(normalize_float(item.get("gen", 0.0)))
                l_val = float(normalize_float(item.get("v", 0.0)))
                if g_val > 1.0:
                    sum_g += g_val
                    sum_l += l_val
                    smp_count += 1
        if smp_count < 3 or sum_g < 1.0:
            return 0.95
        eff_ratio = float((sum_g - sum_l) / sum_g)
        return float(max(0.85, min(0.99, eff_ratio)))

    def run_soc_simulation(
        self, 
        start_soc: float, 
        sim_range: List[int], 
        now: datetime, 
        planner_schedule: Optional[Dict[int, Dict[str, Any]]] = None,
        house_profile_override: Optional[str] = None,
        ignore_blended: bool = False,
        no_solar: bool = False,
        **kwargs
    ) -> Tuple[float, Dict[str, Any], float]:
        """
        Universal SOC simulation engine.
        Supports both new planner_schedule and legacy commands/strategy constraints.
        """
        # Handle legacy or additional strategy constraints
        commands = kwargs.get("commands") or {}
        no_battery_charge_until = kwargs.get("no_battery_charge_until")
        pv_curtail_hours = kwargs.get("pv_curtail_hours") or []
        if not sim_range:
            return float(start_soc), {}, 0.0

        man = self.manager
        _, batt_cap, _ = man.get_battery_state()
        b_cap_f = float(batt_cap)
        if b_cap_f <= 0.1:
            return float(start_soc), {}, 0.0

        eff_period = man.custom_period
        if now.month in [3, 4, 9, 10]:
            eff_period = 7 

        day_idx_today = man.day_type
        tomorrow_dt = now + timedelta(days=1)
        day_idx_tom = tomorrow_dt.weekday()
        
        f_today = float(man.get_forecast_value(man.forecast_today_sensor) or 0.0)
        f_tom = float(man.get_forecast_value(man.forecast_tomorrow_sensor) or 0.0)
        dist_today = man.get_forecast_hourly_distribution(man.forecast_today_hourly_sensor)
        dist_tom = man.get_forecast_hourly_distribution(man.forecast_tomorrow_sensor, tomorrow_dt.strftime("%Y-%m-%d"))

        p_type = house_profile_override or "consumption_total"
        prof_cons_today = dict(man.get_predicted_profile(p_type))
        prof_cons_tom = dict(man.get_average_profile(p_type, eff_period, day_idx_tom))
        
        prof_gen_today = dict(man.get_average_profile("generation", eff_period, day_idx_today))
        prof_gen_tom = dict(man.get_average_profile("generation", eff_period, day_idx_tom))
        prof_losses = dict(man.get_average_profile("losses", 7))
        
        blended_coeff = 1.0 if ignore_blended else float(getattr(man, "last_blended_coeff", 1.0))
        eff_coeff = float(self.get_efficiency_coefficient())
        fraction_left_h1 = float(1.0 - (now.minute / 60.0))
        max_batt_p = float(man.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
        
        all_buy_prices = {}
        try:
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            p_buy_store = dict(man.data.get("prices_buy", {}))
            for h, p in p_buy_store.get(today_str, {}).items(): all_buy_prices[int(h)] = float(normalize_float(p))
            for h, p in p_buy_store.get(tomorrow_str, {}).items(): all_buy_prices[int(h) + 24] = float(normalize_float(p))
        except Exception:
            pass

        simulated_soc = float(start_soc)
        overflow_kwh = 0.0
        history_log = {}
        
        for i, h_abs in enumerate(sim_range):
            real_h = int(h_abs % 24)
            is_tom = bool(h_abs >= 24)
            h_str = str(real_h)
            
            step_duration = float(fraction_left_h1 if i == 0 else 1.0)
            if step_duration <= 0.001: continue

            # 1. Generation Forecast
            if is_tom:
                if dist_tom:
                    total_dist = sum(dist_tom.values())
                    h_acc, _ = man.strategy_engine.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(dist_tom.get(h_str, 0.0) / total_dist * f_tom * blended_coeff * h_acc) if total_dist > 0.1 else 0.0
                else:
                    total_hist = sum(prof_gen_tom.values())
                    h_acc, _ = man.strategy_engine.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(normalize_float(prof_gen_tom.get(h_str, 0.0)) / total_hist * f_tom * blended_coeff * h_acc) if total_hist > 0.1 else 0.0
            else:
                if dist_today:
                    cur_h_weight = float(dist_today.get(h_str, 0.0))
                    rem_dist = (cur_h_weight * step_duration) + sum(float(dist_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    h_acc, _ = man.strategy_engine.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(cur_h_weight / rem_dist * f_today * blended_coeff * h_acc) if rem_dist > 0.1 else 0.0
                else:
                    cur_h_hist = float(prof_gen_today.get(h_str, 0.0))
                    rem_hist = (cur_h_hist * step_duration) + sum(float(prof_gen_today.get(str(hr), 0.0)) for hr in range(now.hour + 1, 24))
                    h_acc, _ = man.strategy_engine.get_hourly_accuracy_coeff(real_h)
                    expected_gen_kw = float(cur_h_hist / rem_hist * f_today * blended_coeff * h_acc) if rem_hist > 0.1 else 0.0
            
            # Night Clamp
            p_gen_check = prof_gen_tom if is_tom else prof_gen_today
            hist_h_val = float(normalize_float(p_gen_check.get(h_str, 0.0)))
            if hist_h_val < 0.01 and (real_h < 8 or real_h > 20):
                expected_gen_kw = 0.0

            # 2. Consumption Forecast
            p_cons = prof_cons_tom if is_tom else prof_cons_today
            occ_coeff, _, _, _, _, _, _ = man.get_occupancy_coefficient()
            expected_cons_kw = float(normalize_float(p_cons.get(h_str, 0.0))) * float(occ_coeff)
            
            if (real_h >= 22 or real_h <= 6) and expected_cons_kw > 3.0:
                expected_cons_kw = 0.5

            # Blending anchors for current hour
            if i == 0:
                anchor_weight = max(0.0, min(1.0, (now.minute / 60.0)))
                real_load = float(getattr(man, "avg_base_load_kw" if house_profile_override == "consumption_base" else "avg_load_kw", expected_cons_kw))
                expected_cons_kw = (real_load * anchor_weight) + (expected_cons_kw * (1.0 - anchor_weight))
                
                real_gen_kw = float(getattr(man, "avg_gen_kw", 0.0))
                if real_gen_kw > 0.01:
                    expected_gen_kw = (real_gen_kw * anchor_weight) + (expected_gen_kw * (1.0 - anchor_weight))

            # Losses correction
            if eff_coeff < 0.999:
                idle_p = float(prof_losses.get(h_str, 0.05))
                expected_cons_kw += idle_p

            if no_solar or h_abs in pv_curtail_hours:
                expected_gen_kw = 0.0

            # 3. Apply Mode Policy from Planner or direct commands
            plan = (planner_schedule or {}).get(int(h_abs))
            if not plan and int(h_abs) in commands:
                # Backward compatibility for simple power commands
                p_val = float(commands[int(h_abs)])
                plan = {
                    "mode": "buy" if p_val > 0 else ("sale_pv_bat" if p_val < 0 else "sale_pv"),
                    "power": abs(p_val)
                }
            
            if not plan:
                plan = {"mode": "sale_pv", "power": 0.0}

            mode_name = plan.get("mode", "sale_pv")
            mode_cfg = get_mode_config(mode_name)
            
            # Additional strategy overrides
            allow_charge = mode_cfg.allow_charge
            if no_battery_charge_until is not None and h_abs < no_battery_charge_until:
                allow_charge = False
            _h_price = float(normalize_float(all_buy_prices.get(int(h_abs), 0.1)))
            if mode_cfg.gen_policy == GenPolicy.FORBIDDEN:
                expected_gen_kw = 0.0
            elif mode_cfg.gen_policy == GenPolicy.HOUSE_ONLY:
                expected_gen_kw = min(expected_gen_kw, expected_cons_kw)
            elif mode_cfg.gen_policy == GenPolicy.NO_EXPORT:
                # In simulation, no_export means we only use PV for house and charging.
                # Selling is blocked, but we'll handle that in the power balance.
                pass
            elif mode_cfg.gen_policy == GenPolicy.THROTTLE_ON_NEGATIVE and _h_price <= 0.0:
                expected_gen_kw = 0.0

            # 4. Power Balance
            cmd_p = float(plan.get("power", 0.0))
            
            # Solar covers house first
            p_for_house = min(expected_gen_kw, expected_cons_kw)
            rem_gen = expected_gen_kw - p_for_house
            rem_cons = expected_cons_kw - p_for_house
            
            # Mode constraints on charge/discharge
            if not mode_cfg.allow_discharge:
                # House is grid-powered or discharge is forbidden
                total_net_kw = (rem_gen if allow_charge else 0.0) + cmd_p
            else:
                # Battery covers remaining house load
                net_pv = rem_gen if allow_charge else 0.0
                total_net_kw = net_pv - rem_cons + cmd_p

            # 5. Battery Delta
            if total_net_kw > 0.001:
                # Charge
                acc_ratio = get_cc_cv_ratio(simulated_soc)
                actual_charge_kw = float(min(total_net_kw * eff_coeff, max_batt_p * acc_ratio))
                old_soc = simulated_soc
                if b_cap_f > 0.1:
                    simulated_soc = float(min(100.0, simulated_soc + (actual_charge_kw * step_duration / b_cap_f * 100.0)))
                
                # Overflow (DC surplus that couldn't fit into battery)
                actual_stored_kwh_ac = 0.0
                if b_cap_f > 0.1:
                    actual_stored_kwh_ac = ((simulated_soc - old_soc) / 100.0 * b_cap_f) / max(0.1, eff_coeff)
                overflow_h = max(0.0, (total_net_kw * step_duration) - actual_stored_kwh_ac)
                overflow_kwh += overflow_h
                
            elif total_net_kw < -0.001 and mode_cfg.allow_discharge:
                # Discharge
                sim_eff = float(max(0.85, eff_coeff))
                actual_discharge_kw = float(min(abs(total_net_kw) / sim_eff, max_batt_p))
                if b_cap_f > 0.1:
                    simulated_soc = float(max(0.0, simulated_soc - (actual_discharge_kw * step_duration / b_cap_f * 100.0)))

            # Logging
            is_tom_log = (h_abs >= 24)
            log_key_str = f"{real_h:02d}:59" + (" (Завтра)" if is_tom_log else "")
            history_log[log_key_str] = {
                "soc": round_f(float(simulated_soc), 1),
                "gen_kw": round_f(float(expected_gen_kw), 3),
                "load_kw": round_f(float(expected_cons_kw), 3),
                "mode": mode_name
            }

        return float(simulated_soc), history_log, float(overflow_kwh)
