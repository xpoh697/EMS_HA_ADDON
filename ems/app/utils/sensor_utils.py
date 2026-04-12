def get_sensor_value(state_obj: dict, attr_name: str = None):
    """Extracts numeric value from HA state object, optionally from an attribute."""
    if not state_obj: return 0
    try:
        if attr_name and attr_name in state_obj.get("attributes", {}):
            return float(state_obj["attributes"][attr_name])
        return float(state_obj.get("state", 0))
    except (ValueError, TypeError):
        return 0
