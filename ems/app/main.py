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
    "solar_energy_total": 0
}

# Solar tracking state
solar_tracking = {
    "hour_start_ts": None,
    "integration_sum_watts": 0,
    "sample_count": 0,
    "hour_start_energy": None,
    "last_hourly_stats": [] # 24h history for chart
}

# Price arrays for the chart
price_arrays = {
    "buy_prices_today": [],
    "sell_prices_today": [],
    "buy_prices_tomorrow": [],
    "sell_prices_tomorrow": []
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

def extract_price_array(raw):
    """Extract hourly price array from various HA sensor attribute formats."""
    if not raw:
        return []
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, (int, float)):
                result.append(float(item))
            elif isinstance(item, dict):
                # Format: {start, end, value} or {hour, price}
                val = item.get("value") or item.get("price") or item.get("total") or 0
                try:
                    result.append(float(val))
                except (ValueError, TypeError):
                    result.append(0)
        return result
    return []

async def save_hourly_solar_stats(prev_hour_ts):
    """Calculates and saves hourly solar metrics to the database."""
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        # 1. Calculate Actual Energy (kWh)
        actual_kwh = 0
        current_energy = current_sensors.get("solar_energy_total")
        
        if current_energy and solar_tracking["hour_start_energy"]:
            # Use delta from energy sensor (Best accuracy)
            actual_kwh = max(0, current_energy - solar_tracking["hour_start_energy"])
        elif solar_tracking["sample_count"] > 0:
            # Fallback: Integrate Watts
            avg_watts = solar_tracking["integration_sum_watts"] / solar_tracking["sample_count"]
            actual_kwh = avg_watts / 1000.0 # Wh -> kWh
        
        # 2. Get Forecast in effect at START of the hour
        # We'll just use today's attribute if available or the current sensor value
        forecast_kwh = current_sensors.get("solar_forecast_today", 0)
        
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
    """Background task to fetch sensors from HA."""
    import datetime
    
    # Initialize tracking timestamp
    if solar_tracking["hour_start_ts"] is None:
        solar_tracking["hour_start_ts"] = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)

    while True:
        try:
            now = datetime.datetime.now()
            
            # Hour transition check
            if now.hour != solar_tracking["hour_start_ts"].hour:
                await save_hourly_solar_stats(solar_tracking["hour_start_ts"])
                solar_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                solar_tracking["integration_sum_watts"] = 0
                solar_tracking["sample_count"] = 0
                solar_tracking["hour_start_energy"] = current_sensors.get("solar_energy_total")

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
                    "solar_energy": "solar_energy_total"
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
                        
                        # Set starting energy if not set
                        if sensor_key == "solar_energy_total" and solar_tracking["hour_start_energy"] is None:
                            solar_tracking["hour_start_energy"] = current_sensors[sensor_key]
                        
                        # Extract price arrays from attributes
                        if "price" in cfg_key and state_obj:
                            attrs = state_obj.get("attributes", {})
                            prefix = "buy" if cfg_key == "buy_price" else "sell"
                            # Try common attribute names for hourly prices
                            for attr_try in ["price_today", "today", "raw_today", "prices_today"]:
                                today_raw = attrs.get(attr_try)
                                if today_raw:
                                    price_arrays[f"{prefix}_prices_today"] = extract_price_array(today_raw)
                                    break
                            for attr_try in ["price_tomorrow", "tomorrow", "raw_tomorrow", "prices_tomorrow"]:
                                tomorrow_raw = attrs.get(attr_try)
                                if tomorrow_raw:
                                    price_arrays[f"{prefix}_prices_tomorrow"] = extract_price_array(tomorrow_raw)
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
    """Returns historical generation vs forecast vs corrected forecast for the UI."""
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        # 1. Get History (Last 24h)
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
        history_entries = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp > cutoff).order_by(SolarHourlyStat.timestamp).all()
        
        # 2. Get Correction Factors
        factors = get_solar_correction_factors()
        
        # 3. Pull Current Forecasts (Raw)
        # We'll need a better way to get hourly forecasts if available, but for now 
        # we'll use the price_arrays logic or similar if Solcast attributes exist.
        # For simplicity in this iteration, we return what we have in current_sensors and history.
        
        result = {
            "history": [
                {
                    "hour": h.hour,
                    "actual": h.actual_kwh,
                    "forecast": h.forecast_kwh,
                    "corrected": h.forecast_kwh * factors.get(h.hour, 1.0)
                } for h in history_entries
            ],
            "factors": factors
        }
        return result
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
    return response

# UI Mounting
if os.path.exists("app/static"):
    app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
