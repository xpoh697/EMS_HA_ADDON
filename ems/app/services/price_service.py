import datetime
import logging

logger = logging.getLogger(__name__)

def extract_price_array(raw, target_date=None, is_solar=False, attr_name=""):
    """Parses raw HA attribute data into a 24-hour array of floats."""
    if not raw:
        return [0.0] * 24, False
        
    buckets = [[] for _ in range(24)]
    target_str = target_date.strftime("%Y-%m-%d") if target_date else None
    items = []
    
    # Support dict format (timestamps as keys)
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                dt = datetime.datetime.fromisoformat(k.replace('Z','+00:00').replace(' ','T'))
                items.append((dt, float(v)))
            except: continue
            
    # Support list format (common for Solcast, Pstryk)
    elif isinstance(raw, list):
        for item in raw:
            try:
                if isinstance(item, dict):
                    ts_str = item.get("period_start") or item.get("start") or item.get("time") or item.get("datetime")
                    if not ts_str: continue
                    dt = datetime.datetime.fromisoformat(ts_str.replace('Z','+00:00').replace(' ','T'))
                    
                    val = 0
                    for key in ["pv_estimate", "estimate", "value", "price", "total", "amount"]:
                        v = item.get(key)
                        if v is not None:
                            val = float(v)
                            break
                    items.append((dt, val))
                elif isinstance(item, (int, float)):
                    # Simple list of values - assumed to be sequential starting at 0:00 (legacy)
                    # We can't really date-filter these well without more context, but we handle them
                    pass
            except: continue

    found = False
    for dt, val in items:
        # Date filtering
        if target_str and dt.strftime("%Y-%m-%d") != target_str:
            continue
            
        h = dt.hour
        if 0 <= h <= 23:
            # MAGNITUDE FILTER: v1.3.47 fix to prevent 20kWh+ daily totals from contaminating hourly stats
            if is_solar and 20.0 < val < 1000.0:
                continue
            buckets[h].append(val)
            found = True

    result = [0.0] * 24
    # Some solar sensors provide cumulative Wh per hour (need summation)
    should_sum = is_solar and ("wh_hours" in attr_name.lower() or "energy" in attr_name.lower())
    
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        if should_sum:
            s = sum(vals)
            # Normalize Wh to kWh if needed
            result[h] = round(s / 1000.0 if s > 150.0 else s, 3)
        else:
            # Standard average for price/integrated power
            result[h] = round(sum(vals) / len(vals), 3)
            
    return result, found
