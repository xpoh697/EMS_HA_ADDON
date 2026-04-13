import datetime
import logging
import os
import json
from app.providers.hass import HomeAssistantClient
from app.services.inverter import InverterController
from app.services.occupancy import OccupancyEngine
from app.services.guardian import PowerGuardian
from app.models.database import SessionLocal, SystemSetting

logger = logging.getLogger(__name__)

class SystemState:
    def __init__(self):
        # Global Sensor Data
        self.current_sensors = {
            "battery_soc": 0, "solar_power": 0, "buy_price": 0, "sell_price": 0, "house_power": 0,
            "survival_soc": 20, "price_tomorrow": 0, "currency": "EUR", "current_hour": 0,
            "solar_forecast_today": 0, "solar_forecast_tomorrow": 0,
            "solar_energy_total": 0, "solar_energy_today": 0, "house_energy_today": 0
        }

        # Pricing Buffers (initialized with zeros to prevent UI 'No Data' error)
        self.price_arrays = {
            "buy_prices_today": [0.0] * 24, "buy_prices_tomorrow": [0.0] * 24,
            "sell_prices_today": [0.0] * 24, "sell_prices_tomorrow": [0.0] * 24,
            "solar_forecast_today": [0.0] * 24, "solar_forecast_tomorrow": [0.0] * 24
        }

        # Tracking (Hour Start State)
        self.solar_tracking = {
            "hour_start_ts": None,
            "integration_sum_watts": 0,
            "sample_count": 0,
            "hour_start_energy": None,
            "day_start_energy": None
        }

        self.house_tracking = {
            "hour_start_ts": None,
            "integration_sum_watts": 0,
            "sample_count": 0,
            "hour_start_energy": None
        }

        # Shared Service Instances
        self.inverter = InverterController(dry_run=True)
        self.occupancy = OccupancyEngine()
        self.guardian = PowerGuardian(max_grid_power_w=11000.0)
        self.handlers = []
        
        # HA Client Setup
        token = self._resolve_ha_token()
        self.ha_client = HomeAssistantClient(base_url="http://supervisor/core/api", token=token)
        
        self.last_state_save_ts = datetime.datetime.min
        self._solar_history_recovered = False
        self._house_history_recovered = False

    def _resolve_ha_token(self):
        token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "REPLACE_ME")
        
        # Path for Addon options
        if os.path.exists("/data/options.json"):
            try:
                with open("/data/options.json", "r") as f:
                    opts = json.load(f)
                    manual = opts.get("ha_token")
                    if manual and len(manual) > 20:
                        logger.info("Using manual HA_TOKEN from options.json")
                        return manual
            except: pass

        # Database Fallback
        db = SessionLocal()
        try:
            setting = db.query(SystemSetting).filter(SystemSetting.key == "ha_token").first()
            if setting and setting.value:
                return setting.value
        finally:
            db.close()
        return token

    def save_tracking_states(self, force=False):
        now = datetime.datetime.now()
        if not force and (now - self.last_state_save_ts).total_seconds() < 300:
            return
            
        db = SessionLocal()
        try:
            state = {
                "solar": {
                    "hour_start_ts": self.solar_tracking["hour_start_ts"].isoformat() if self.solar_tracking["hour_start_ts"] else None,
                    "integration_sum_watts": self.solar_tracking["integration_sum_watts"],
                    "sample_count": self.solar_tracking["sample_count"],
                    "hour_start_energy": self.solar_tracking["hour_start_energy"]
                },
                "house": {
                    "hour_start_ts": self.house_tracking["hour_start_ts"].isoformat() if self.house_tracking["hour_start_ts"] else None,
                    "integration_sum_watts": self.house_tracking["integration_sum_watts"],
                    "sample_count": self.house_tracking["sample_count"],
                    "hour_start_energy": self.house_tracking["hour_start_energy"]
                }
            }
            setting = db.query(SystemSetting).filter(SystemSetting.key == "tracking_state").first()
            if not setting:
                db.add(SystemSetting(key="tracking_state", value=state))
            else:
                setting.value = state
            db.commit()
            self.last_state_save_ts = now
        except Exception as e:
            logger.error(f"Failed to save tracking states: {e}")
        finally:
            db.close()

    def load_tracking_states(self):
        db = SessionLocal()
        try:
            setting = db.query(SystemSetting).filter(SystemSetting.key == "tracking_state").first()
            if setting and setting.value:
                s = setting.value.get("solar", {})
                h = setting.value.get("house", {})
                if s.get("hour_start_ts"):
                    self.solar_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(s["hour_start_ts"])
                    self.solar_tracking["integration_sum_watts"] = s.get("integration_sum_watts", 0)
                    self.solar_tracking["sample_count"] = s.get("sample_count", 0)
                    self.solar_tracking["hour_start_energy"] = s.get("hour_start_energy")
                if h.get("hour_start_ts"):
                    self.house_tracking["hour_start_ts"] = datetime.datetime.fromisoformat(h["hour_start_ts"])
                    self.house_tracking["integration_sum_watts"] = h.get("integration_sum_watts", 0)
                    self.house_tracking["sample_count"] = h.get("sample_count", 0)
                    self.house_tracking["hour_start_energy"] = h.get("hour_start_energy")
        finally:
            db.close()

# Singleton Instance
state_manager = SystemState()
