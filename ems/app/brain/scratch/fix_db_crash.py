
import os

filepath = "e:/systemair/EMS/ems/app/main.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

target = """                # Load strategy limits from DB
                limits_setting = db.query(SystemSetting).filter(SystemSetting.key == "strategy_limits").first()
                if limits_setting:
                    current_sensors.update(limits_setting.value)"""

replacement = """                # Load strategy limits from DB
                db_loc = SessionLocal()
                try:
                    limits_setting = db_loc.query(SystemSetting).filter(SystemSetting.key == "strategy_limits").first()
                    if limits_setting:
                        current_sensors.update(limits_setting.value)
                finally:
                    db_loc.close()"""

if target in content:
    content = content.replace(target, replacement)
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("Replaced successfully!")
else:
    # Try line by line flexible matching
    import re
    target_regex = re.compile(
        r"([ \t]+)# Load strategy limits from DB\s+"
        r"limits_setting = db\.query\(SystemSetting\)\.filter\(SystemSetting\.key == \"strategy_limits\"\)\.first\(\)\s+"
        r"if limits_setting:\s+"
        r"current_sensors\.update\(limits_setting\.value\)",
        re.MULTILINE
    )
    if target_regex.search(content):
        def repl(m):
            indent = m.group(1)
            return (f"{indent}# Load strategy limits from DB\n"
                    f"{indent}db_loc = SessionLocal()\n"
                    f"{indent}try:\n"
                    f"{indent}    limits_setting = db_loc.query(SystemSetting).filter(SystemSetting.key == 'strategy_limits').first()\n"
                    f"{indent}    if limits_setting:\n"
                    f"{indent}        current_sensors.update(limits_setting.value)\n"
                    f"{indent}finally:\n"
                    f"{indent}    db_loc.close()")
        content = target_regex.sub(repl, content)
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        print("Replaced successfully using regex!")
    else:
        print("Target not found at all!")
