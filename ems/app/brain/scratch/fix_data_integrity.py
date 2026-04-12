
import os
import re

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Replace extract_price_array with a more robust version
new_extract_fn = """
def extract_price_array(raw, target_date=None, is_solar=False):
    \"\"\"
    Extract hourly array from HA sensor attributes.
    target_date: datetime.date object to filter for (usually Today or Tomorrow).
    is_solar: If True, values are SUMMED (Energy Wh->kWh). If False, values are AVERAGED (Price).
    Returns (24_item_list, success_bool)
    \"\"\"
    if not raw:
        return [0.0]*24, False

    buckets = [[] for _ in range(24)]
    found = False
    
    # Standardize input to a list of (datetime, value)
    items = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                # Handle ISO timestamps or relative keys
                clean_ts = k.replace('Z', '+00:00').replace(' ', 'T')
                dt = datetime.datetime.fromisoformat(clean_ts)
                items.append((dt, float(v)))
            except: continue
    elif isinstance(raw, list):
        for item in raw:
            try:
                if isinstance(item, (int, float)):
                    # Simple list assumes index is hour? No, that's brittle. 
                    # We only support lists of values if they match 24/48 items.
                    continue 
                if isinstance(item, dict):
                    ts_str = item.get("period_start") or item.get("start") or item.get("time") or item.get("datetime")
                    if not ts_str: continue
                    clean_ts = ts_str.replace('Z', '+00:00').replace(' ', 'T')
                    dt = datetime.datetime.fromisoformat(clean_ts)
                    val = 0
                    for key in ["pv_estimate", "estimate", "value", "price", "total", "amount"]:
                        v = item.get(key)
                        if v is not None:
                            val = float(v)
                            break
                    items.append((dt, val))
            except: continue

    # Filter and bucket
    target_str = target_date.strftime("%Y-%m-%d") if target_date else None
    for dt, val in items:
        if target_str and dt.strftime("%Y-%m-%d") != target_str:
            continue
        hour = dt.hour
        if 0 <= hour <= 23:
            buckets[hour].append(val)
            found = True

    # Aggregate
    result = [0.0]*24
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        if is_solar:
            # Solar Summation (Wh -> kWh detection)
            s = sum(vals)
            if s > 100.0: # Likely Wh
                result[h] = round(s / 1000.0, 3)
            else:
                result[h] = round(s, 3)
        else:
            # Price/Power Averaging
            result[h] = round(sum(vals) / len(vals), 4)

    return result, found
"""

# Find old function and replace
fn_pattern = re.compile(r"def extract_price_array\(raw, target_date=None\):.*?return result, True.*?# Minimalist list", re.DOTALL)
# Actually, I'll just write a cleaner replacement for the whole function block.
# Let's find the start of extract_price_array and the next function.
start_idx = content.find("def extract_price_array(raw, target_date=None):")
end_idx = content.find("def load_handlers():")

if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_extract_fn + "\n" + content[end_idx:]
    print("Replaced extract_price_array successfully!")

# 2. Fix save_hourly_solar_stats to be more robust against cumulative sensor jumps
old_stat_save = """        if current_energy and solar_tracking["hour_start_energy"]:
            # Handle possible sensor resets (today sensors)
            if current_energy >= solar_tracking["hour_start_energy"]:
                actual_kwh = current_energy - solar_tracking["hour_start_energy"]
            else:
                actual_kwh = current_energy # Reset happened
            
            # Robust verification: If Energy delta is 0 or too small, but we have Power integration, use Power
            if actual_kwh < 0.01 and fallback_kwh > 0.1:
                actual_kwh = fallback_kwh
                logger.info("Using Power-integration fallback for Solar Fact data.")
        else:
            actual_kwh = fallback_kwh"""

new_stat_save = """        if current_energy and solar_tracking["hour_start_energy"]:
            # Handle possible sensor resets (today sensors)
            if current_energy >= solar_tracking["hour_start_energy"]:
                actual_kwh = current_energy - solar_tracking["hour_start_energy"]
            else:
                actual_kwh = current_energy # Reset happened
            
            # CRITICAL: If the delta is suspiciously high (> 20kWh in an hour), it's likely a mis-sync with a cumulative sensor
            if actual_kwh > 20.0:
                logger.warning(f"Suspiciously high solar delta ({actual_kwh}kWh). Falling back to power integration.")
                actual_kwh = fallback_kwh
                
            # Robust verification: If Energy delta is 0 or too small, but we have Power integration, use Power
            if actual_kwh < 0.01 and fallback_kwh > 0.05:
                actual_kwh = fallback_kwh
                logger.info("Using Power-integration fallback for Solar Fact data.")
        else:
            # If we don't have a starting snapshot (e.g. after restart), use power integration ONLY
            actual_kwh = fallback_kwh
            logger.info("Starting energy snapshot missing. Used Power-integration fallback.")"""

if old_stat_save in content:
    content = content.replace(old_stat_save, new_stat_save)
    print("Fixed save_hourly_solar_stats successfully!")

# 3. Update calls to extract_price_array to pass is_solar=True
content = content.replace(
    'price_arrays[f"solar_forecast_{day_key}"], _ = extract_price_array(raw_data)',
    'price_arrays[f"solar_forecast_{day_key}"], _ = extract_price_array(raw_data, is_solar=True)'
)
content = content.replace(
    'forecast_array, success = extract_price_array(raw, target_date=now.date())',
    'forecast_array, success = extract_price_array(raw, target_date=now.date(), is_solar=True)'
)

with open(filepath, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)
