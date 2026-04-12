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
        # Legacy/Flat-list support (v1.3.48 logic)
        if len(raw) >= 24 and all(isinstance(x, (int, float)) for x in raw[:24]):
            return [float(x) for x in raw[:24]], True
            
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
            except: continue

    found = False
    for dt, val in items:
        # Better date matching using .date() objects
        if target_date and dt.date() != target_date:
            continue
            
        h = dt.hour
        if 0 <= h <= 23:
            # MAGNITUDE FILTER: Relaxed to avoid filtering Wh-based sensors. 
            # Only filter truly impossible hourly peaks (>100kWh or >100000Wh).
            if is_solar and (val > 100.0 and val < 1000.0): # Skip mid-range trash if suspected kWh
                continue
            if val > 100000.0: # Impossible Wh
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
