import os
import re

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: The DetachedInstanceError on global_sensors
target_regex = re.compile(
    r"(\s*)db = SessionLocal\(\)\s+"
    r"setting = db\.query\(SystemSetting\)\.filter\(SystemSetting\.key == \"global_sensors\"\)\.first\(\)\s+"
    r"db\.close\(\)",
    re.MULTILINE
)

def repl(m):
    indent = m.group(1)
    return (f"{indent}db_glob = SessionLocal()\n"
            f"{indent}try:\n"
            f"{indent}    setting = db_glob.query(SystemSetting).filter(SystemSetting.key == 'global_sensors').first()\n"
            f"{indent}    config = setting.value if setting else None\n"
            f"{indent}finally:\n"
            f"{indent}    db_glob.close()")

if target_regex.search(content):
    content = target_regex.sub(repl, content)
    # Correct the reference to config later
    content = content.replace("if setting:\n                config = setting.value", "if config:")
    
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("Fixed setting DetachedInstanceError!")
else:
    print("Could not find global_sensors target block! Maybe already fixed?")
