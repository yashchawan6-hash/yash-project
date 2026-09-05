import time
import base64
import requests
import re
from urllib.parse import urlparse

def clean_string(val):
    if not isinstance(val, str): return val
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', val)

def harvest_storefront_token(domain):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        r = requests.get(f"https://www.{domain}/", headers=headers, timeout=15)
        html = r.text
        tokens = re.findall(r'"storefrontAccessToken":"([a-f0-9]{32})"', html)
        tokens_js = re.findall(r'storefrontAccessToken\s*:\s*["\']([a-f0-9]{32})["\']', html)
        tokens_raw = re.findall(r'accessToken["\']?\s*:\s*["\']([a-f0-9]{32})["\']', html, re.IGNORECASE)
        found = tokens + tokens_js + tokens_raw
        if found:
            return found[0]
    except Exception as e:
        print(f"[{domain}] Token harvest error: {e}")

    # Fallbacks
    fallbacks = {
        'dulhanjewels.com': '2a836df2b82846963a38b5ef407d6018',
        'kanshijewels.com': '9a3eed2b46c608cf9357de80d28fac47',
        'daivik.in': '2d20ed5b79d467bbbc827f43162d6f26'
    }
    clean_domain = domain.lower().replace('www.', '')
    return fallbacks.get(clean_domain)

def post_graphql_query(url, headers, payload, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=25)
            if r.status_code == 429:
                time.sleep(6 * attempt)
                continue
            if r.status_code == 200:
                data = r.json()
                errors = data.get('errors', [])
                throttled = any(err.get('extensions', {}).get('code') == 'THROTTLED' for err in errors)
                if throttled:
                    time.sleep(7 * attempt)
                    continue
                return data
        except Exception:
            time.sleep(3)
    return None

def fetch_current_inventory(domain, token):
    url = f"https://{domain}/api/2023-07/graphql.json"
    headers = {
        'X-Shopify-Storefront-Access-Token': token,
        'Content-Type': 'application/json'
    }
    
    query = """
    query getAllProducts($cursor: String) {
      products(first: 250, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            title handle
            images(first: 1) { edges { node { url } } }
            variants(first: 100) {
              edges { node { id title sku price { amount } availableForSale } }
            }
          }
        }
      }
    }
    """
    
    active_variants = []
    out_of_stock_variants = []
    cursor = None
    has_next = True
    
    while has_next:
        payload = {'query': query, 'variables': {"cursor": cursor}}
        data = post_graphql_query(url, headers, payload)
        if not data: break
            
        products_conn = data.get('data', {}).get('products', {})
        if not products_conn: break
            
        for edge in products_conn.get('edges', []):
            node = edge['node']
            p_title = node['title']
            p_handle = node['handle']
            
            image_url = None
            img_edges = node.get('images', {}).get('edges', [])
            if img_edges: image_url = img_edges[0]['node']['url']
            
            for v_edge in node.get('variants', {}).get('edges', []):
                v_node = v_edge['node']
                global_id = v_node['id']
                try:
                    decoded = base64.b64decode(global_id).decode('utf-8')
                    variant_id = str(int(decoded.split('/')[-1]))
                except:
                    variant_id = global_id
                    
                v_info = {
                    'product_title': p_title,
                    'product_handle': p_handle,
                    'variant_id': variant_id,
                    'global_id': global_id,
                    'variant_title': v_node['title'],
                    'sku': v_node['sku'],
                    'price': float(v_node['price']['amount']),
                    'url': f"https://www.{domain}/products/{p_handle}?variant={variant_id}",
                    'image_url': image_url
                }
                
                if v_node.get('availableForSale', False):
                    active_variants.append(v_info)
                else:
                    v_info['stock'] = 0
                    out_of_stock_variants.append(v_info)
                    
        page_info = products_conn.get('pageInfo', {})
        has_next = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor', None)
        
    mutation = """
    mutation cartCreate($input: CartInput!) {
      cartCreate(input: $input) {
        cart { lines(first: 250) { edges { node { quantity merchandise { ... on ProductVariant { id } } } } } }
      }
    }
    """
    
    stock_results = {}
    batch_size = 90
    for i in range(0, len(active_variants), batch_size):
        batch = active_variants[i:i+batch_size]
        lines = [{'merchandiseId': v['global_id'], 'quantity': 9999} for v in batch]
        
        data = post_graphql_query(url, headers, {'query': mutation, 'variables': {"input": {"lines": lines}}})
        if data:
            cart_data = data.get('data', {}).get('cartCreate', {}).get('cart', {})
            if cart_data:
                quantities = {}
                for edge in cart_data.get('lines', {}).get('edges', []):
                    n = edge['node']
                    quantities[n['merchandise']['id']] = n['quantity']
                for v in batch:
                    stock_results[v['variant_id']] = quantities.get(v['global_id'], 0)
            else:
                for v in batch: stock_results[v['variant_id']] = -1
        else:
            for v in batch: stock_results[v['variant_id']] = -1
        time.sleep(1.5)
        
    current_inventory = {}
    for v in active_variants:
        stock = stock_results.get(v['variant_id'], 0)
        if stock > 0:
            v['stock'] = stock
            current_inventory[v['variant_id']] = v
            
    for v in out_of_stock_variants:
        v['stock'] = 0
        current_inventory[v['variant_id']] = v
        
    for k in current_inventory:
        current_inventory[k]['product_title'] = clean_string(current_inventory[k]['product_title'])
        current_inventory[k]['variant_title'] = clean_string(current_inventory[k]['variant_title'])
        
    return current_inventory
