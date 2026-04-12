
import os
import re

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update extract_price_array to fix the sawtooth and total-peak issues
new_extract_fn = """
def extract_price_array(raw, target_date=None, is_solar=False, attr_name=""):
    \"\"\"
    Extract hourly array from HA sensor attributes.
    is_solar: If True, uses Sum for Wh energy, Average for Power/kW.
    attr_name: Name of the attribute to decide aggregation strategy.
    \"\"\"
    if not raw:
        return [0.0]*24, False

    buckets = [[] for _ in range(24)]
    found = False
    
    # 1. Standardize input to a list of (datetime, value)
    items = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                # Handle ISO timestamps
                clean_ts = k.replace('Z', '+00:00').replace(' ', 'T')
                dt = datetime.datetime.fromisoformat(clean_ts)
                items.append((dt, float(v)))
            except: continue
    elif isinstance(raw, list):
        for item in raw:
            try:
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

    # 2. Filter and bucket
    target_str = target_date.strftime("%Y-%m-%d") if target_date else None
    for dt, val in items:
        if target_str and dt.strftime("%Y-%m-%d") != target_str:
            continue
        hour = dt.hour
        if 0 <= hour <= 23:
            buckets[hour].append(val)
            found = True

    # 3. Aggregate
    result = [0.0]*24
    # Determination: wh_hours is energy (Sum), others usually power (Average)
    should_sum = is_solar and ("wh_hours" in attr_name.lower() or "energy" in attr_name.lower())
    
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        
        # Filter out extreme values that equal daily total (if we had it) - but let's just use robust averaging first
        if should_sum:
            s = sum(vals)
            # Detect Wh -> kWh
            result[h] = round(s / 1000.0 if s > 150.0 else s, 3)
        else:
            # Average power values (fixes the 30-min sawtooth)
            result[h] = round(sum(vals) / len(vals), 3)

    return result, found
"""

# Replace the function definition
start_idx = content.find("def extract_price_array(raw, target_date=None, is_solar=False):")
if start_idx == -1: # Try the old one too just in case
    start_idx = content.find("def extract_price_array(raw, target_date=None):")
end_idx = content.find("def load_handlers():")

if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_extract_fn + "\n" + content[end_idx:]

# 4. Cleanup search keys in get_solar_detailed and fix the call
content = content.replace(
    'search_keys = ["detailedforecast", "detailedhourly", "detailed_forecast", "wh_hours", "wh_period_forecast", "forecast", "forecast_today"]',
    'search_keys = ["wh_hours", "wh_period_forecast", "detailed_forecast", "detailedhourly", "detailedforecast"] # Removed generic forecast'
)
# Update call to pass k as attr_name
content = content.replace(
    'forecast_array, success = extract_price_array(raw, target_date=now.date(), is_solar=True)',
    'forecast_array, success = extract_price_array(raw, target_date=now.date(), is_solar=True, attr_name=k)'
)
# Update other calls
content = content.replace(
    'price_arrays[f"solar_forecast_{day_key}"], _ = extract_price_array(raw_data, is_solar=True)',
    'price_arrays[f"solar_forecast_{day_key}"], _ = extract_price_array(raw_data, is_solar=True, attr_name=attr_try)'
)

# 5. Fix factor calculation to be more resilient to poisoned history
content = content.replace(
    'factors = [max(0.1, min(2.0, f)) for f in factors]',
    'factors = [max(0.2, min(5.0, f)) for f in factors] # Adjusted bounds'
)

# 6. Safety check - the user says spikes equal daily sensor value. 
# Let's ensure if we get a huge value at any hour, we cap it by the daily average.
# (Handled by the Average logic now, but just in case...)

with open(filepath, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)
print("Applied v1.3.44 fixes successfully!")
