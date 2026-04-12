def get_sensor_value(state_obj: dict, attr_name: str = None):
    """Extracts numeric value from HA state object, optionally from an attribute."""
    if not state_obj: return 0
    try:
        if attr_name:
            # Try exact match, then fuzzy match for attributes
            attrs = state_obj.get("attributes", {})
            if attr_name in attrs:
                val = attrs[attr_name]
            else:
                val = fuzzy_get(attrs, [attr_name])
        else:
            val = state_obj.get("state")
            
        return float(val) if val not in [None, 'unavailable', 'unknown'] else 0
    except:
        return 0

def fuzzy_get(data: dict, keys: list):
    """Finds a value in a dictionary by matching keys ignoring case, spaces, and underscores."""
    if not data or not keys: return None
    
    def normalize(s):
        return str(s).lower().replace("_", "").replace(" ", "")

    # Pre-normalize targets
    targets = [normalize(k) for k in keys]
    
    for k, v in data.items():
        if normalize(k) in targets:
            return v
    return None
