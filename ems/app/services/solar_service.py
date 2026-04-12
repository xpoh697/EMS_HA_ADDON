import datetime
import logging
from app.models.database import SessionLocal, SolarHourlyStat

logger = logging.getLogger(__name__)

def save_hourly_solar_stats(prev_hour_ts, solar_tracking, current_sensors, price_arrays):
    """Calculates and persists the actual solar generation for the previous hour."""
    db = SessionLocal()
    try:
        cur_e = current_sensors.get("solar_energy_total")
        
        # Fallback to integrated power if energy sensor is missing or stale
        fb_kwh = 0
        if solar_tracking["sample_count"] > 0:
            avg_w = solar_tracking["integration_sum_watts"] / solar_tracking["sample_count"]
            fb_kwh = max(0, avg_w / 1000.0) # Power Integration (Wh) converted to kWh block

        actual = 0
        if cur_e and solar_tracking["hour_start_energy"] is not None:
            # Handle possible counter resets by using fallback if current < start
            actual = cur_e - solar_tracking["hour_start_energy"] if cur_e >= solar_tracking["hour_start_energy"] else cur_e
            
            # MAGNITUDE SANITY CHECK: v1.3.47 fix. 
            # If delta is huge (>20kWh) or tiny when integration says otherwise, use integration fallback.
            if actual > 20.0 or actual < 0.001:
                actual = fb_kwh
        else:
            actual = fb_kwh

        h_idx = prev_hour_ts.hour
        f_arr = price_arrays.get("solar_forecast_today", [])
        forecast = f_arr[h_idx] if h_idx < len(f_arr) else 0

        stat = SolarHourlyStat(
            timestamp=prev_hour_ts,
            hour=h_idx,
            actual_kwh=float(actual),
            forecast_kwh=float(forecast)
        )
        db.add(stat)
        db.commit()
        logger.info(f">>> SOLAR_SERVICE: Saved H{h_idx} stat: {actual:.2f}kWh (Actual) vs {forecast:.2f}kWh (Forecast)")
    except Exception as e:
        logger.error(f">>> SOLAR_SERVICE: Error saving solar stats: {e}")
    finally:
        db.close()

def get_solar_correction_factors():
    """Calculates correction factors (Actual/Forecast) over the last 14 days."""
    db = SessionLocal()
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=14)
        history = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp > cutoff).all()
        
        if not history:
            return {h: 1.0 for h in range(24)}

        stats = {h: {"actual": 0.0, "forecast": 0.0} for h in range(24)}
        for entry in history:
            stats[entry.hour]["actual"] += entry.actual_kwh
            stats[entry.hour]["forecast"] += entry.forecast_kwh

        factors = {}
        for h in range(24):
            f = stats[h]["forecast"]
            a = stats[h]["actual"]
            # Clamp factor between 0.1 and 3.0 to prevent extreme oscillations
            factors[h] = min(max(a / f, 0.1), 3.0) if f > 0.05 else 1.0
            
        return factors
    finally:
        db.close()

async def repopulate_history_from_ha(db, ha_client, entity_id, price_arrays):
    """Reconstructs SolarHourlyStat records from HA history if local DB is empty."""
    try:
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Check if we already have data for today
        today = now.date()
        existing_stats = db.query(SolarHourlyStat).filter(SolarHourlyStat.date == today).all()
        total_kwh = sum(s.actual_kwh for s in existing_stats)
        count_existing = len(existing_stats)
        
        logger.info(f">>> SOLAR_SERVICE: Today's stats in DB: count={count_existing}, sum={total_kwh:.3f} kWh")
        
        # Aggressive reconstruction: if we have zero data (< 0.05 kWh) or very few records (< 3)
        if count_existing >= 3 and total_kwh > 0.05:
            logger.info(">>> SOLAR_SERVICE: Valid data already exists. Skipping reconstruction.")
            return
            
        logger.info(f">>> SOLAR_SERVICE: Attempting aggressive reconstruction for {today}...")
        # Clear existing 'poison' records for today
        db.query(SolarHourlyStat).filter(SolarHourlyStat.date == today).delete()
        db.commit()

        logger.info(f">>> SOLAR_SERVICE: Attempting to reconstruct history from HA for {entity_id}")
        history = await ha_client.get_history(entity_id, today_start)
        
        if not history or not isinstance(history, list):
            logger.warning(f">>> SOLAR_SERVICE: No history found in HA for {entity_id}.")
            return

        logger.info(f">>> SOLAR_SERVICE: HA History returned {len(history)} items for {entity_id}")

        # Group data by hour
        buckets = {h: [] for h in range(now.hour + 1)}
        for entry in history:
            try:
                # HA history might have 's' (state) and 'lu' (last_updated) in minimal_response
                v_str = entry.get("state") or entry.get("s")
                t_str = entry.get("last_updated") or entry.get("lu")
                if v_str is None or t_str is None or v_str in ["unknown", "unavailable"]:
                    continue
                
                val = float(v_str)
                dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                # Convert to local time for grouping
                dt_local = dt.astimezone() 
                
                if dt_local.date() == now.date() and dt_local.hour <= now.hour:
                    buckets[dt_local.hour].append(val)
            except: continue

        forecast_today = price_arrays.get("solar_forecast_today", [0.0]*24)
        reconstructed_count = 0
        
        for h in range(now.hour):
            # Already have record for this hour?
            h_start = today_start.replace(hour=h)
            exists = db.query(SolarHourlyStat).filter(
                SolarHourlyStat.timestamp >= h_start,
                SolarHourlyStat.timestamp < h_start + datetime.timedelta(hours=1)
            ).first()
            if exists: continue

            vals = buckets.get(h, [])
            if len(vals) < 2: continue
            
            # For cumulative energy today sensor: Hour production = Max - Min in that hour
            delta = max(vals) - min(vals)
            
            # Basic sanity check
            if delta < 0 or delta > 20.0: continue 

            stat = SolarHourlyStat(
                timestamp=h_start,
                hour=h,
                actual_kwh=float(delta),
                forecast_kwh=float(forecast_today[h]) if h < len(forecast_today) else 0.0
            )
            db.add(stat)
            reconstructed_count += 1

        db.commit()
        if reconstructed_count > 0:
            logger.info(f">>> SOLAR_SERVICE: Reconstructed {reconstructed_count} hours of solar history.")
    except Exception as e:
        logger.error(f">>> SOLAR_SERVICE: History reconstruction failed: {e}")
