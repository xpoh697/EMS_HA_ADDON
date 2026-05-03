import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

class InverterPlanner:
    """Manages the 48-hour schedule for the inverter."""
    def __init__(self, manager):
        self.manager = manager
        # schedule: {hour_abs: {mode, power, target_soc, amps, source}}
        self.schedule: Dict[int, Dict[str, Any]] = {}
        self._load_schedule()

    def _get_hour_abs(self, dt: datetime) -> int:
        """Convert datetime to absolute hour (timestamp // 3600)."""
        return int(dt.timestamp() // 3600)

    def propose(self, hour_abs: int, mode: str, power: float = 0.0, target_soc: float = 0.0, amps: float = 0.0, source: str = "system"):
        """Propose a change to the schedule. System cannot override User."""
        existing = self.schedule.get(hour_abs)
        if existing and existing.get("source") == "user" and source == "system":
            return

        self.schedule[hour_abs] = {
            "mode": mode,
            "power": round(float(power), 3),
            "target_soc": round(float(target_soc), 1),
            "amps": round(float(amps), 2),
            "source": source,
            "updated_at": dt_util.now().isoformat()
        }
        
        # Save immediately if it's a user override
        if source == "user":
            self.save_schedule()

    def set_mode(self, mode: str, hour: Optional[int] = None, power: float = 0.0, target_soc: float = 0.0, source: str = "system"):
        """Convenience method for setting a mode."""
        h_abs = hour if hour is not None else self._get_hour_abs(dt_util.now())
        self.propose(h_abs, mode, power=power, target_soc=target_soc, source=source)

    def get_plan_at(self, dt: datetime) -> Dict[str, Any]:
        """Get the planned state for a specific datetime."""
        h_abs = self._get_hour_abs(dt)
        return self.get_plan(h_abs)

    def get_plan(self, hour_abs: int) -> Dict[str, Any]:
        """Get the planned state for a specific absolute hour."""
        return self.schedule.get(hour_abs, {
            "mode": "sale_pv",
            "power": 0.0,
            "target_soc": 0.0,
            "amps": 0.0,
            "source": "system"
        })

    def get_48h_schedule(self) -> List[Dict[str, Any]]:
        """Returns a list of 48 hours starting from now."""
        now = dt_util.now()
        start_h = self._get_hour_abs(now)
        res = []
        for i in range(48):
            h_abs = start_h + i
            dt = datetime.fromtimestamp(h_abs * 3600, tz=dt_util.DEFAULT_TIME_ZONE)
            plan = self.get_plan(h_abs)
            item = {
                "hour_abs": h_abs,
                "time": dt.strftime("%Y-%m-%d %H:00"),
                **plan
            }
            res.append(item)
        return res

    def cleanup(self):
        """Remove past hours and future system plans to allow re-planning."""
        now_h = self._get_hour_abs(dt_util.now())
        
        # 1. Remove past
        to_del = [h for h in self.schedule if h < now_h]
        for h in to_del:
            del self.schedule[h]
            
        # 2. Remove future system plans (to allow fresh planning by strategies)
        to_del_sys = [h for h, v in self.schedule.items() if v.get("source") == "system"]
        for h in to_del_sys:
            del self.schedule[h]

    def _load_schedule(self):
        """Load from manager storage."""
        stored = self.manager.data.get("planner_schedule", {})
        # Convert keys back to int
        self.schedule = {int(k): v for k, v in stored.items()}
        self.cleanup()

    def save_schedule(self):
        """Save to manager storage."""
        self.cleanup()
        # Limit to next 72 hours for safety/cleanup
        cur_h = self._get_hour_abs(dt_util.now())
        clean_schedule = {h: v for h, v in self.schedule.items() if h < cur_h + 72}
        self.manager.data["planner_schedule"] = clean_schedule
        # The manager will save this data in its next sync cycle or we can force it
        if hasattr(self.manager, "async_save_data"):
             self.manager.hass.async_create_task(self.manager.async_save_data())
