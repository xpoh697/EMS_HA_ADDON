import logging
from typing import Dict, Any, List
from app.models.enums import InverterState

logger = logging.getLogger(__name__)

class CyclicLoad:
    def __init__(self, entity_id: str, priority: int = 1):
        self.entity_id = entity_id
        self.priority = priority
        self.state = "idle" # idle, waiting, running
        self.cycle_start_time = None

class LoadScheduler:
    """
    Manages cyclic loads like washing machines and dishwashers.
    Follows "Active Cycle" logic to avoid interrupting running appliances.
    """
    def __init__(self):
        self.loads: List[CyclicLoad] = []

    def add_load(self, entity_id: str, priority: int = 1):
        self.loads.append(CyclicLoad(entity_id, priority))

    def manage(self, sensors: Dict[str, Any], can_use_energy: bool) -> Dict[str, bool]:
        """
        Determines which loads to turn ON/OFF.
        """
        commands = {}
        grid_price = sensors.get("grid_price", 10.0)
        
        # Sort by priority
        for load in sorted(self.loads, key=lambda x: x.priority):
            current_power = sensors.get(f"{load.entity_id}_power", 0.0)
            
            # 1. Detect active cycle (self-healing/protection)
            if current_power > 5.0: # Threshold for "Active"
                load.state = "running"
                commands[load.entity_id] = True
                continue

            # 2. If it was running but power dropped, it's either finished or in a pause
            if load.state == "running" and current_power < 2.0:
                # We stay ON for a while to allow the machine to finish (e.g. heating pause)
                # In a real system, we'd use a timer here.
                commands[load.entity_id] = True
                continue

            # 3. Decision for new cycle (waiting loads)
            if load.state == "waiting":
                if grid_price <= 0 or can_use_energy:
                    logger.info(f"Starting cyclic load {load.entity_id} due to favorable conditions.")
                    commands[load.entity_id] = True
                    load.state = "running"
                else:
                    commands[load.entity_id] = False
            else:
                commands[load.entity_id] = False

        return commands
