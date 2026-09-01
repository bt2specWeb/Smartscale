import sqlite3
import requests
import time

DB_FILE = "/home/ecorvmpi/pos.db"
API_URL = "https://app.ecobarter.africa/api/pos"

def sync_offline_data():
    try:
        # Connect to SQLite database
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pos'")
        if not cursor.fetchone():
            print("No 'pos' table found. Nothing to sync.")
            return

        # Get all unique transaction sessions
        cursor.execute("SELECT DISTINCT session_id FROM pos")
        sessions = cursor.fetchall()

        if not sessions:
            print("Database is empty. No offline transactions pending.")
            return

        print(f"Found {len(sessions)} offline session(s) to sync. Starting...")

        for session_row in sessions:
            session_id = session_row['session_id']
            
            # Fetch all items for this specific session
            cursor.execute("SELECT * FROM pos WHERE session_id = ?", (session_id,))
            rows = cursor.fetchall()

            if not rows: 
                continue

            # Reconstruct the exact JSON payload the backend expects
            first_row = rows[0]
            payload = {
                "machine_name": str(first_row["machine_name"]),
                "session_id": str(first_row["session_id"]),
                "owner": str(first_row["owner"]),
                "agent": str(first_row["agent"]),
                "request_type": str(first_row["request_type"]),
                "recycled_at": str(first_row["recycled_at"]),
                "items": []
            }

            # Append the nested items array
            for r in rows:
                payload["items"].append({
                    "item_type": str(r["item_type"]),
                    "weight": float(r["weight"]),
                    "weight_awarded": float(r["weight_awarded"])
                })

            print(f"\n---> Sending Session: {session_id}")
            
            try:
                # Send to CakePHP Backend
                resp = requests.post(API_URL, json=payload, timeout=10)
                
                # Check for success (200/201)
                if resp.status_code in [200, 201]:
                    # Delete ONLY the synced rows so we don't drop new ones
                    cursor.execute("DELETE FROM pos WHERE session_id = ?", (session_id,))
                    conn.commit()
                    print(f"✅ Success! Session {session_id} synced and cleared locally.")
                else:
                    print(f"❌ Failed to sync {session_id}. Code: {resp.status_code}")
                    print(f"Response: {resp.text}")
                    
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Network error while syncing {session_id}: {e}")
                break # Stop trying if network is down

    except Exception as e:
        print(f"Database error: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    sync_offline_data()