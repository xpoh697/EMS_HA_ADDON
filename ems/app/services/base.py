from abc import ABC, abstractmethod
from typing import Dict, Any, List

class BaseLoadHandler(ABC):
    """
    Abstract base class for all controllable loads.
    Following System Engineering principles for modularity and reuse.
    """
    def __init__(self, name: str, entity_id: str, priority: int = 10):
        self.name = name
        self.entity_id = entity_id
        self.priority = priority
        self.current_state = "idle"
        self.nominal_power_w = 2000.0  # Default estimate in Watts

    @abstractmethod
    def decide(self, sensors: Dict[str, Any], can_use_energy: bool, available_power_w: float) -> bool:
        """
        Decision logic for the load.
        :param sensors: Current HA sensor data.
        :param can_use_energy: Boolean from InverterController.
        :param available_power_w: Current margin before reaching Grid Limit.
        :return: True to request ON, False to request OFF.
        """
        pass

    @abstractmethod
    def get_config_schema(self) -> Dict[str, Any]:
        """
        Returns a schema for the UI to render dynamic settings.
        """
        return {
            "entity_id": {"type": "string", "label": "Entity ID"},
            "priority": {"type": "number", "label": "Priority"}
        }

    def get_power_usage(self, sensors: Dict[str, Any]) -> float:
        """Returns current power usage from sensors or estimate."""
        return float(sensors.get(f"{self.entity_id}_power", 0.0))
