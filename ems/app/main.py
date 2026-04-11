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

@app.on_event("startup")
async def update_ha_config():
    """Fetch system config from HA like currency."""
    config = await ha_client.get_config()
    if config:
        current_sensors["currency"] = config.get("currency", "EUR")
        logger.info(f"HA Currency: {current_sensors['currency']}")

# State
current_sensors = {
    "battery_soc": 0, "solar_power": 0, "buy_price": 0, "sell_price": 0, "house_power": 0,
    "survival_soc": 20, "price_tomorrow": 0, "currency": "EUR"
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

async def sensor_poller():
    """Background task to fetch sensors from HA."""
    while True:
        try:
            db = SessionLocal()
            setting = db.query(SystemSetting).filter(SystemSetting.key == "global_sensors").first()
            db.close()

            # Ensure we have currency if it's missing (e.g. failed at startup)
            if not current_sensors.get("currency") or current_sensors["currency"] == "EUR":
                await update_ha_config()
            
            if setting:
                config = setting.value
                # Map keys to sensor names
                mapping = {
                    "soc": "battery_soc",
                    "solar": "solar_power",
                    "buy_price": "buy_price",
                    "sell_price": "sell_price",
                    "house_power": "house_power"
                }
                
                for cfg_key, sensor_key in mapping.items():
                    entity_id = config.get(cfg_key)
                    if entity_id:
                        state_obj = await ha_client.get_state(entity_id)
                        attr_name = config.get(f"{cfg_key}_attr")
                        current_sensors[sensor_key] = get_sensor_value(state_obj, attr_name)
                        
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
