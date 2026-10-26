import sqlite3
import datetime
import os
import re

DB_PATH = 'tracker.db'
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

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
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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

def parse_single_date_ist(token_str, default_year):
    """Parses a single date string into a datetime.date object."""
    s = token_str.strip().lower()
    # Try YYYY-MM-DD or YYYY/MM/DD
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
                '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y',
                '%d-%m-%y', '%d/%m/%y', '%d.%m.%y',
                '%d %b %Y', '%d %B %Y', '%b %d %Y', '%B %d %Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    # Formats without year (assume default_year)
    for fmt in ('%d-%m', '%d/%m', '%d.%m', '%d %b', '%d %B', '%b %d', '%B %d'):
        try:
            dt = datetime.datetime.strptime(f"{s} {default_year}", f"{fmt} %Y")
            return dt.date()
        except ValueError:
            pass

    return None

def parse_sale_query_ist(raw_text):
    """
    Parses a /sale command argument string and returns:
      (start_utc, end_utc, label, is_multi_day, error_msg)
    where start_utc and end_utc are naive UTC datetimes matching sales_log.timestamp.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    now_ist = now_utc + IST_OFFSET
    today_ist = now_ist.date()

    # Strip '/sale' or '/sale@botname' prefix
    text = (raw_text or '').strip()
    text = re.sub(r'^/sales?(?:@\S+)?\s*', '', text, flags=re.IGNORECASE).strip()
    arg = text.lower()

    if not arg or arg in ('today', 'now'):
        start_ist = datetime.datetime.combine(today_ist, datetime.time.min)
        end_ist = now_ist
        label = f"TODAY ({today_ist.strftime('%d %b %Y')}, Since Midnight IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, False, None

    if arg in ('help', '?'):
        return None, None, None, False, "HELP"

    if arg in ('yesterday', 'yest', 'yd'):
        yest = today_ist - datetime.timedelta(days=1)
        start_ist = datetime.datetime.combine(yest, datetime.time.min)
        end_ist = datetime.datetime.combine(yest, datetime.time.max)
        label = f"YESTERDAY ({yest.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, False, None

    if arg in ('all', 'total', 'lifetime', 'alltime', 'all-time'):
        start_ist = datetime.datetime(2020, 1, 1, 0, 0, 0)
        end_ist = now_ist
        label = f"ALL-TIME TOTAL (Up to {today_ist.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, True, None

    if arg in ('week', '7d', '7days', 'last7d'):
        start_date = today_ist - datetime.timedelta(days=6)
        start_ist = datetime.datetime.combine(start_date, datetime.time.min)
        end_ist = now_ist
        label = f"LAST 7 DAYS ({start_date.strftime('%d %b %Y')} to {today_ist.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, True, None

    if arg in ('month', 'mtd', 'thismonth'):
        start_date = today_ist.replace(day=1)
        start_ist = datetime.datetime.combine(start_date, datetime.time.min)
        end_ist = now_ist
        label = f"THIS MONTH ({start_date.strftime('%d %b %Y')} to {today_ist.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, True, None

    if arg in ('lastmonth', 'prevmonth'):
        first_this_month = today_ist.replace(day=1)
        last_prev_month = first_this_month - datetime.timedelta(days=1)
        first_prev_month = last_prev_month.replace(day=1)
        start_ist = datetime.datetime.combine(first_prev_month, datetime.time.min)
        end_ist = datetime.datetime.combine(last_prev_month, datetime.time.max)
        label = f"LAST MONTH ({first_prev_month.strftime('%d %b %Y')} to {last_prev_month.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, True, None

    # Match Nd (e.g. 3d, 10d, 14d, 30d, 90d)
    m_days = re.match(r'^(\d+)\s*d(?:ays?)?$', arg)
    if m_days:
        n_days = max(1, int(m_days.group(1)))
        start_date = today_ist - datetime.timedelta(days=n_days - 1)
        start_ist = datetime.datetime.combine(start_date, datetime.time.min)
        end_ist = now_ist
        label = f"LAST {n_days} DAYS ({start_date.strftime('%d %b %Y')} to {today_ist.strftime('%d %b %Y')} IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, (n_days > 1), None

    # Check if it's a date range: separated by ' to ', ' - ', ' .. ', ' till ', ' until ', or two space-separated date tokens
    range_parts = re.split(r'\s+(?:to|till|until|\.\.|-)\s+', arg)
    if len(range_parts) != 2:
        # Also support two space-separated date strings like: /sale 01-10-2026 25-10-2026
        tokens = arg.split()
        if len(tokens) == 2:
            d1 = parse_single_date_ist(tokens[0], today_ist.year)
            d2 = parse_single_date_ist(tokens[1], today_ist.year)
            if d1 and d2:
                range_parts = [tokens[0], tokens[1]]

    if len(range_parts) == 2:
        d1 = parse_single_date_ist(range_parts[0], today_ist.year)
        d2 = parse_single_date_ist(range_parts[1], today_ist.year)
        if d1 and d2:
            if d1 > d2:
                d1, d2 = d2, d1
            start_ist = datetime.datetime.combine(d1, datetime.time.min)
            end_ist = datetime.datetime.combine(d2, datetime.time.max)
            label = f"{d1.strftime('%d %b %Y')} to {d2.strftime('%d %b %Y')} (IST)"
            return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, (d1 != d2), None

    # Otherwise try parsing as a single date
    single_date = parse_single_date_ist(arg, today_ist.year)
    if single_date:
        start_ist = datetime.datetime.combine(single_date, datetime.time.min)
        end_ist = datetime.datetime.combine(single_date, datetime.time.max)
        label = f"DATE: {single_date.strftime('%d %b %Y')} (IST)"
        return start_ist - IST_OFFSET, end_ist - IST_OFFSET, label, False, None

    return None, None, None, False, f"Could not understand date format: <code>{text}</code>"

def get_sales_summary_for_range(start_utc, end_utc):
    """Returns store summaries, top products, and IST daily breakdown between start_utc and end_utc."""
    conn = get_connection()
    cursor = conn.cursor()

    start_str = start_utc.strftime('%Y-%m-%d %H:%M:%S.%f')
    end_str = end_utc.strftime('%Y-%m-%d %H:%M:%S.%f')

    cursor.execute('''
        SELECT 
            domain,
            SUM(qty_sold) as total_items,
            SUM(price * qty_sold) as total_revenue
        FROM sales_log
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY domain
        ORDER BY total_revenue DESC
    ''', (start_str, end_str))
    store_summaries = cursor.fetchall()

    cursor.execute('''
        SELECT 
            domain,
            product_title,
            variant_title,
            SUM(qty_sold) as total_qty,
            SUM(price * qty_sold) as total_rev
        FROM sales_log
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY domain, variant_id
        ORDER BY total_rev DESC
        LIMIT 10
    ''', (start_str, end_str))
    top_products = cursor.fetchall()

    cursor.execute('''
        SELECT 
            date(timestamp, '+5 hours', '+30 minutes') as ist_date,
            SUM(qty_sold) as total_qty,
            SUM(price * qty_sold) as total_rev
        FROM sales_log
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY ist_date
        ORDER BY ist_date DESC
    ''', (start_str, end_str))
    daily_breakdown = cursor.fetchall()

    conn.close()
    return store_summaries, top_products, daily_breakdown

def get_daily_sales_summary():
    """Backwards-compatible helper returning (store_summaries, top_products) for today in IST."""
    start_utc, end_utc, _, _, _ = parse_sale_query_ist('/sale')
    store_summaries, top_products, _ = get_sales_summary_for_range(start_utc, end_utc)
    # Match legacy 4-tuple format (product_title, variant_title, total_qty, total_rev)
    legacy_top = [(p[1], p[2], p[3], p[4]) for p in top_products]
    return store_summaries, legacy_top

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
