import json
import datetime
import os
import requests
from db_manager import (
    init_db,
    get_all_known_stock,
    update_inventory_state,
    record_sale,
    get_daily_sales_summary,
    parse_sale_query_ist,
    get_sales_summary_for_range,
    get_last_update_id,
    set_last_update_id
)
from shopify_scraper import harvest_storefront_token, fetch_current_inventory

CONFIG_FILE = 'config.json'

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def send_telegram_message(bot_token, chat_id, text, parse_mode='HTML'):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Safely handle Telegram 4096-char limit by chunking on newlines if needed
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] if len(text) > max_len else [text]
    for chunk in chunks:
        payload = {'chat_id': chat_id, 'text': chunk, 'parse_mode': parse_mode}
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

def get_sale_help_text():
    return (
        "📖 <b>SALES REPORT COMMAND GUIDE (IST)</b> 📖\n\n"
        "• <code>/sale</code> — Today's sales (since 12:00 AM IST)\n"
        "• <code>/sale yesterday</code> — Yesterday's full sales\n"
        "• <code>/sale 25-10-2026</code> — Sales on a specific date\n"
        "• <code>/sale 01-10-2026 to 26-10-2026</code> — Custom date range\n"
        "• <code>/sale 7d</code> or <code>/sale 30d</code> — Last 7 or 30 days\n"
        "• <code>/sale month</code> — Current month-to-date\n"
        "• <code>/sale lastmonth</code> — Previous full month\n"
        "• <code>/sale all</code> — All-time total sales\n"
    )

def handle_sale_command(bot_token, chat_id, raw_text='/sale'):
    start_utc, end_utc, label, is_multi_day, err = parse_sale_query_ist(raw_text)
    if err:
        if err == "HELP":
            send_telegram_message(bot_token, chat_id, get_sale_help_text())
        else:
            send_telegram_message(bot_token, chat_id, f"⚠️ {err}\n\n" + get_sale_help_text())
        return

    store_summaries, top_products, daily_breakdown = get_sales_summary_for_range(start_utc, end_utc)
    msg = f"📊 <b>SALES REPORT — {label}</b> 📊\n\n"
    total_rev = 0.0
    total_items = 0

    if not store_summaries:
        msg += "No sales recorded for this period.\n"
    else:
        for store, items, rev in store_summaries:
            msg += f"🏪 <b>{store}</b>: {items} items sold | ₹{rev:,.2f}\n"
            total_rev += rev
            total_items += items

        msg += f"\n💰 <b>TOTAL REVENUE:</b> ₹{total_rev:,.2f}\n"
        msg += f"📦 <b>TOTAL ITEMS SOLD:</b> {total_items}\n"

        if is_multi_day and daily_breakdown:
            msg += f"\n📅 <b>DAILY BREAKDOWN (IST):</b>\n"
            for ist_date, d_qty, d_rev in daily_breakdown[:15]:
                msg += f"• <b>{ist_date}</b>: {d_qty} items — ₹{d_rev:,.2f}\n"
            if len(daily_breakdown) > 15:
                msg += f"<i>...and {len(daily_breakdown) - 15} earlier days</i>\n"

        if top_products:
            msg += "\n🏆 <b>TOP SELLING ITEMS:</b>\n"
            for domain, p_title, v_title, qty, rev in top_products:
                v_text = f" ({v_title})" if v_title and v_title != 'Default Title' else ""
                msg += f"• [{domain.split('.')[0].capitalize()}] {p_title}{v_text} - {qty} sold - ₹{rev:,.2f}\n"

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
                text = (msg.get('text') or '').strip()
                if text.lower().startswith('/sale'):
                    print(f"Received command '{text}', generating report...")
                    handle_sale_command(bot_token, chat_id, text)
            
            if max_update_id and max_update_id != offset:
                set_last_update_id(max_update_id)
    except Exception as e:
        print(f"Telegram checking error: {e}")

def run_tracker(config, bot_token, chat_id):
    domains = config.get('target_domains', [])
    for domain in domains:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Scanning {domain}...")
        try:
            token = harvest_storefront_token(domain)
            if not token:
                print(f"Could not find token for {domain}. Skipping.")
                continue
                
            known_stock = get_all_known_stock(domain)
            current_inventory = fetch_current_inventory(domain, token)
            if not current_inventory:
                print(f"No inventory returned for {domain}. Skipping update.")
                continue
            
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
        except Exception as e:
            print(f"Error scanning domain {domain}: {e}")

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
