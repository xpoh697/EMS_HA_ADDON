
import os

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    lines = f.readlines()

def find_all_indices(pattern, lines_list):
    return [i for i, line in enumerate(lines_list) if pattern in line]

# We know we have duplicates for:
# 1. price_arrays = {
# 2. def get_sensor_value
# 3. def extract_price_array

price_array_indices = find_all_indices("price_arrays = {", lines)
sensor_value_indices = find_all_indices("def get_sensor_value", lines)
extract_array_indices = find_all_indices("def extract_price_array", lines)

print(f"Indices for price_arrays: {price_array_indices}")
print(f"Indices for get_sensor_value: {sensor_value_indices}")
print(f"Indices for extract_price_array: {extract_array_indices}")

# We want to keep the FIRST one of each (if it's the new one) or the one that is correct.
# Actually, the FIRST one of extract_price_array (line 202) is the one with attr_name.
# The SECOND one (line 417) is the old one.
# So we should delete the block containing the second occurrences.

if len(price_array_indices) > 1 and len(extract_array_indices) > 1:
    start_del = price_array_indices[1] # Start of the second block (line 399 approx)
    
    # End of deletion should be before the next legitimate function.
    # The next function after the second extract_price_array is load_handlers or save_hourly_solar_stats.
    end_del = -1
    for i in range(start_del + 10, len(lines)):
        if "async def save_hourly_solar_stats" in lines[i] or "def load_handlers():" in lines[i]:
            # Check if this is also a duplicate. 
            # save_hourly_solar_stats is at line 616. 
            # If we find another one earlier, it's a mess.
            end_del = i
            break
    
    if start_del != -1 and end_del != -1:
        print(f"Deleting duplicate block from index {start_del} to {end_del}...")
        del lines[start_del:end_del]
        
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(lines)
        print("Cleanup successful!")
    else:
        print("Could not determine deletion range.")
else:
    print("Insufficient duplicates found for auto-cleanup.")
