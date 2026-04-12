from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from app.services.inverter import InverterController
from app.services.boiler import BoilerManager
from app.services.loads import CyclicLoadHandler
from app.services.occupancy import OccupancyEngine
from app.services.guardian import PowerGuardian
from app.providers.hass import HomeAssistantClient
from app.models.database import init_db, SessionLocal, SystemSetting
import logging
import os
import json
import datetime

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EMS Control API")
init_db()

# Core Components
inverter = InverterController(dry_run=True)
occupancy = OccupancyEngine()
guardian = PowerGuardian(max_grid_power_w=11000.0)

# HA Client using Supervisor Token or fallback
ha_token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "REPLACE_ME")

# Read options for manual token fallback
if os.path.exists("/data/options.json"):
    try:
        with open("/data/options.json", "r") as f:
            opts = json.load(f)
            manual_token = opts.get("ha_token")
            if manual_token and len(manual_token) > 20:
                ha_token = manual_token
                logger.info("Using manual HA_TOKEN from configuration options.")
    except Exception as e:
        logger.warning(f"Could not read options.json: {e}")

# Try to load token from DB if not found yet or to keep it persistent
db = SessionLocal()
token_setting = db.query(SystemSetting).filter(SystemSetting.key == "ha_token").first()
if token_setting and token_setting.value:
    ha_token = token_setting.value
    logger.info("Using HA_TOKEN from database.")
db.close()

ha_client = HomeAssistantClient(base_url="http://supervisor/core/api", token=ha_token)

# Dynamic Handlers
handlers = []

def load_handlers():
    """Load load managers from database config."""
    global handlers
    db = SessionLocal()
    setting = db.query(SystemSetting).filter(SystemSetting.key == "loads").first()
    db.close()
    
    new_handlers = []
    if setting:
        load_configs = setting.value  # List of dicts
        for cfg in load_configs:
            if cfg["type"] == "boiler":
                h = BoilerManager(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"])
                h.target_temp = cfg.get("target_temp", 60)
                new_handlers.append(h)
            elif cfg["type"] == "cyclic":
                h = CyclicLoadHandler(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"])
                new_handlers.append(h)
    
    handlers = new_handlers
    logger.info(f"Loaded {len(handlers)} handlers from database.")

async def update_ha_config():
    """Fetch system config from HA like currency."""
    if ha_client.auth_failed:
        logger.warning("Skipping HA config fetch: Auth failed.")
        return
    config = await ha_client.get_config()
    if config:
        current_sensors["currency"] = config.get("currency", "EUR")
        logger.info(f"HA Currency: {current_sensors['currency']}")

# State
current_sensors = {
    "battery_soc": 0, "solar_power": 0, "buy_price": 0, "sell_price": 0, "house_power": 0,
    "survival_soc": 20, "price_tomorrow": 0, "currency": "EUR", "current_hour": 0,
    "solar_forecast_today": 0, "solar_forecast_tomorrow": 0,
    "solar_energy_total": 0, "solar_energy_today": 0, "house_energy_today": 0
}

# Sensor tracking state (Persisted)
solar_tracking = {
    "hour_start_ts": None,
    "integration_sum_watts": 0,
    "sample_count": 0,
    "hour_start_energy": None,
    "day_start_energy": None,
    "last_hourly_stats": [] # 24h history for chart
}

house_tracking = {
    "hour_start_ts": None,
    "integration_sum_watts": 0,
    "sample_count": 0,
    "hour_start_energy": None
}

last_state_save_ts = datetime.datetime.min

def save_tracking_states(force=False):
    """Persists tracking objects to DB with 5-minute cooldown."""
    global last_state_save_ts
    now = datetime.datetime.now()
    if not force and (now - last_state_save_ts).total_seconds() < 300: # 5 Minutes
        return

    db = SessionLocal()
    try:
        state = {
            "solar": {
                "hour_start_ts": solar_tracking["hour_start_ts"].isoformat() if solar_tracking["hour_start_ts"] else None,
                "integration_sum_watts": solar_tracking["integration_sum_watts"],
                "sample_count": solar_tracking["sample_count"],
                "hour_start_energy": solar_tracking["hour_start_energy"]
            },
            "house": {
                "hour_start_ts": house_tracking["hour_start_ts"].isoformat() if house_tracking["hour_start_ts"] else None,
                "integration_sum_watts": house_tracking["integration_sum_watts"],
                "sample_count": house_tracking["sample_count"],
                "hour_start_energy": house_tracking["hour_start_energy"]
            }
        }
        setting = db.query(SystemSetting).filter(SystemSetting.key == "tracking_state").first()
        if not setting:
            setting = SystemSetting(key="tracking_state", value=state)
            db.add(setting)
        else:
            setting.value = state
        db.commit()
        last_state_save_ts = now
    except Exception as e:
        logger.error(f"Failed to save tracking states: {e}")
    finally:
        db.close()

def load_tracking_states():
    """Loads tracking objects from DB."""
    global solar_tracking, house_tracking
    db = SessionLocal()
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "tracking_state").first()
        if setting and setting.value:
            s_data = setting.value.get("solar", {})
            h_data = setting.value.get("house", {})
            
            # Solar
            if s_data.get("hour_start_ts"):
                solar_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(s_data["hour_start_ts"])
                solar_tracking["integration_sum_watts"] = s_data.get("integration_sum_watts", 0)
                solar_tracking["sample_count"] = s_data.get("sample_count", 0)
                solar_tracking["hour_start_energy"] = s_data.get("hour_start_energy")
            
            # House
            if h_data.get("hour_start_ts"):
                house_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(h_data["hour_start_ts"])
                house_tracking["integration_sum_watts"] = h_data.get("integration_sum_watts", 0)
                house_tracking["sample_count"] = h_data.get("sample_count", 0)
                house_tracking["hour_start_energy"] = h_data.get("hour_start_energy")
            
            logger.info("Universal tracking states loaded from database.")
    except Exception as e:
        logger.error(f"Failed to load tracking states: {e}")
    finally:
        db.close()

load_tracking_states()

# Price arrays for the chart
price_arrays = {
    "buy_prices_tomorrow": [],
    "sell_prices_tomorrow": [],
    "solar_forecast_today": [],
    "solar_forecast_tomorrow": []
}

def get_sensor_value(state_obj: dict, attr_name: str = None):
    """Extract value from state or attribute."""
    if not state_obj: return 0
    try:
        if attr_name and attr_name in state_obj.get("attributes", {}):
            return float(state_obj["attributes"][attr_name])
        return float(state_obj.get("state", 0))
    except (ValueError, TypeError):
        return 0

def extract_price_array(raw, target_date=None):
    """
    Extract hourly price/solar array from various HA sensor attribute formats.
    Returns (list_of_values, found_target_date_match)
    """
    if not raw:
        return [], False

    # 1. Handle Dictionary of ISO timestamps (e.g., Solcast wh_hours)
    if isinstance(raw, dict):
        try:
            sorted_keys = sorted(raw.keys())
            return [float(raw[k]) for k in sorted_keys], True
        except:
            return [], False

    # 2. Handle List formats
    if isinstance(raw, list):
        if not raw: return [], False
        
        # Check if first item is a dict with timestamps
        first = raw[0]
        if isinstance(first, dict) and any(k in first for k in ["period_start", "start", "time", "datetime"]):
            # Timestamp aggregation mode
            buckets = [0.0] * 24
            counts = [0] * 24
            target_str = target_date.strftime("%Y-%m-%d") if target_date else None
            found_target = False
            
            for item in raw:
                try:
                    ts_str = item.get("period_start") or item.get("start") or item.get("time") or item.get("datetime")
                    if not ts_str: continue
                    
                    # Robust ISO parsing
                    clean_ts = ts_str.replace('Z', '+00:00').replace(' ', 'T')
                    dt = datetime.datetime.fromisoformat(clean_ts)
                    
                    # Filter by date if requested
                    if target_str and dt.strftime("%Y-%m-%d") != target_str:
                        continue
                    
                    hour = dt.hour
                    
                    # Correctly handle 0.0 values
                    val = 0
                    for key in ["pv_estimate", "estimate", "value", "price", "total"]:
                        v = item.get(key)
                        if v is not None:
                            val = v
                            break
                    
                    buckets[hour] += float(val)
                    counts[hour] += 1
                    found_target = True
                except: continue
                
            if found_target:
                # Return averaged values
                result = []
                for i in range(24):
                    if counts[i] > 0:
                        result.append(round(buckets[i] / counts[i], 3))
                    else:
                        result.append(buckets[i])
                return result, True

        # Minimalist list or list of dicts without timestamps
        result = []
        for item in raw:
            if isinstance(item, (int, float)):
                result.append(float(item))
            elif isinstance(item, dict):
                val = 0
                for key in ["pv_estimate", "estimate", "value", "price", "total"]:
                    v = item.get(key)
                    if v is not None:
                        val = v
                        break
                try: result.append(float(val))
                except: result.append(0.0)
        return result, len(result) > 0

    return [], False

async def save_hourly_solar_stats(prev_hour_ts):
    """Calculates and saves hourly solar metrics to the database."""
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        # 1. Calculate Actual Energy (kWh)
        actual_kwh = 0
        current_energy = current_sensors.get("solar_energy_total")
        
        # Power integration fallback (Watt-hours -> kWh)
        fallback_kwh = 0
        if solar_tracking["sample_count"] > 0:
            avg_watts = solar_tracking["integration_sum_watts"] / solar_tracking["sample_count"]
            fallback_kwh = max(0, avg_watts / 1000.0)
            
        if current_energy and solar_tracking["hour_start_energy"]:
            # Handle possible sensor resets (today sensors)
            if current_energy >= solar_tracking["hour_start_energy"]:
                actual_kwh = current_energy - solar_tracking["hour_start_energy"]
            else:
                actual_kwh = current_energy # Reset happened
            
            # Robust verification: If Energy delta is 0 or too small, but we have Power integration, use Power
            if actual_kwh < 0.01 and fallback_kwh > 0.1:
                actual_kwh = fallback_kwh
                logger.info("Using Power-integration fallback for Solar Fact data.")
        else:
            actual_kwh = fallback_kwh

        # 2. Get Forecast from Extracted Array
        h_idx = prev_hour_ts.hour
        solar_forecast_array = price_arrays.get("solar_forecast_today", [])
        forecast_kwh = solar_forecast_array[h_idx] if h_idx < len(solar_forecast_array) else 0
        
        # 3. Save to DB
        stat = SolarHourlyStat(
            timestamp=prev_hour_ts,
            hour=prev_hour_ts.hour,
            actual_kwh=float(actual_kwh),
            forecast_kwh=float(forecast_kwh)
        )
        db.add(stat)
        db.commit()
        logger.info(f"Saved hourly solar stats for {prev_hour_ts.hour}:00. Actual: {actual_kwh:.2f}kWh, Forecast: {forecast_kwh:.2f}kWh")
        
        # 4. Prune old data (> 30 days)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=30)
        db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp < cutoff).delete()
        db.commit()
        
    except Exception as e:
        logger.error(f"Failed to save solar stats: {e}")
    finally:
        db.close()

async def save_hourly_house_stats(prev_hour_ts):
    """Calculates and saves hourly house consumption metrics to the database."""
    from app.models.database import HouseHourlyStat
    db = SessionLocal()
    try:
        # 1. Calculate Actual Energy (kWh)
        actual_kwh = 0
        current_energy = current_sensors.get("house_energy_today")
        
        if current_energy and house_tracking["hour_start_energy"]:
            # Use delta from today's energy sensor. Handle midnight resets (current < start)
            if current_energy >= house_tracking["hour_start_energy"]:
                actual_kwh = current_energy - house_tracking["hour_start_energy"]
            else:
                # Sensor reset (midnight)? Use current value as the delta for this hour
                actual_kwh = current_energy
        elif house_tracking["sample_count"] > 0:
            # Fallback: Integrate Watts
            avg_watts = house_tracking["integration_sum_watts"] / house_tracking["sample_count"]
            actual_kwh = avg_watts / 1000.0 # Wh -> kWh (assuming 1 hour)
        
        # 2. Save to DB
        stat = HouseHourlyStat(
            timestamp=prev_hour_ts,
            hour=prev_hour_ts.hour,
            actual_kwh=float(actual_kwh)
        )
        db.add(stat)
        db.commit()
        logger.info(f"Saved hourly house stats for {prev_hour_ts.hour}:00. Actual: {actual_kwh:.2f}kWh")
        
        # 3. Prune old data (N * 7 days)
        settings = await get_settings()
        history_weeks = settings.get("strategy_limits", {}).get("history_weeks", 4)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=int(history_weeks) * 7)
        db.query(HouseHourlyStat).filter(HouseHourlyStat.timestamp < cutoff).delete()
        db.commit()
        
    except Exception as e:
        logger.error(f"Failed to save house stats: {e}")
    finally:
        db.close()

def get_solar_correction_factors():
    """Calculates per-hour multipliers based on past 14 days of history."""
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=14)
        history = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp > cutoff).all()
        
        if not history:
            return {h: 1.0 for h in range(24)}
            
        # Group by hour
        stats = {h: {"actual": 0, "forecast": 0} for h in range(24)}
        for entry in history:
            stats[entry.hour]["actual"] += entry.actual_kwh
            stats[entry.hour]["forecast"] += entry.forecast_kwh
            
        # Calculate multipliers
        factors = {}
        for h in range(24):
            f = stats[h]["forecast"]
            a = stats[h]["actual"]
            if f > 0.05: # Ignore very small forecasts
                multiplier = a / f
                factors[h] = min(max(multiplier, 0.1), 3.0) # Cap at 0.1x to 3x
            else:
                factors[h] = 1.0
        return factors
    finally:
        db.close()

async def sensor_poller():
    """Background task to fetch sensors from HA and track stats."""
    while True:
        try:
            now = datetime.datetime.now()
            
            # SINGLE Hour transition check (Consolidated)
            if solar_tracking["hour_start_ts"] is None:
                solar_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                house_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                save_tracking_states(force=True)

            if now.hour != solar_tracking["hour_start_ts"].hour:
                prev_hour_ts = solar_tracking["hour_start_ts"]
                logger.info(f"Hour transition detected: {prev_hour_ts.hour}:00 -> {now.hour}:00. Saving stats...")
                
                # 1. Save Stats (using currently accumulated trackers)
                await save_hourly_solar_stats(prev_hour_ts)
                await save_hourly_house_stats(prev_hour_ts)
                
                # 2. Reset Trackers for next hour
                solar_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                solar_tracking["integration_sum_watts"] = 0
                solar_tracking["sample_count"] = 0
                solar_tracking["hour_start_energy"] = current_sensors.get("solar_energy_total")
                
                house_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                house_tracking["integration_sum_watts"] = 0
                house_tracking["sample_count"] = 0
                house_tracking["hour_start_energy"] = current_sensors.get("house_energy_today")
                
                save_tracking_states(force=True)
                logger.info("Trackers reset and persisted for new hour.")

            db = SessionLocal()
            setting = db.query(SystemSetting).filter(SystemSetting.key == "global_sensors").first()
            db.close()

            # Ensure we have currency if it's missing (e.g. failed at startup)
            if not current_sensors.get("currency") or current_sensors["currency"] == "EUR":
                await update_ha_config()
            
            if setting:
                config = setting.value
                # Update current hour
                import datetime
                current_sensors["current_hour"] = datetime.datetime.now().hour

                # Load strategy limits from DB
                limits_setting = db.query(SystemSetting).filter(SystemSetting.key == "strategy_limits").first()
                if limits_setting:
                    current_sensors.update(limits_setting.value)
                
                # Map keys to sensor names
                mapping = {
                    "soc": "battery_soc",
                    "solar": "solar_power",
                    "buy_price": "buy_price",
                    "sell_price": "sell_price",
                    "house_power": "house_power",
                    "solar_forecast_today": "solar_forecast_today",
                    "solar_forecast_tomorrow": "solar_forecast_tomorrow",
                    "solar_energy": "solar_energy_total",
                    "solar_energy_today": "solar_energy_today",
                    "house_energy_today": "house_energy_today"
                }
                
                for cfg_key, sensor_key in mapping.items():
                    entity_id = config.get(cfg_key)
                    if entity_id:
                        state_obj = await ha_client.get_state(entity_id)
                        attr_name = config.get(f"{cfg_key}_attr")
                        current_sensors[sensor_key] = get_sensor_value(state_obj, attr_name)
                        
                        # Tracking integration
                        if sensor_key == "solar_power":
                            solar_tracking["integration_sum_watts"] += current_sensors[sensor_key]
                            solar_tracking["sample_count"] += 1
                        
                        if sensor_key == "house_power":
                            house_tracking["integration_sum_watts"] += current_sensors[sensor_key]
                            house_tracking["sample_count"] += 1
                        
                        # Set starting energy if not set
                        if sensor_key == "solar_energy_total" and solar_tracking["hour_start_energy"] is None:
                            solar_tracking["hour_start_energy"] = current_sensors[sensor_key]
                        
                        if sensor_key == "house_energy_today" and house_tracking["hour_start_energy"] is None:
                            house_tracking["hour_start_energy"] = current_sensors[sensor_key]
                        
                        # Extract price & solar arrays from attributes
                        if ("price" in cfg_key or "solar_forecast" in cfg_key) and state_obj:
                            attrs = state_obj.get("attributes", {})
                            prefix = "buy" if cfg_key == "buy_price" else ("sell" if cfg_key == "sell_price" else "solar")
                            
                            # Handle Solar Forecast Extraction
                            if prefix == "solar":
                                # Identify if today or tomorrow
                                day_key = "today" if "today" in cfg_key else "tomorrow"
                                # Try common Solcast/Forecast-Solar attribute names
                                for attr_try in ["wh_hours", "hourly", "forecast", "detailed_forecast", "wh_period_forecast"]:
                                    raw_data = attrs.get(attr_try)
                                    if raw_data:
                                        price_arrays[f"solar_forecast_{day_key}"], _ = extract_price_array(raw_data)
                                        break
                            else:
                                # Handle Price Extraction
                                for attr_try in ["price_today", "today", "raw_today", "prices_today"]:
                                    today_raw = attrs.get(attr_try)
                                    if today_raw:
                                        price_arrays[f"{prefix}_prices_today"], _ = extract_price_array(today_raw)
                                        break
                                for attr_try in ["price_tomorrow", "tomorrow", "raw_tomorrow", "prices_tomorrow"]:
                                    tomorrow_raw = attrs.get(attr_try)
                                    if tomorrow_raw:
                                        price_arrays[f"{prefix}_prices_tomorrow"], _ = extract_price_array(tomorrow_raw)
                                        break

            # 1. Update Survival SOC
            target_soc = occupancy.calculate_target_soc(current_sensors, 10.0) # Assume 10kWh if not set
            current_sensors["survival_soc"] = target_soc
            
            # 2. Decide Inverter State
            state = inverter.update_state(current_sensors)
            
            # 3. Energy Comfort availability
            # Logic: Can use energy if selling or if buy price is zero/negative
            can_use_energy = state.value in [5, 6, 7] or (state.value == 1 and current_sensors["buy_price"] <= 0)
            
            # 4. Coordinate Loads via Guardian
            guardian.coordinate(handlers, current_sensors, can_use_energy)

            # 6. Frequent tracking state save (every poll, throttled to 5min)
            save_tracking_states(force=False)
            # 5. Calculate Daily Solar Yield (Skip if dedicated sensor is mapped)
            mapped_today_sensor = config.get("solar_energy_today")
            if mapped_today_sensor and current_sensors.get("solar_energy_today") is not None:
                # Value is already set from HA in the mapping loop
                if now.minute % 5 == 0 and now.second < 10:
                    logger.info(f"Using MAPPED sensor for yield: {mapped_today_sensor} = {current_sensors['solar_energy_today']} kWh")
            else:
                if now.hour == 0 and now.minute == 0:
                    solar_tracking["day_start_energy"] = current_sensors.get("solar_energy_total")

                today_sum = 0
                if current_sensors.get("solar_energy_total") and solar_tracking.get("day_start_energy") is not None:
                    today_sum = max(0, current_sensors["solar_energy_total"] - solar_tracking["day_start_energy"])
                else:
                    try:
                        from app.models.database import SolarHourlyStat
                        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                        history = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp >= today_start).all()
                        today_sum = sum(h.actual_kwh for h in history)
                        if solar_tracking["sample_count"] > 0:
                            current_wh = (solar_tracking["integration_sum_watts"] / solar_tracking["sample_count"]) / 1000.0
                            today_sum += current_wh
                    except: pass
                
                if now.minute % 5 == 0 and now.second < 10:
                    logger.info(f"CALCULATING yield from history/total: {today_sum} kWh")
                current_sensors["solar_energy_today"] = round(today_sum, 2)
            
        except Exception as e:
            logger.error(f"Error in sensor poller: {e}")
            
        await asyncio.sleep(10) # Poll every 10 seconds

import asyncio
@app.on_event("startup")
async def startup_event():
    # Probe for working HA connection style
    await ha_client.test_connection()
    await update_ha_config()
    
    load_handlers()
    asyncio.create_task(sensor_poller())

@app.on_event("shutdown")
async def shutdown_event():
    await ha_client.close()

@app.get("/api/ha/entities")
async def get_ha_entities():
    """Proxy to HA to get all entities for the UI dropdowns."""
    logger.debug("/api/ha/entities was HIT by the browser!")
    states = await ha_client.get_all_states()
    return [{"id": s["entity_id"], "name": s.get("attributes", {}).get("friendly_name", s["entity_id"])} for s in states]

@app.get("/api/settings")
async def get_settings():
    db = SessionLocal()
    settings = db.query(SystemSetting).all()
    db.close()
    return {s.key: s.value for s in settings}

@app.post("/api/settings")
async def save_settings(data: dict):
    db = SessionLocal()
    for key, value in data.items():
        setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if not setting:
            setting = SystemSetting(key=key, value=value)
            db.add(setting)
        else:
            setting.value = value
    db.commit()
    db.close()
    
    # Refresh handlers in memory
    load_handlers()
    return {"status": "ok"}

@app.get("/api/settings/export")
async def export_settings():
    db = SessionLocal()
    settings = db.query(SystemSetting).all()
    # Mask ha_token for security? No, the user wants to export it for import/backup.
    # Note: we provide it as is.
    res = {s.key: s.value for s in settings}
    db.close()
    return res

@app.post("/api/settings/import")
async def import_settings(data: dict):
    db = SessionLocal()
    try:
        # Clear existing or just update
        for key, value in data.items():
            setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
            if not setting:
                setting = SystemSetting(key=key, value=value)
                db.add(setting)
            else:
                setting.value = value
        db.commit()
        
        # If ha_token was imported, update the client
        if "ha_token" in data:
            ha_client.token = data["ha_token"]
            ha_client.headers["Authorization"] = f"Bearer {data['ha_token']}"
            await ha_client.test_connection()
            await update_ha_config()
            
        load_handlers()
        return {"status": "ok"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/solar_detailed")
async def get_solar_detailed():
    """Returns historical generation vs live forecast array for a full 24h dashboard view."""
    logger.info(">>> SOLAR_DETAILED_API: Request Start")
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        # 1. Get History for today
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        history_entries = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp >= today_start).all()
        history_map = {h.hour: h for h in history_entries}
        logger.info(f">>> SOLAR_DETAILED_API: History Found: {len(history_entries)} items")
        
        # 2. Try to get Live Forecast array from HA for the future
        forecast_array = [0] * 24
        settings = await get_settings()
        
        # FIX: The actual mapping is stored INSIDE the 'global_sensors' JSON setting
        global_sensors = settings.get("global_sensors", {})
        forecast_entity = global_sensors.get("solar_forecast_today")
        
        logger.info(f">>> SOLAR_DETAILED_API: Using forecast entity: '{forecast_entity}' (extracted from global_sensors)")
        
        if forecast_entity:
            state_obj = await ha_client.get_state(forecast_entity)
            if not state_obj:
                logger.warning(f">>> SOLAR_DETAILED_API: Entity '{forecast_entity}' not found in HA")
            else:
                attrs = state_obj.get("attributes", {})
                logger.info(f">>> SOLAR_DETAILED_API: Sensor Attributes Keys: {list(attrs.keys())}")
                # CASE-INSENSITIVE search for forecast attributes
                search_keys = ["detailedforecast", "detailedhourly", "detailed_forecast", "wh_hours", "wh_period_forecast", "forecast", "forecast_today"]
                raw = None
                for k, v in attrs.items():
                    if k.lower() in search_keys:
                        logger.info(f">>> SOLAR_DETAILED_API: Matches found for '{k}'")
                        raw = v
                        if isinstance(raw, list) and len(raw) > 0:
                            logger.info(f">>> SOLAR_DETAILED_API: Found {len(raw)} items. Keys of first item: {list(raw[0].keys()) if isinstance(raw[0], dict) else 'non-dict'}")
                        forecast_array, success = extract_price_array(raw, target_date=now.date())
                        if success: 
                            logger.info(f">>> SOLAR_DETAILED_API: Successfully parsed forecast from '{k}'")
                            break
        else:
            logger.warning("Detailed Solar API: No 'solar_forecast_today' entity mapped in settings.")
        
        # 3. Build a full 24-hour dataset
        factors = get_solar_correction_factors()
        combined = []
        for h in range(24):
            hist = history_map.get(h)
            actual_val = hist.actual_kwh if hist else 0
            
            # Use history forecast if we have it (snapshot at hour start), else live forecast_array
            base_forecast = hist.forecast_kwh if hist else (forecast_array[h] if h < len(forecast_array) else 0)
            
            # Unit Normalization: If value is suspiciously high (>100 kWh per hour), assume it's Wh and divide by 1000
            if base_forecast > 100:
                base_forecast = base_forecast / 1000.0
            
            combined.append({
                "hour": h,
                "actual": actual_val,
                "forecast": base_forecast,
                "corrected": base_forecast * factors.get(h, 1.0)
            })
            
        return {"history": combined, "factors": factors}
    finally:
        db.close()

@app.get("/api/house_detailed")
async def get_house_detailed():
    """Returns historical consumption vs archival average for the same weekday."""
    logger.info(">>> HOUSE_DETAILED_API: Request Start")
    from app.models.database import HouseHourlyStat
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        # 1. Get History for today
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        history_entries = db.query(HouseHourlyStat).filter(HouseHourlyStat.timestamp >= today_start).all()
        history_map = {h.hour: h for h in history_entries}
        
        # 2. Calculate archival average for this day of the week (e.g. 0=Sunday)
        weekday_str = now.strftime('%w')
        
        settings = await get_settings()
        history_weeks = settings.get("strategy_limits", {}).get("history_weeks", 4)
        history_cutoff = now - datetime.timedelta(days=int(history_weeks) * 7)
        
        from sqlalchemy import text
        # Using raw SQL for efficient SQLite weekday filtering
        archive_stats = db.query(HouseHourlyStat).filter(
            HouseHourlyStat.timestamp >= history_cutoff,
            HouseHourlyStat.timestamp < today_start,
            text(f"strftime('%w', timestamp) = '{weekday_str}'")
        ).all()
        
        # Group by hour and average
        archival_map = {}
        hour_counts = {}
        for s in archive_stats:
            archival_map[s.hour] = archival_map.get(s.hour, 0) + s.actual_kwh
            hour_counts[s.hour] = hour_counts.get(s.hour, 0) + 1
            
        for h in archival_map:
            archival_map[h] = archival_map[h] / hour_counts[h]
            
        # 3. Build a full 24-hour dataset
        combined = []
        for h in range(24):
            hist = history_map.get(h)
            actual_val = hist.actual_kwh if hist else 0
            avg_val = archival_map.get(h, 0)
            
            combined.append({
                "hour": h,
                "actual": actual_val,
                "average": avg_val
            })
            
        return {"history": combined}
    finally:
        db.close()

@app.get("/api/dashboard")
async def get_dashboard():
    return {
        "sensors": current_sensors,
        "inverter_state": inverter.current_state.name,
        "prices": price_arrays,
        "loads": [
            {
                "name": h.name,
                "id": h.entity_id,
                "state": h.current_state,
                "priority": h.priority,
                "schema": h.get_config_schema()
            } for h in handlers
        ]
    }


# Anti-cache middleware — must be before static mount
@app.middleware("http")
async def add_headers(request: Request, call_next):
    root_path = request.headers.get("X-Ingress-Path", "")
    if root_path:
        request.scope["root_path"] = root_path
    response = await call_next(request)
    # Prevent browser/Ingress from caching HTML and API responses
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Version"] = "1.3.37"
    return response

# UI Mounting
if os.path.exists("app/static"):
    app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
