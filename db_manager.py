import sqlite3
import datetime
import os

DB_PATH = 'tracker.db'

def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    # Table to store the latest known stock for each variant
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory_state (
            domain TEXT,
            variant_id TEXT,
            product_title TEXT,
            variant_title TEXT,
            sku TEXT,
            price REAL,
            stock INTEGER,
            last_checked TIMESTAMP,
            PRIMARY KEY (domain, variant_id)
        )
    ''')
    
    # Ledger to store every sale event
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT,
            variant_id TEXT,
            product_title TEXT,
            variant_title TEXT,
            sku TEXT,
            price REAL,
            qty_sold INTEGER,
            timestamp TIMESTAMP
        )
    ''')

    # State table to remember Telegram offset
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_last_update_id():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM bot_state WHERE key = "last_update_id"')
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None

def set_last_update_id(update_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('last_update_id', str(update_id)))
    conn.commit()
    conn.close()

def get_all_known_stock(domain):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT variant_id, stock FROM inventory_state WHERE domain = ?', (domain,))
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

def update_inventory_state(domain, item):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.datetime.now()
    cursor.execute('''
        INSERT INTO inventory_state (domain, variant_id, product_title, variant_title, sku, price, stock, last_checked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain, variant_id) DO UPDATE SET
            stock = excluded.stock,
            price = excluded.price,
            product_title = excluded.product_title,
            last_checked = excluded.last_checked
    ''', (
        domain,
        item['variant_id'],
        item.get('product_title', ''),
        item.get('variant_title', ''),
        item.get('sku', ''),
        item.get('price', 0.0),
        item['stock'],
        now
    ))
    conn.commit()
    conn.close()

def record_sale(domain, item, qty_sold):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.datetime.now()
    cursor.execute('''
        INSERT INTO sales_log (domain, variant_id, product_title, variant_title, sku, price, qty_sold, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        domain,
        item['variant_id'],
        item.get('product_title', ''),
        item.get('variant_title', ''),
        item.get('sku', ''),
        item.get('price', 0.0),
        qty_sold,
        now
    ))
    conn.commit()
    conn.close()

def get_daily_sales_summary():
    """Returns a summary of sales from midnight today."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Midnight today
    today_midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    cursor.execute('''
        SELECT 
            domain,
            SUM(qty_sold) as total_items,
            SUM(price * qty_sold) as total_revenue
        FROM sales_log
        WHERE timestamp >= ?
        GROUP BY domain
    ''', (today_midnight,))
    
    store_summaries = cursor.fetchall()
    
    cursor.execute('''
        SELECT 
            product_title,
            variant_title,
            SUM(qty_sold) as total_qty,
            SUM(price * qty_sold) as total_rev
        FROM sales_log
        WHERE timestamp >= ?
        GROUP BY variant_id
        ORDER BY total_rev DESC
        LIMIT 10
    ''', (today_midnight,))
    
    top_products = cursor.fetchall()
    conn.close()
    
    return store_summaries, top_products

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
