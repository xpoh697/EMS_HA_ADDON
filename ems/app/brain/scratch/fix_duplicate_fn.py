
import os

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Identify the duplicate block around lines 400-500
# Starting from line 400 (0-indexed 399)
# Ending at line 500 (0-indexed 499)

# Verification: check if line 400 starts with '# Price arrays' 
# or line 418 starts with 'def extract_price_array'
if lines[399].strip().startswith("# Price arrays") and "def extract_price_array(raw, target_date=None):" in lines[417]:
    print("Confirmed duplicate block at 400-500. Deleting...")
    # Delete lines 400 to 500 (inclusive of the blank line after)
    del lines[399:500]
    
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    print("Deleted duplicate function successfully!")
else:
    print(f"Mismatch at expected line 400/418. Line 400: '{lines[399].strip()}'. Line 418: '{lines[417].strip()}'.")
    # SEARCH logic if line numbers shifted
    start_found = -1
    for i, line in enumerate(lines):
        if i > 250 and "# Price arrays for the chart" in line: # Skip the first one
            start_found = i
            break
    
    if start_found != -1:
        # Find the end of this duplicate block (next logical section or blank lines)
        end_found = start_found
        for i in range(start_found, len(lines)):
            if "def load_handlers():" in lines[i]: # The next func after the duplicate
                end_found = i
                break
        
        print(f"Found duplicate block starting at line {start_found+1} to {end_found}. Deleting...")
        del lines[start_found:end_found]
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(lines)
        print("Deleted duplicate function using search successfully!")
    else:
        print("Could not find duplicate block using search.")
