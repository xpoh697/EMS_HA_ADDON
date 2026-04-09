from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from app.services.inverter import InverterController
from app.services.boiler import BoilerManager
from app.services.loads import LoadScheduler
from app.services.occupancy import OccupancyEngine
from app.providers.hass import HomeAssistantClient
from app.models.database import init_db, SessionLocal
import logging
import os
import asyncio

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EMS Control API")
init_db()

# Components
inverter = InverterController(dry_run=True)
boiler = BoilerManager()
loads = LoadScheduler()
occupancy = OccupancyEngine()

# State
current_sensors = {}

@app.on_event("startup")
async def startup_event():
    logger.info("EMS Engine initialized.")

@app.post("/api/update")
async def update_sensors(sensors: dict):
    """
    Periodic update from HA.
    """
    global current_sensors
    current_sensors.update(sensors)
    
    # 1. Update Survival SOC based on occupancy
    target_survival_soc = occupancy.calculate_target_soc(current_sensors, sensors.get("battery_capacity", 10.0))
    current_sensors["survival_soc"] = target_survival_soc
    
    # 2. Decide Inverter State
    state = inverter.update_state(current_sensors)
    
    # 3. Can we use energy for comfort?
    # Comfort is allowed in states where we have excess: sale_pv, sale_pv_no_bat, or buy (if price is negative)
    can_use_energy = state in [5, 6] or (state == 1 and current_sensors.get("grid_price", 0) <= 0)
    
    # 4. Manage Boiler
    boiler_on = boiler.decide(current_sensors, can_use_energy)
    
    # 5. Manage Loads
    load_commands = loads.manage(current_sensors, can_use_energy)
    
    return {
        "inverter_state": state.name,
        "survival_soc": target_survival_soc,
        "boiler_on": boiler_on,
        "load_commands": load_commands
    }

@app.get("/api/dashboard")
async def get_dashboard():
    return {
        "sensors": current_sensors,
        "inverter_state": inverter.current_state.name,
        "loads": {l.entity_id: l.state for l in loads.loads}
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
