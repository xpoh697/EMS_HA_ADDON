
import sqlite3
import json

db_path = "e:/systemair/EMS/ems.db"

def check_db():
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        print("--- System Settings ---")
        cursor.execute("SELECT key, value FROM system_settings")
        for row in cursor.fetchall():
            key, val = row
            try:
                # Try to pretty print JSON values
                data = json.loads(val)
                print(f"{key}: {json.dumps(data, indent=2)}")
            except:
                print(f"{key}: {val}")
        
        print("\n--- Current Tracker States (tracking_state) ---")
        cursor.execute("SELECT value FROM system_settings WHERE key='tracking_state'")
        row = cursor.fetchone()
        if row:
            print(json.dumps(json.loads(row[0]), indent=2))
        else:
            print("No tracking_state found.")

        conn.close()
    except Exception as e:
        print(f"Error checking DB: {e}")

if __name__ == "__main__":
    check_db()
