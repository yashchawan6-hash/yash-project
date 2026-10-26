import json
import time
import datetime
import threading
import requests
from db_manager import (
    init_db,
    get_all_known_stock,
    update_inventory_state,
    record_sale,
    parse_sale_query_ist,
    get_sales_summary_for_range
)
from cron_runner import handle_sale_command, send_telegram_message, send_telegram_photo
from shopify_scraper import harvest_storefront_token, fetch_current_inventory

CONFIG_FILE = 'config.json'

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def send_sale_alert(config, domain, item, qty_sold, old_stock, new_stock):
    bot_token = config['telegram_bot_token']
    chat_id = config['telegram_chat_id']
    
    event_type = 'SOLD OUT' if new_stock == 0 else 'NEW SALE'
    emoji = "🚨" if new_stock == 0 else "🛍️"
    
    caption = f"{emoji} <b>{event_type}!</b> {emoji}\n\n" \
              f"<b>Store:</b> {domain}\n" \
              f"<b>Product:</b> {item.get('product_title')}\n" \
              f"<b>Variant:</b> {item.get('variant_title')}\n" \
              f"<b>Price:</b> ₹{item.get('price'):,.2f}\n" \
              f"<b>Qty Sold:</b> {qty_sold}\n" \
              f"<b>Stock:</b> {old_stock} ➡️ {new_stock}\n\n" \
              f"🔗 <a href='{item.get('url')}'>View Product</a>"
              
    if item.get('image_url'):
        send_telegram_photo(bot_token, chat_id, item['image_url'], caption)
    else:
        send_telegram_message(bot_token, chat_id, caption)

def telegram_polling_loop():
    print("Started Telegram polling loop...")
    config = load_config()
    bot_token = config['telegram_bot_token']
    chat_id = config['telegram_chat_id']
    offset = None
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {'timeout': 30}
            if offset:
                params['offset'] = offset
            
            r = requests.get(url, params=params, timeout=35)
            if r.status_code == 200:
                data = r.json()
                for update in data.get('result', []):
                    offset = update['update_id'] + 1
                    msg = update.get('message', {})
                    text = (msg.get('text') or '').strip()
                    if text.lower().startswith('/sale'):
                        print(f"Received '{text}' command from {msg.get('chat', {}).get('id')}")
                        handle_sale_command(bot_token, chat_id, text)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)

def tracker_loop():
    print("Started inventory tracking loop...")
    while True:
        try:
            config = load_config()
            for domain in config['target_domains']:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Scanning {domain}...")
                token = harvest_storefront_token(domain)
                if not token:
                    print(f"Could not find token for {domain}. Skipping.")
                    continue
                    
                known_stock = get_all_known_stock(domain)
                current_inventory = fetch_current_inventory(domain, token)
                
                for vid, item in current_inventory.items():
                    new_stock = item['stock']
                    old_stock = known_stock.get(vid)
                    
                    if old_stock is not None:
                        if 0 <= new_stock < old_stock:
                            qty_sold = old_stock - new_stock
                            print(f"SALE DETECTED: {domain} - {item.get('product_title')} ({qty_sold} sold)")
                            record_sale(domain, item, qty_sold)
                            send_sale_alert(config, domain, item, qty_sold, old_stock, new_stock)
                    
                    update_inventory_state(domain, item)
                    
            interval = config.get('check_interval_minutes', 5)
            print(f"Scan complete. Sleeping for {interval} minutes.")
            time.sleep(interval * 60)
        except Exception as e:
            print(f"Tracker loop error: {e}")
            time.sleep(60)

if __name__ == '__main__':
    init_db()
    
    t = threading.Thread(target=telegram_polling_loop, daemon=True)
    t.start()
    
    tracker_loop()
