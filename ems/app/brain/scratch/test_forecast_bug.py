
import datetime

def extract_price_array_OLD(raw, target_date=None):
    if not raw:
        return [], False

    # 1. Handle Dictionary of ISO timestamps (e.g., Solcast wh_hours)
    if isinstance(raw, dict):
        try:
            sorted_keys = sorted(raw.keys())
            # THIS IS THE BUG: It just returns the values as-is. 
            # If there are 48 keys (30-min), it returns 48 items!
            return [float(raw[k]) for k in sorted_keys], True
        except:
            return [], False

    # (Skipping list logic for now as it already has buckets, but might be averaging energy incorrectly)
    return [], False

# Simulate 30-min Solcast data
raw_30m = {}
start = datetime.datetime(2026, 4, 12, 0, 0)
for i in range(48):
    ts = (start + datetime.timedelta(minutes=30*i)).isoformat()
    raw_30m[ts] = 1.0 + (i % 2) # Alternating values to see alignment

print("Simulated 48-item data:")
forecast_array, success = extract_price_array_OLD(raw_30m)
print(f"Array length: {len(forecast_array)}")

# Simulate the UI/API consumption loop
print("\nAPI Output (first 24 hours mapped directly):")
result = []
for h in range(24):
    # This is what main.py currently does:
    val = forecast_array[h] if h < len(forecast_array) else 0
    result.append(val)
    print(f"{h:02}:00 -> {val}")

print("\nOBSERVATION: At 23:00, we are actually seeing the value for index 23, which is 11:30 AM!")
