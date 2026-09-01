import sqlite3
import uuid

DB_PATH = 'pos.db'

def create_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Added agent and request_type columns to the schema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pos (
            session_id TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            owner TEXT DEFAULT NULL,
            agent TEXT DEFAULT NULL,
            request_type TEXT DEFAULT NULL,
            item_type TEXT NOT NULL,
            weight DECIMAL(10,2) DEFAULT 0.00,
            weight_awarded DECIMAL(10,2) DEFAULT 0.00,
            recycled_at DATETIME NOT NULL,
            PRIMARY KEY (session_id, item_type)
        )
    ''')
        
    conn.commit()
    conn.close()

def session_id_exists(session_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM pos WHERE session_id = ?', (session_id,))
    exists = cursor.fetchone()[0] > 0
    conn.close()
    return exists 

def create_session_id():
    session_id = f"SESS-{uuid.uuid4().hex[:7].upper()}"
    while session_id_exists(session_id):
        session_id = f"SESS-{uuid.uuid4().hex[:7].upper()}"
    return session_id

def insert_or_update_values(session_id, machine_name, owner, agent, request_type, item_type, weight, weight_awarded, recycled_at):
    create_table()
    
    fixed_machine_name = str(machine_name).replace("Smartscale-", "SmartScale-")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO pos (session_id, machine_name, owner, agent, request_type, item_type, weight, weight_awarded, recycled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, item_type) DO UPDATE SET
                machine_name = excluded.machine_name,
                owner = excluded.owner,
                agent = excluded.agent,
                request_type = excluded.request_type,
                weight = excluded.weight,
                weight_awarded = excluded.weight_awarded,
                recycled_at = excluded.recycled_at
        ''', (session_id, fixed_machine_name, owner, agent, request_type, item_type, weight, weight_awarded, recycled_at))
        
        conn.commit()
        print(f"Data successfully logged for Session: {session_id} | Item: {item_type}")
        
    except sqlite3.IntegrityError as e:
        print("Integrity Error executing local pipeline statement:", e)
    finally:
        conn.close()