from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import logging
import os
import datetime
import asyncio
from sqlalchemy import text

# Internal Imports
from app.models.database import init_db, SessionLocal, SystemSetting, SolarHourlyStat, HouseHourlyStat
from app.core.state import state_manager
from app.utils.sensor_utils import get_sensor_value
from app.services.price_service import extract_price_array
from app.services.solar_service import save_hourly_solar_stats, get_solar_correction_factors, repopulate_history_from_ha
from app.services.house_service import save_hourly_house_stats

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EMS Control API")
init_db()

def run_once_v1348_reset():
    """Wipes contaminated solar history to reset correction factors (Run once for v1.3.48 cleanup)."""
    db = SessionLocal()
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "v1348_reset_done").first()
        if not setting:
            logger.info(">>> v1.3.48 STARTUP: Performing ARCHITECTURAL CLEANUP. Resetting history for clean state...")
            db.query(SolarHourlyStat).delete()
            
            reset_flag = SystemSetting(key="v1348_reset_done", value={"done": True})
            db.add(reset_flag)
            db.commit()
            logger.info(">>> v1.3.48 STARTUP: Cleanup and reset successful!")
        else:
            logger.info(">>> v1.3.48 STARTUP: Cleanup already performed.")
    except Exception as e:
        logger.error(f"Failed to perform v1.3.48 reset: {e}")
    finally:
        db.close()

run_once_v1348_reset()

async def update_ha_config():
    config = await state_manager.ha_client.get_config()
    if config:
        state_manager.current_sensors["currency"] = config.get("currency", "EUR")

def load_handlers():
    from app.services.boiler import BoilerManager
    from app.services.loads import CyclicLoadHandler
    db = SessionLocal()
    setting = db.query(SystemSetting).filter(SystemSetting.key == "loads").first()
    db.close()
    
    new_handlers = []
    if setting:
        for cfg in setting.value:
            if cfg["type"] == "boiler":
                h = BoilerManager(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"])
                h.target_temp = cfg.get("target_temp", 60)
                new_handlers.append(h)
            elif cfg["type"] == "cyclic":
                new_handlers.append(CyclicLoadHandler(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"]))
    state_manager.handlers = new_handlers

# Background Task
async def sensor_poller():
    """The central orchestrator of the EMS system."""
    while True:
        try:
            now = datetime.datetime.now()
            
            # 1. Hour Transition Check
            if state_manager.solar_tracking["hour_start_ts"] is None:
                state_manager.solar_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                state_manager.house_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
            
            if now.hour != state_manager.solar_tracking["hour_start_ts"].hour:
                prev_hour = state_manager.solar_tracking["hour_start_ts"]
                # Save historical metrics via domain services
                save_hourly_solar_stats(prev_hour, state_manager.solar_tracking, state_manager.current_sensors, state_manager.price_arrays)
                save_hourly_house_stats(prev_hour, state_manager.house_tracking, state_manager.current_sensors)
                
                # Reset tracking for new hour
                state_manager.solar_tracking.update({
                    "hour_start_ts": now.replace(minute=0, second=0, microsecond=0),
                    "integration_sum_watts": 0,
                    "sample_count": 0,
                    "hour_start_energy": state_manager.current_sensors.get("solar_energy_total")
                })
                state_manager.house_tracking.update({
                    "hour_start_ts": now.replace(minute=0, second=0, microsecond=0),
                    "integration_sum_watts": 0,
                    "sample_count": 0,
                    "hour_start_energy": state_manager.current_sensors.get("house_energy_today")
                })
                state_manager.save_tracking_states(force=True)

            # 2. Fetch Configuration
            db = SessionLocal()
            glob_sensors = db.query(SystemSetting).filter(SystemSetting.key == 'global_sensors').first()
            strategy_lims = db.query(SystemSetting).filter(SystemSetting.key == "strategy_limits").first()
            db.close()
            
            config = glob_sensors.value if glob_sensors else {}
            if strategy_lims:
                state_manager.current_sensors.update(strategy_lims.value)
            state_manager.current_sensors["current_hour"] = now.hour

            # 3. Poll Sensors from Home Assistant
            mapping = {
                "soc": "battery_soc", "solar": "solar_power", 
                "buy_price": "buy_price", "sell_price": "sell_price", 
                "buy_price_tomorrow": "buy_price_tomorrow", "sell_price_tomorrow": "sell_price_tomorrow",
                "house_power": "house_power", "house_energy_total": "house_energy_today",
                "solar_energy_total": "solar_energy_today",
                "solar_forecast_today": "solar_forecast_today",
                "solar_forecast_tomorrow": "solar_forecast_tomorrow"
            }
            
            for config_key, state_key in mapping.items():
                entity_id = config.get(config_key)
                if not entity_id:
                    # v1.3.67: Flexible mapping fallback for older config keys
                    if config_key == "solar_energy_total": entity_id = config.get("solar_energy")
                    elif config_key == "house_energy_total": entity_id = config.get("house_energy")
                
                if not entity_id: continue
                
                state_obj = await state_manager.ha_client.get_state(entity_id)
                if not state_obj: continue
                
                val = get_sensor_value(state_obj, config.get(f"{config_key}_attr"))
                state_manager.current_sensors[state_key] = val
                
                # Accumulate for power integration fallback
                if state_key == "solar_power":
                    state_manager.solar_tracking["integration_sum_watts"] += val
                    state_manager.solar_tracking["sample_count"] += 1
                elif state_key == "house_power":
                    state_manager.house_tracking["integration_sum_watts"] += val
                    state_manager.house_tracking["sample_count"] += 1
                elif state_key == "solar_energy_today":
                    if state_manager.solar_tracking["hour_start_energy"] is None:
                        state_manager.solar_tracking["hour_start_energy"] = val
                    
                    if not state_manager._solar_history_recovered:
                        state_manager._solar_history_recovered = True
                        logger.info(f">>> POLLER: Triggering solar history recovery for {entity_id}")
                        db_hist = SessionLocal()
                        asyncio.create_task(repopulate_history_from_ha(db_hist, state_manager.ha_client, entity_id, state_manager.price_arrays))
                elif state_key == "house_energy_today":
                    if state_manager.house_tracking["hour_start_energy"] is None:
                        state_manager.house_tracking["hour_start_energy"] = val
                    
                    if not state_manager._house_history_recovered:
                        state_manager._house_history_recovered = True
                        logger.info(f">>> POLLER: Triggering house history recovery for {entity_id}")
                        db_hist = SessionLocal()
                        from app.services.house_service import repopulate_history_from_ha as house_repopulate
                        asyncio.create_task(house_repopulate(db_hist, state_manager.ha_client, entity_id))
                
                # Extract complex attributes (Prices, Forecasts)
                if ("price" in config_key or "solar_forecast" in config_key):
                    prefix = "buy" if config_key == "buy_price" else ("sell" if config_key == "sell_price" else "solar")
                    day = "tomorrow" if "tomorrow" in config_key else "today"
                    target_dt = now.date() if day == "today" else (now + datetime.timedelta(days=1)).date()
                    attrs = state_obj.get("attributes", {})
                    
                    if prefix == "solar":
                        # v1.3.55: Fuzzy match for Solcast / Forecast-Solar (supports detailedForecast CamelCase)
                        search_keys = ["detailedForecast", "detailed_forecast", "wh_hours", "wh_period_forecast", "detailedhourly", "forecast", "forecasts"]
                        for attr in search_keys:
                            val = attrs.get(attr)
                            if val is None:
                                from app.utils.sensor_utils import fuzzy_get
                                val = fuzzy_get(attrs, [attr])
                                
                            if val:
                                array, matched_count = extract_price_array(val, target_date=target_dt, is_solar=True, attr_name=attr, day_hint=day)
                                if matched_count > 0:
                                    state_manager.price_arrays[f"solar_forecast_{day}"] = array
                                    if f"solar_{day}" not in state_manager.current_sensors.get("_picked_attrs", []):
                                        logger.info(f">>> POLLER: Identified Solar {day} attribute: {attr}")
                                        state_manager.current_sensors.setdefault("_picked_attrs", []).append(f"solar_{day}")
                                    break
                    else:
                        # v1.3.55: Fuzzy match for prices (supports "Price today" spaced names)
                        search_keys = [f"price_{day}", f"prices_{day}", f"raw_{day}", f"{day}_prices", day, f"price {day}"]
                        for attr in search_keys:
                            val = attrs.get(attr)
                            if val is None:
                                from app.utils.sensor_utils import fuzzy_get
                                val = fuzzy_get(attrs, [attr])
                                
                            if val:
                                array, matched_count = extract_price_array(val, target_date=target_dt, attr_name=attr, day_hint=day)
                                if matched_count > 0:
                                    state_manager.price_arrays[f"{prefix}_prices_{day}"] = array
                                    if f"{prefix}_{day}" not in state_manager.current_sensors.get("_picked_attrs", []):
                                        logger.info(f">>> POLLER: Identified {prefix} {day} attribute: {attr}")
                                        state_manager.current_sensors.setdefault("_picked_attrs", []).append(f"{prefix}_{day}")
                                    break

            # 4. Run Strategy & Coordination
            state_manager.current_sensors["survival_soc"] = state_manager.occupancy.calculate_target_soc(state_manager.current_sensors, 10.0)
            inv_state = state_manager.inverter.update_state(state_manager.current_sensors)
            
            # Coordinating Loads
            grid_friendly = inv_state.value in [5,6,7] or (inv_state.value == 1 and state_manager.current_sensors["buy_price"] <= 0)
            state_manager.guardian.coordinate(state_manager.handlers, state_manager.current_sensors, grid_friendly)
            
            # Periodic Persistence
            state_manager.save_tracking_states()

            # Fallback for Daily Yield if sensor missing
            if not config.get("solar_energy_today"):
                db = SessionLocal()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                hist = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp >= today_start).all()
                db.close()
                subtotal = sum(h.actual_kwh for h in hist)
                current_w = (state_manager.solar_tracking["integration_sum_watts"] / state_manager.solar_tracking["sample_count"]) if state_manager.solar_tracking["sample_count"] > 0 else 0
                state_manager.current_sensors["solar_energy_today"] = round(subtotal + (current_w / 1000.0), 2)

        except Exception as e:
            logger.error(f">>> POLLER: Global loop error: {e}")
            
        await asyncio.sleep(10)

# Lifecycle
@app.on_event("startup")
async def startup_event():
    await state_manager.ha_client.test_connection()
    await update_ha_config()
    state_manager.load_tracking_states()
    load_handlers()
    asyncio.create_task(sensor_poller())

@app.on_event("shutdown")
async def shutdown_event():
    await state_manager.ha_client.close()

# API Endpoints
@app.get("/api/ha/entities")
async def get_ha_entities():
    states = await state_manager.ha_client.get_all_states()
    return [{"id": s["entity_id"], "name": s.get("attributes", {}).get("friendly_name", s["entity_id"])} for s in states]

@app.get("/api/settings")
async def get_settings():
    db = SessionLocal()
    rows = {s.key: s.value for s in db.query(SystemSetting).all()}
    db.close()
    
    # 1.3.70: Flatten mapping object for the frontend
    mapping = rows.get("mapping", {})
    if isinstance(mapping, str):
        import json
        try: mapping = json.loads(mapping)
        except: mapping = {}
    
    # Merge mapping into top-level settings
    if isinstance(mapping, dict):
        for k, v in mapping.items():
            if k not in rows:
                rows[k] = v
    return rows

@app.post("/api/settings")
async def save_settings(data: dict):
    db = SessionLocal()
    try:
        # 1.3.70: Identify sensor mapping keys
        sensor_keys = [
            "buy_price", "sell_price", "solar_power", "house_power", 
            "solar_energy_total", "house_energy_total", "solar_energy_today", "house_energy_today",
            "battery_soc", "battery_power", "inverter_status"
        ]
        
        # Get existing mapping or create new
        mapping_row = db.query(SystemSetting).filter(SystemSetting.key == "mapping").first()
        import json
        if mapping_row:
            try: mapping = mapping_row.value if isinstance(mapping_row.value, dict) else json.loads(mapping_row.value)
            except: mapping = {}
        else:
            mapping = {}
            
        remaining_data = {}
        for k, v in data.items():
            if k == "mapping":
                # v1.3.71: Merge UI-provided mapping object
                if isinstance(v, dict): mapping.update(v)
            elif k in sensor_keys or "_entity" in k or k.endswith("_sensor"):
                mapping[k] = v
            else:
                remaining_data[k] = v
        
        # Save unified mapping
        if not mapping_row:
            db.add(SystemSetting(key="mapping", value=mapping))
        else:
            mapping_row.value = mapping
            
        # Save other settings individually
        for k, v in remaining_data.items():
            setting = db.query(SystemSetting).filter(SystemSetting.key == k).first()
            if not setting: db.add(SystemSetting(key=k, value=v))
            else: setting.value = v
            
        db.commit()
        load_handlers()
        return {"status": "ok"}
    finally:
        db.close()

@app.get("/api/solar_detailed")
async def get_solar_detailed(day: str = "today"):
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        target_date = now.date() if day == "today" else (now + datetime.timedelta(days=1)).date()
        target_start = datetime.datetime.combine(target_date, datetime.time.min)
        target_end = datetime.datetime.combine(target_date, datetime.time.max)
        
        history = {h.hour: h for h in db.query(SolarHourlyStat).filter(
            SolarHourlyStat.timestamp >= target_start,
            SolarHourlyStat.timestamp <= target_end
        ).all()}
        
        factors = get_solar_correction_factors()
        combined = []
        forecast_array = state_manager.price_arrays.get(f"solar_forecast_{day}", [0]*24)
        
        for h in range(24):
            hist = history.get(h)
            actual = hist.actual_kwh if hist else 0
            # Snapshot forecast if it was saved, else use live forecast array
            f = hist.forecast_kwh if hist else (forecast_array[h] if h < len(forecast_array) else 0)
            if f > 100: f /= 1000.0 # Wh to kWh normalization
            combined.append({
                "hour": h, "actual": actual, "forecast": f, 
                "corrected": round(f * factors.get(h, 1.0), 3)
            })
        return {"history": combined, "factors": factors}
    finally:
        db.close()

@app.get("/api/house_detailed")
async def get_house_detailed(day: str = "today"):
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        target_date = now.date() if day == "today" else (now + datetime.timedelta(days=1)).date()
        target_start = datetime.datetime.combine(target_date, datetime.time.min)
        target_end = datetime.datetime.combine(target_date, datetime.time.max)
        
        history = {h.hour: h for h in db.query(HouseHourlyStat).filter(
            HouseHourlyStat.timestamp >= target_start,
            HouseHourlyStat.timestamp <= target_end
        ).all()}
        
        # Archival Average for the same weekday
        history_cutoff = now - datetime.timedelta(days=28)
        archive_stats = db.query(HouseHourlyStat).filter(
            HouseHourlyStat.timestamp >= history_cutoff,
            # We use current weekday for comparison
            text(f"strftime('%w', timestamp) = '{target_date.strftime('%w')}'")
        ).all()
        
        arch_map, counts = {}, {}
        for s in archive_stats:
            arch_map[s.hour] = arch_map.get(s.hour, 0) + s.actual_kwh
            counts[s.hour] = counts.get(s.hour, 0) + 1
            
        combined = []
        for h in range(24):
            combined.append({
                "hour": h,
                "actual": history.get(h).actual_kwh if history.get(h) else 0,
                "average": round(arch_map.get(h, 0) / counts[h], 3) if counts.get(h) else 0
            })
        return {"history": combined}
    finally:
        db.close()

@app.get("/api/db/export")
async def export_db():
    db_path = "/data/ems_data.db" if os.path.exists("/data") else "ems_data.db"
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Database file not found")
    return FileResponse(db_path, filename="ems_data.db", media_type="application/x-sqlite3")

@app.post("/api/db/import")
async def import_db(file: UploadFile = File(...)):
    db_path = "/data/ems_data.db" if os.path.exists("/data") else "ems_data.db"
    try:
        content = await file.read()
        with open(db_path, "wb") as f:
            f.write(content)
        return {"status": "success", "message": "Database imported successfully. Restart the addon to apply all changes."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard")
async def get_dashboard():
    return {
        "sensors": state_manager.current_sensors,
        "inverter_state": state_manager.inverter.current_state.name,
        "prices": state_manager.price_arrays,
        "loads": [
            {
                "name": h.name, "id": h.entity_id, "state": h.current_state,
                "priority": h.priority, "schema": h.get_config_schema()
            } for h in state_manager.handlers
        ]
    }

@app.middleware("http")
async def add_headers(request: Request, call_next):
    root = request.headers.get("X-Ingress-Path", "")
    if root: request.scope["root_path"] = root
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0", "X-Version": "1.3.71"
    })
    return response

if os.path.exists("app/static"):
    app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
