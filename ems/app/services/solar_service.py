import datetime
import logging
from app.models.database import SessionLocal, SolarHourlyStat

logger = logging.getLogger(__name__)

def save_hourly_solar_stats(prev_hour_ts, solar_tracking, current_sensors, price_arrays):
    \"\"\"Calculates and persists the actual solar generation for the previous hour.\"\"\"
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
    \"\"\"Calculates correction factors (Actual/Forecast) over the last 14 days.\"\"\"
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
