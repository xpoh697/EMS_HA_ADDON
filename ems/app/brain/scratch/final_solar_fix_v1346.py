
import os
import re

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update extract_price_array with the strict 20kWh cap and better logic
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
                # Handle ISO timestamps. Values like 'total' will fail here.
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
            # MAGNITUDE FILTER: 
            # If a single hourly value is > 20.0 (kWh) it is definitely the Daily Total.
            # No residential inverter produces 20kWh within a single hour slot.
            if is_solar and 20.0 < val < 1000.0:
                logger.info(f"Skipping value {val} at hour {hour} - identified as potential daily total peak.")
                continue 
            buckets[hour].append(val)
            found = True

    # 3. Aggregate
    result = [0.0]*24
    should_sum = is_solar and ("wh_hours" in attr_name.lower() or "energy" in attr_name.lower())
    
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        
        if should_sum:
            s = sum(vals)
            # Detect Wh -> kWh conversion
            result[h] = round(s / 1000.0 if s > 150.0 else s, 3)
        else:
            # Average power values (fixes the 30-min sawtooth)
            result[h] = round(sum(vals) / len(vals), 3)

    return result, found
"""

# 2. Add the Startup Reset Logic
startup_reset_code = """
def run_once_v1346_reset():
    \"\"\"Wipes contaminated solar history to reset correction factors (Run once).\"\"\"
    db = SessionLocal()
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "v1346_reset_done").first()
        if not setting:
            logger.info(">>> v1.3.46 STARTUP: Clearing poisoned solar history to reset correction factors...")
            from app.models.database import SolarHourlyStat
            db.query(SolarHourlyStat).delete()
            
            reset_flag = SystemSetting(key="v1346_reset_done", value={"done": True})
            db.add(reset_flag)
            db.commit()
            logger.info(">>> v1.3.46 STARTUP: Solar history reset successful!")
        else:
            logger.info(">>> v1.3.46 STARTUP: History reset already done.")
    except Exception as e:
        logger.error(f"Failed to perform v1.3.46 reset: {e}")
    finally:
        db.close()

run_once_v1346_reset()
"""

# Apply the function replacement
start_idx = content.find("def extract_price_array(raw, target_date=None, is_solar=False, attr_name=\"\"):")
end_idx = content.find("def load_handlers():")

if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_extract_fn + "\n" + content[end_idx:]

# Find a good place for the startup reset (after init_db)
init_db_idx = content.find("init_db()")
if init_db_idx != -1:
    insert_pos = content.find("\n", init_db_idx) + 1
    content = content[:insert_pos] + startup_reset_code + "\n" + content[insert_pos:]

with open(filepath, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)
print("Applied v1.3.46 fixes with startup reset successfully!")
