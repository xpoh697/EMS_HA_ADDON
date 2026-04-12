import sqlite3
import os

db_path = r"e:\systemair\EMS\ems\app\data\ems.db"
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
    exit()

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- SolarHourlyStat (Last 24 items) ---")
cursor.execute("SELECT timestamp, hour, actual_kwh, forecast_kwh FROM SolarHourlyStat ORDER BY timestamp DESC LIMIT 24")
rows = cursor.fetchall()
for row in rows:
    print(f"Time: {row[0]}, Hour: {row[1]}, Actual: {row[2]}, Forecast: {row[3]}")

print("\n--- Current Settings (Global Sensors) ---")
cursor.execute("SELECT category, key, value FROM settings WHERE category='global_sensors'")
rows = cursor.fetchall()
for row in rows:
    print(f"{row[0]}.{row[1]}: {row[2]}")

conn.close()
