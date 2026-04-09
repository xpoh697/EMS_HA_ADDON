from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from app.services.inverter import InverterController
from app.services.boiler import BoilerManager
from app.services.loads import CyclicLoadHandler
from app.services.occupancy import OccupancyEngine
from app.services.guardian import PowerGuardian
from app.models.database import init_db
import logging
import os

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EMS Control API")
init_db()

# Core Components
inverter = InverterController(dry_run=True)
occupancy = OccupancyEngine()
guardian = PowerGuardian(max_grid_power_w=11000.0)

# Modular Load Handlers
handlers = [
    BoilerManager(name="Boiler", entity_id="switch.boiler", priority=5),
    CyclicLoadHandler(name="Washing Machine", entity_id="switch.washer", priority=10),
    CyclicLoadHandler(name="Dishwasher", entity_id="switch.dishwasher", priority=15)
]

# State
current_sensors = {}

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
