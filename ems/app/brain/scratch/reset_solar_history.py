
import sqlite3
import os

db_path = "e:/systemair/EMS/ems/app/ems.db"

if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
else:
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("Clearing 'solar_hourly_stats' table to reset poisoned correction factors...")
        cursor.execute("DELETE FROM solar_hourly_stats")
        
        conn.commit()
        conn.close()
        print("Successfully reset solar history history. Metrics will rebuild from the next hour.")
    except Exception as e:
        print(f"Error resetting database: {e}")
