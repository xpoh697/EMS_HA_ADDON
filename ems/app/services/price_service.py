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

    # Clean up targets
    target_dt_obj = target_date if isinstance(target_date, datetime.date) else None
    
    found_count = 0
    for dt, val in items:
        # Robust Date Matching: Compare .date() objects directly
        match = False
        if target_dt_obj and dt.date() == target_dt_obj:
            match = True
            
        if match:
            h = dt.hour
            if 0 <= h <= 23:
                # MAGNITUDE FILTER: Relaxed (>100kWh or >100000Wh)
                if is_solar and (val > 100.0 and val < 1000.0):
                    continue
                buckets[h].append(val)
                found_count += 1

    # FALLBACK LOGIC: If no items matched by date, but we have a 24-hour sequence 
    # in an attribute clearly labeled "today" or "tomorrow", trust the sequence.
    if found_count == 0 and len(items) >= 24:
        # Check if attribute name matches our target day
        day_tag = "today" if target_dt_obj == datetime.datetime.now().date() else "tomorrow"
        if day_tag in attr_name.lower():
            logger.info(f">>> PRICE_SERVICE: Fallback triggered for {attr_name}. Trusting sequence as {day_tag}.")
            for i, (dt, val) in enumerate(items[:24]):
                buckets[i].append(val)
                found_count += 1

    if found_count > 0:
        logger.info(f">>> PRICE_SERVICE: Parsed {attr_name}. {found_count} matches for {target_date}.")
    else:
        logger.warning(f">>> PRICE_SERVICE: No matches for {attr_name} on {target_date}. Total suspect items: {len(items)}")

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
