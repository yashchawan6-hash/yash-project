import json
import datetime
import os
import requests
from db_manager import init_db, get_all_known_stock, update_inventory_state, record_sale, get_daily_sales_summary, get_last_update_id, set_last_update_id
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

def handle_sale_command(bot_token, chat_id):
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

def check_telegram_commands(bot_token, chat_id):
    """Fetch updates once to see if /sale was typed since the last run."""
    offset = get_last_update_id()
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {'timeout': 5}
    if offset:
        params['offset'] = offset
        
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            updates = data.get('result', [])
            max_update_id = offset
            
            for update in updates:
                update_id = update['update_id']
                if not max_update_id or update_id >= max_update_id:
                    max_update_id = update_id + 1
                    
                msg = update.get('message', {})
                text = msg.get('text', '')
                if text.startswith('/sale'):
                    print(f"Received /sale command, generating report...")
                    handle_sale_command(bot_token, chat_id)
            
            if max_update_id and max_update_id != offset:
                set_last_update_id(max_update_id)
    except Exception as e:
        print(f"Telegram checking error: {e}")

def run_tracker(config, bot_token, chat_id):
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
            
            update_inventory_state(domain, item)

if __name__ == '__main__':
    # Initialize DB (creates file if not exists)
    init_db()
    
    # Read secrets from Environment Variables (set by GitHub Actions)
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    if not bot_token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables!")
        exit(1)
        
    config = load_config()
    
    print("Checking for pending Telegram commands...")
    check_telegram_commands(bot_token, chat_id)
    
    print("Running inventory scraper...")
    run_tracker(config, bot_token, chat_id)
    print("Cron execution completed.")
