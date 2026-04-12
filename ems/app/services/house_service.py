import datetime
import logging
from app.models.database import SessionLocal, HouseHourlyStat

logger = logging.getLogger(__name__)

def save_hourly_house_stats(prev_hour_ts, house_tracking, current_sensors):
    \"\"\"Calculates and persists the actual house consumption for the previous hour.\"\"\"
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
