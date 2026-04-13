import datetime
import logging

logger = logging.getLogger(__name__)

def extract_price_array(raw, target_date=None, is_solar=False, attr_name="", day_hint=None):
    """Parses raw HA attribute data into a 24-hour array of floats."""
    if not raw:
        return [0.0] * 24, False
        
    buckets = [[] for _ in range(24)]
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
        # 1.3.68 fix: Handle 48-hour lists for tomorrow
        if len(raw) >= 48 and day_hint == "tomorrow":
             return [float(x) for x in raw[24:48]], True
             
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
        # 1.3.68: Robust Local-Time Date Matching
        # dt is aware, convert to local system time before comparing dates
        local_dt = dt.astimezone() if dt.tzinfo else dt
        if target_dt_obj and local_dt.date() == target_dt_obj:
            h = dt.hour # Use original hour or local? Use local for display grouping
            h = local_dt.hour
            if 0 <= h <= 23:
                # MAGNITUDE FILTER: Relaxed
                if is_solar and (val > 100.0 and val < 1000.0):
                    continue
                buckets[h].append(val)
                found_count += 1

    # FALLBACK LOGIC: 
    if found_count == 0 and len(items) >= 24:
        day_tag = day_hint or ("today" if "today" in attr_name.lower() else ("tomorrow" if "tomorrow" in attr_name.lower() else None))
        
        target_is_today = (target_dt_obj == datetime.datetime.now().date())
        # Only fallback if day info matches the request intent
        if (day_tag == "today" and target_is_today) or (day_tag == "tomorrow" and not target_is_today):
            if day_tag == "tomorrow" and len(items) >= 48:
                logger.info(f">>> PRICE_SERVICE: Tomorrow fallback for {attr_name}. Slicing [24:48].")
                itms = items[24:48]
            else:
                logger.info(f">>> PRICE_SERVICE: Today fallback for {attr_name}. Slicing [0:24].")
                itms = items[:24]
                
            for i, (dt, val) in enumerate(itms):
                if i < 24:
                    buckets[i].append(val)
                    found_count += 1

    if found_count > 0:
        logger.info(f">>> PRICE_SERVICE: Parsed {attr_name}. {found_count} matches for {target_date}.")
    else:
        logger.warning(f">>> PRICE_SERVICE: No matches for {attr_name} on {target_date}. Total suspect items: {len(items)}")

    result = [0.0] * 24
    should_sum = is_solar and ("wh_hours" in attr_name.lower() or "energy" in attr_name.lower())
    
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        if should_sum:
            s = sum(vals)
            result[h] = round(s / 1000.0 if s > 150.0 else s, 3)
        else:
            result[h] = round(sum(vals) / len(vals), 3)
            
    return result, found_count
