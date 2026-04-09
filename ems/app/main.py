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

# HA Client using Supervisor Token
ha_token = os.environ.get("SUPERVISOR_TOKEN", "REPLACE_ME")
ha_client = HomeAssistantClient(base_url="http://supervisor/core", token=ha_token)

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

# Load on startup
load_handlers()

@app.get("/api/ha/entities")
async def get_ha_entities():
    """Proxy to HA to get all entities for the UI dropdowns."""
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

@app.post("/api/update")
async def update_sensors(sensors: dict):
    global current_sensors
    current_sensors.update(sensors)
    
    # 1. Update Survival SOC
    target_soc = occupancy.calculate_target_soc(current_sensors, sensors.get("battery_capacity", 10.0))
    current_sensors["survival_soc"] = target_soc
    
    # 2. Decide Inverter State
    state = inverter.update_state(current_sensors)
    
    # 3. Energy Comfort availability
    can_use_energy = state in [5, 6] or (state == 1 and current_sensors.get("grid_price", 0) <= 0)
    
    # 4. Coordinate Loads via Guardian
    load_commands = guardian.coordinate(handlers, current_sensors, can_use_energy)
    
    return {
        "inverter_state": state.name,
        "survival_soc": target_soc,
        "load_commands": load_commands
    }

@app.get("/api/dashboard")
async def get_dashboard():
    return {
        "sensors": current_sensors,
        "inverter_state": inverter.current_state.name,
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


# UI Mounting
if os.path.exists("app/static"):
    app.mount("/", StaticFiles(directory="app/static", html=True), name="static")

@app.middleware("http")
async def add_ingress_path(request: Request, call_next):
    root_path = request.headers.get("X-Ingress-Path", "")
    if root_path:
        request.scope["root_path"] = root_path
    return await call_next(request)
