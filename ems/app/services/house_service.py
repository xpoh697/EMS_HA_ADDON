import datetime
import logging
from sqlalchemy import cast, Date
from app.models.database import SessionLocal, HouseHourlyStat

logger = logging.getLogger(__name__)

def save_hourly_house_stats(prev_hour_ts, house_tracking, current_sensors):
    """Calculates and persists the actual house consumption for the previous hour."""
    db = SessionLocal()
    try:
        cur_e = current_sensors.get("house_energy_today")
        
        # Fallback to integrated power
        fb_kwh = 0
        if house_tracking["sample_count"] > 0:
            avg_w = house_tracking["integration_sum_watts"] / house_tracking["sample_count"]
            fb_kwh = avg_w / 1000.0

        actual = 0
        # If we have energy sensor data and it's valid (incremental)
        if cur_e is not None and house_tracking["hour_start_energy"] is not None:
            actual = cur_e - house_tracking["hour_start_energy"] if cur_e >= house_tracking["hour_start_energy"] else cur_e
            # GIGANTIC CONSUMPTION FILTER: If delta > 15kWh per hour (unlikely for residential), use integration
            if actual > 15.0 or actual < -0.001:
                actual = fb_kwh
        else:
            actual = fb_kwh

        stat = HouseHourlyStat(
            timestamp=prev_hour_ts,
            hour=prev_hour_ts.hour,
            actual_kwh=float(actual)
        )
        db.add(stat)
        db.commit()
    except Exception as e:
        logger.error(f">>> HOUSE_SERVICE: Error saving house stats: {e}")
    finally:
        db.close()

async def repopulate_history_from_ha(db, ha_client, entity_id):
    """Reconstructs HouseHourlyStat records from HA history if local DB is empty."""
    try:
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = now.date()
        
        # Check existing stats for today
        existing_stats = db.query(HouseHourlyStat).filter(cast(HouseHourlyStat.timestamp, Date) == today).all()
        total_kwh = sum(s.actual_kwh for s in existing_stats)
        count_existing = len(existing_stats)
        
        logger.info(f">>> HOUSE_SERVICE: Today's stats in DB: count={count_existing}, sum={total_kwh:.3f} kWh")
        
        if count_existing >= 3 and total_kwh > 0.05:
            logger.info(">>> HOUSE_SERVICE: Valid data already exists. Skipping reconstruction.")
            return

        logger.info(f">>> HOUSE_SERVICE: Attempting aggressive reconstruction for {today}...")
        db.query(HouseHourlyStat).filter(cast(HouseHourlyStat.timestamp, Date) == today).delete()
        db.commit()

        logger.info(f">>> HOUSE_SERVICE: Attempting to reconstruct history from HA for {entity_id}")
        history = await ha_client.get_history(entity_id, today_start)
        
        if not history or not isinstance(history, list):
            logger.warning(f">>> HOUSE_SERVICE: No history found in HA for {entity_id}.")
            return

        # Group data by hour
        buckets = {h: [] for h in range(now.hour + 1)}
        for entry in history:
            try:
                v_str = entry.get("state") or entry.get("s")
                t_str = entry.get("last_updated") or entry.get("lu")
                if v_str is None or t_str is None or v_str in ["unknown", "unavailable"]:
                    continue
                
                val = float(v_str)
                dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                dt_local = dt.astimezone() 
                
                if dt_local.date() == now.date() and dt_local.hour <= now.hour:
                    buckets[dt_local.hour].append(val)
            except: continue

        reconstructed_count = 0
        for h in range(now.hour):
            vals = buckets.get(h, [])
            if len(vals) < 2: continue
            
            delta = max(vals) - min(vals)
            if delta < 0 or delta > 20.0: continue 

            stat = HouseHourlyStat(
                timestamp=today_start.replace(hour=h),
                hour=h,
                actual_kwh=float(delta)
            )
            db.add(stat)
            reconstructed_count += 1

        db.commit()
        if reconstructed_count > 0:
            logger.info(f">>> HOUSE_SERVICE: Reconstructed {reconstructed_count} hours of house history.")
    except Exception as e:
        logger.error(f">>> HOUSE_SERVICE: History reconstruction failed: {e}")
