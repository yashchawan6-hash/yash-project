import json
import time
import datetime
import threading
import requests
from db_manager import init_db, get_all_known_stock, update_inventory_state, record_sale, get_daily_sales_summary
from shopify_scraper import harvest_storefront_token, fetch_current_inventory

CONFIG_FILE = 'config.json'

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def send_telegram_message(bot_token, chat_id, text, parse_mode='HTML'):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending msg: {e}")

def send_telegram_photo(bot_token, chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = {'chat_id': chat_id, 'photo': photo_url, 'caption': caption, 'parse_mode': 'HTML'}
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code != 200:
            send_telegram_message(bot_token, chat_id, caption)
    except Exception as e:
        print(f"Error sending photo: {e}")
        send_telegram_message(bot_token, chat_id, caption)

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

def handle_sale_command(config):
    bot_token = config['telegram_bot_token']
    chat_id = config['telegram_chat_id']
    
    store_summaries, top_products = get_daily_sales_summary()
    
    msg = "📊 <b>DAILY SALES REPORT (Since Midnight)</b> 📊\n\n"
    
    total_rev = 0
    total_items = 0
    
    if not store_summaries:
        msg += "No sales recorded yet today.\n"
    else:
        for store, items, rev in store_summaries:
            msg += f"🏪 <b>{store}</b>: {items} items sold | ₹{rev:,.2f}\n"
            total_rev += rev
            total_items += items
            
        msg += f"\n💰 <b>TOTAL REVENUE:</b> ₹{total_rev:,.2f}\n"
        msg += f"📦 <b>TOTAL ITEMS SOLD:</b> {total_items}\n"
        
        if top_products:
            msg += "\n🏆 <b>TOP SELLING ITEMS TODAY:</b>\n"
            for p_title, v_title, qty, rev in top_products:
                v_text = f" ({v_title})" if v_title and v_title != 'Default Title' else ""
                msg += f"• {p_title}{v_text} - {qty} sold - ₹{rev:,.2f}\n"
                
    send_telegram_message(bot_token, chat_id, msg)

def telegram_polling_loop():
    print("Started Telegram polling loop...")
    config = load_config()
    bot_token = config['telegram_bot_token']
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
                    text = msg.get('text', '')
                    if text.startswith('/sale'):
                        print(f"Received /sale command from {msg.get('chat', {}).get('id')}")
                        handle_sale_command(config)
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
                        # Only consider it a sale if we previously knew the stock, 
                        # it has decreased, and the new stock is valid (>=0).
                        if 0 <= new_stock < old_stock:
                            qty_sold = old_stock - new_stock
                            print(f"SALE DETECTED: {domain} - {item.get('product_title')} ({qty_sold} sold)")
                            record_sale(domain, item, qty_sold)
                            send_sale_alert(config, domain, item, qty_sold, old_stock, new_stock)
                    
                    # Update database with current state
                    update_inventory_state(domain, item)
                    
            interval = config.get('check_interval_minutes', 5)
            print(f"Scan complete. Sleeping for {interval} minutes.")
            time.sleep(interval * 60)
        except Exception as e:
            print(f"Tracker loop error: {e}")
            time.sleep(60)

if __name__ == '__main__':
    init_db()
    
    # Start telegram listener in background thread
    t = threading.Thread(target=telegram_polling_loop, daemon=True)
    t.start()
    
    # Run tracker in main thread
    tracker_loop()
