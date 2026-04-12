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
import asyncio

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
            from app.models.database import SolarHourlyStat
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

# Persistent Token from DB
db_token = SessionLocal()
token_setting = db_token.query(SystemSetting).filter(SystemSetting.key == "ha_token").first()
if token_setting and token_setting.value:
    ha_token = token_setting.value
db_token.close()

ha_client = HomeAssistantClient(base_url="http://supervisor/core/api", token=ha_token)

# Globals
handlers = []
current_sensors = {
    "battery_soc": 0, "solar_power": 0, "buy_price": 0, "sell_price": 0, "house_power": 0,
    "survival_soc": 20, "price_tomorrow": 0, "currency": "EUR", "current_hour": 0,
    "solar_forecast_today": 0, "solar_forecast_tomorrow": 0,
    "solar_energy_total": 0, "solar_energy_today": 0, "house_energy_today": 0
}

solar_tracking = {
    "hour_start_ts": None,
    "integration_sum_watts": 0,
    "sample_count": 0,
    "hour_start_energy": None,
    "day_start_energy": None
}

house_tracking = {
    "hour_start_ts": None,
    "integration_sum_watts": 0,
    "sample_count": 0,
    "hour_start_energy": None
}

price_arrays = {
    "buy_prices_today": [], "buy_prices_tomorrow": [],
    "sell_prices_today": [], "sell_prices_tomorrow": [],
    "solar_forecast_today": [], "solar_forecast_tomorrow": []
}

last_state_save_ts = datetime.datetime.min

# Helpers
def get_sensor_value(state_obj: dict, attr_name: str = None):
    if not state_obj: return 0
    try:
        if attr_name and attr_name in state_obj.get("attributes", {}):
            return float(state_obj["attributes"][attr_name])
        return float(state_obj.get("state", 0))
    except (ValueError, TypeError):
        return 0

def extract_price_array(raw, target_date=None, is_solar=False, attr_name=""):
    if not raw: return [0.0]*24, False
    buckets = [[] for _ in range(24)]
    target_str = target_date.strftime("%Y-%m-%d") if target_date else None
    items = []
    
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                dt = datetime.datetime.fromisoformat(k.replace('Z','+00:00').replace(' ','T'))
                items.append((dt, float(v)))
            except: continue
    elif isinstance(raw, list):
        for item in raw:
            try:
                if isinstance(item, dict):
                    ts_str = item.get("period_start") or item.get("start") or item.get("time") or item.get("datetime")
                    if not ts_str: continue
                    dt = datetime.datetime.fromisoformat(ts_str.replace('Z','+00:00').replace(' ','T'))
                    val = 0
                    for key in ["pv_estimate", "estimate", "value", "price", "total", "amount"]:
                        v = item.get(key)
                        if v is not None: val = float(v); break
                    items.append((dt, val))
            except: continue

    found = False
    for dt, val in items:
        if target_str and dt.strftime("%Y-%m-%d") != target_str: continue
        h = dt.hour
        if 0 <= h <= 23:
            # MAGNITUDE FILTER: 20kWh Cap
            if is_solar and 20.0 < val < 1000.0: continue
            buckets[h].append(val)
            found = True

    result = [0.0]*24
    should_sum = is_solar and ("wh_hours" in attr_name.lower() or "energy" in attr_name.lower())
    for h in range(24):
        vals = buckets[h]
        if not vals: continue
        if should_sum:
            s = sum(vals)
            result[h] = round(s / 1000.0 if s > 150.0 else s, 3)
        else:
            result[h] = round(sum(vals) / len(vals), 3)
    return result, found

# Persistence
def save_tracking_states(force=False):
    global last_state_save_ts
    now = datetime.datetime.now()
    if not force and (now - last_state_save_ts).total_seconds() < 300: return
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
        if not setting: db.add(SystemSetting(key="tracking_state", value=state))
        else: setting.value = state
        db.commit()
        last_state_save_ts = now
    except Exception as e: logger.error(f"Save states failed: {e}")
    finally: db.close()

def load_tracking_states():
    db = SessionLocal()
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "tracking_state").first()
        if setting and setting.value:
            s, h = setting.value.get("solar", {}), setting.value.get("house", {})
            if s.get("hour_start_ts"):
                solar_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(s["hour_start_ts"])
                solar_tracking["integration_sum_watts"] = s.get("integration_sum_watts", 0)
                solar_tracking["sample_count"] = s.get("sample_count", 0)
                solar_tracking["hour_start_energy"] = s.get("hour_start_energy")
            if h.get("hour_start_ts"):
                house_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(h["hour_start_ts"])
                house_tracking["integration_sum_watts"] = h.get("integration_sum_watts", 0)
                house_tracking["sample_count"] = h.get("sample_count", 0)
                house_tracking["hour_start_energy"] = h.get("hour_start_energy")
    finally: db.close()

load_tracking_states()

# Database Metrics
async def save_hourly_solar_stats(prev_hour_ts):
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        cur_e = current_sensors.get("solar_energy_total")
        fb_kwh = 0
        if solar_tracking["sample_count"] > 0:
            fb_kwh = max(0, (solar_tracking["integration_sum_watts"] / solar_tracking["sample_count"]) / 1000.0)
        
        actual = 0
        if cur_e and solar_tracking["hour_start_energy"]:
            actual = cur_e - solar_tracking["hour_start_energy"] if cur_e >= solar_tracking["hour_start_energy"] else cur_e
            if actual > 20.0 or actual < 0.01: actual = fb_kwh
        else: actual = fb_kwh

        h_idx = prev_hour_ts.hour
        f_arr = price_arrays.get("solar_forecast_today", [])
        forecast = f_arr[h_idx] if h_idx < len(f_arr) else 0

        db.add(SolarHourlyStat(timestamp=prev_hour_ts, hour=h_idx, actual_kwh=float(actual), forecast_kwh=float(forecast)))
        db.commit()
        logger.info(f"Saved Solar Stats H{h_idx}: {actual:.2f}kWh")
    except Exception as e: logger.error(f"Solar Stat Error: {e}")
    finally: db.close()

async def save_hourly_house_stats(prev_hour_ts):
    from app.models.database import HouseHourlyStat
    db = SessionLocal()
    try:
        cur_e = current_sensors.get("house_energy_today")
        fb_kwh = 0
        if house_tracking["sample_count"] > 0:
            fb_kwh = (house_tracking["integration_sum_watts"] / house_tracking["sample_count"]) / 1000.0
        
        actual = cur_e - house_tracking["hour_start_energy"] if (cur_e and house_tracking["hour_start_energy"] and cur_e >= house_tracking["hour_start_energy"]) else fb_kwh
        db.add(HouseHourlyStat(timestamp=prev_hour_ts, hour=prev_hour_ts.hour, actual_kwh=float(actual)))
        db.commit()
    except Exception as e: logger.error(f"House Stat Error: {e}")
    finally: db.close()

def get_solar_correction_factors():
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=14)
        history = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp > cutoff).all()
        if not history: return {h: 1.0 for h in range(24)}
        stats = {h: {"a": 0, "f": 0} for h in range(24)}
        for e in history:
            stats[e.hour]["a"] += e.actual_kwh
            stats[e.hour]["f"] += e.forecast_kwh
        factors = {}
        for h in range(24):
            f, a = stats[h]["f"], stats[h]["a"]
            factors[h] = min(max(a/f, 0.1), 3.0) if f > 0.05 else 1.0
        return factors
    finally: db.close()

def load_handlers():
    global handlers
    db = SessionLocal()
    setting = db.query(SystemSetting).filter(SystemSetting.key == "loads").first()
    db.close()
    new = []
    if setting:
        for cfg in setting.value:
            if cfg["type"] == "boiler":
                h = BoilerManager(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"])
                h.target_temp = cfg.get("target_temp", 60); new.append(h)
            elif cfg["type"] == "cyclic":
                new.append(CyclicLoadHandler(name=cfg["name"], entity_id=cfg["entity_id"], priority=cfg["priority"]))
    handlers = new

async def update_ha_config():
    config = await ha_client.get_config()
    if config: current_sensors["currency"] = config.get("currency", "EUR")

# Background Task
async def sensor_poller():
    while True:
        try:
            now = datetime.datetime.now()
            if solar_tracking["hour_start_ts"] is None:
                solar_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
                house_tracking["hour_start_ts"] = now.replace(minute=0, second=0, microsecond=0)
            
            if now.hour != solar_tracking["hour_start_ts"].hour:
                prev = solar_tracking["hour_start_ts"]
                await save_hourly_solar_stats(prev)
                await save_hourly_house_stats(prev)
                solar_tracking.update({"hour_start_ts": now.replace(minute=0, second=0, microsecond=0), "integration_sum_watts": 0, "sample_count": 0, "hour_start_energy": current_sensors.get("solar_energy_total")})
                house_tracking.update({"hour_start_ts": now.replace(minute=0, second=0, microsecond=0), "integration_sum_watts": 0, "sample_count": 0, "hour_start_energy": current_sensors.get("house_energy_today")})
                save_tracking_states(force=True)

            db = SessionLocal()
            glob = db.query(SystemSetting).filter(SystemSetting.key == 'global_sensors').first()
            lims = db.query(SystemSetting).filter(SystemSetting.key == "strategy_limits").first()
            db.close()
            
            config = glob.value if glob else {}
            if lims: current_sensors.update(lims.value)
            current_sensors["current_hour"] = now.hour

            mapping = {"soc": "battery_soc", "solar": "solar_power", "buy_price": "buy_price", "sell_price": "sell_price", "house_power": "house_power", "solar_forecast_today": "solar_forecast_today", "solar_forecast_tomorrow": "solar_forecast_tomorrow", "solar_energy": "solar_energy_total", "solar_energy_today": "solar_energy_today", "house_energy_today": "house_energy_today"}
            
            for ck, sk in mapping.items():
                ent = config.get(ck)
                if ent:
                    st = await ha_client.get_state(ent)
                    current_sensors[sk] = get_sensor_value(st, config.get(f"{ck}_attr"))
                    if sk == "solar_power": solar_tracking["integration_sum_watts"] += current_sensors[sk]; solar_tracking["sample_count"] += 1
                    if sk == "house_power": house_tracking["integration_sum_watts"] += current_sensors[sk]; house_tracking["sample_count"] += 1
                    if sk == "solar_energy_total" and solar_tracking["hour_start_energy"] is None: solar_tracking["hour_start_energy"] = current_sensors[sk]
                    if sk == "house_energy_today" and house_tracking["hour_start_energy"] is None: house_tracking["hour_start_energy"] = current_sensors[sk]
                    
                    if ("price" in ck or "solar_forecast" in ck) and st:
                        prefix = "buy" if ck == "buy_price" else ("sell" if ck == "sell_price" else "solar")
                        day = "today" if "today" in ck else "tomorrow"
                        attrs = st.get("attributes", {})
                        if prefix == "solar":
                            # Target Date is critical for Solcast/Forecast-Solar multi-day attributes
                            target_dt = now.date() if day == "today" else (now + datetime.timedelta(days=1)).date()
                            for a in ["wh_hours", "wh_period_forecast", "detailed_forecast", "detailedhourly", "forecast", "forecasts"]:
                                if attrs.get(a): 
                                    price_arrays[f"solar_forecast_{day}"], _ = extract_price_array(attrs[a], target_date=target_dt, is_solar=True, attr_name=a)
                                    break
                        else:
                            # Target Date for Prices
                            target_dt = now.date() if day == "today" else (now + datetime.timedelta(days=1)).date()
                            for a in [f"price_{day}", f"prices_{day}", f"raw_{day}", f"{day}_prices", day]:
                                if attrs.get(a): 
                                    price_arrays[f"{prefix}_prices_{day}"], _ = extract_price_array(attrs[a], target_date=target_dt)
                                    break

            current_sensors["survival_soc"] = occupancy.calculate_target_soc(current_sensors, 10.0)
            inv_state = inverter.update_state(current_sensors)
            guardian.coordinate(handlers, current_sensors, inv_state.value in [5,6,7] or (inv_state.value == 1 and current_sensors["buy_price"] <= 0))
            save_tracking_states()

            # Daily Yield
            if not config.get("solar_energy_today"):
                from app.models.database import SolarHourlyStat
                db = SessionLocal()
                hist = db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp >= now.replace(hour=0,minute=0,second=0,microsecond=0)).all()
                db.close()
                current_sensors["solar_energy_today"] = round(sum(h.actual_kwh for h in hist) + ((solar_tracking["integration_sum_watts"]/solar_tracking["sample_count"])/1000.0 if solar_tracking["sample_count"]>0 else 0), 2)

        except Exception as e: logger.error(f"Poller Error: {e}")
        await asyncio.sleep(10)

# Lifecycle
@app.on_event("startup")
async def startup_event():
    await ha_client.test_connection()
    await update_ha_config()
    load_handlers()
    asyncio.create_task(sensor_poller())

@app.on_event("shutdown")
async def shutdown_event(): await ha_client.close()

# API Endpoints
@app.get("/api/ha/entities")
async def get_ha_entities():
    s = await ha_client.get_all_states()
    return [{"id": x["entity_id"], "name": x.get("attributes", {}).get("friendly_name", x["entity_id"])} for x in s]

@app.get("/api/settings")
async def get_settings():
    db = SessionLocal()
    res = {s.key: s.value for s in db.query(SystemSetting).all()}
    db.close(); return res

@app.post("/api/settings")
async def save_settings(data: dict):
    db = SessionLocal()
    for k, v in data.items():
        s = db.query(SystemSetting).filter(SystemSetting.key == k).first()
        if not s: db.add(SystemSetting(key=k, value=v))
        else: s.value = v
    db.commit(); db.close()
    load_handlers(); return {"status": "ok"}

@app.get("/api/solar_detailed")
async def get_solar_detailed():
    from app.models.database import SolarHourlyStat
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        history = {h.hour: h for h in db.query(SolarHourlyStat).filter(SolarHourlyStat.timestamp >= now.replace(hour=0,minute=0,second=0)).all()}
        factors = get_solar_correction_factors()
        combined = []
        forecast_array = price_arrays.get("solar_forecast_today", [0]*24)
        for h in range(24):
            hist = history.get(h)
            act = hist.actual_kwh if hist else 0
            f = hist.forecast_kwh if hist else (forecast_array[h] if h < len(forecast_array) else 0)
            if f > 100: f /= 1000.0
            combined.append({"hour": h, "actual": act, "forecast": f, "corrected": f * factors.get(h, 1.0)})
        return {"history": combined, "factors": factors}
    finally: db.close()

@app.get("/api/house_detailed")
async def get_house_detailed():
    from app.models.database import HouseHourlyStat
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        history = {h.hour: h for h in db.query(HouseHourlyStat).filter(HouseHourlyStat.timestamp >= now.replace(hour=0,minute=0,second=0)).all()}
        cutoff = now - datetime.timedelta(days=28)
        from sqlalchemy import text
        archive = db.query(HouseHourlyStat).filter(HouseHourlyStat.timestamp >= cutoff, HouseHourlyStat.timestamp < now.replace(hour=0,minute=0,second=0), text(f"strftime('%w', timestamp) = '{now.strftime('%w')}'")).all()
        arch_map = {}
        counts = {}
        for s in archive:
            arch_map[s.hour] = arch_map.get(s.hour, 0) + s.actual_kwh
            counts[s.hour] = counts.get(s.hour, 0) + 1
        combined = []
        for h in range(24):
            combined.append({"hour": h, "actual": history.get(h).actual_kwh if history.get(h) else 0, "average": arch_map.get(h,0)/counts[h] if counts.get(h) else 0})
        return {"history": combined}
    finally: db.close()

@app.get("/api/dashboard")
async def get_dashboard():
    return {"sensors": current_sensors, "inverter_state": inverter.current_state.name, "prices": price_arrays, "loads": [{"name": h.name, "id": h.entity_id, "state": h.current_state, "priority": h.priority, "schema": h.get_config_schema()} for h in handlers]}

@app.middleware("http")
async def add_headers(request: Request, call_next):
    root = request.headers.get("X-Ingress-Path", "")
    if root: request.scope["root_path"] = root
    response = await call_next(request)
    response.headers.update({"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0", "X-Version": "1.3.49"})
    return response

if os.path.exists("app/static"):
    app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
