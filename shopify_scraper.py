import time
import base64
import requests
import re
from urllib.parse import urlparse

FALLBACK_TOKENS = {
    'dulhanjewels.com': '2a836df2b82846963a38b5ef407d6018',
    'kanshijewels.com': '9a3eed2b46c608cf9357de80d28fac47',
    'muskanjewel.com': 'ece39a0ad683230d3ffd3737c38c3145',
    'daivik.in': '2d20ed5b79d467bbbc827f43162d6f26'
}

def clean_string(val):
    if not isinstance(val, str):
        return val
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', val)

def get_fallback_token(domain):
    clean_domain = domain.lower().replace('www.', '')
    return FALLBACK_TOKENS.get(clean_domain)

def harvest_storefront_token(domain):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for host in (f"https://www.{domain}/", f"https://{domain}/"):
        try:
            r = requests.get(host, headers=headers, timeout=15)
            if r.status_code == 200 and r.text:
                html = r.text
                tokens = re.findall(r'"storefrontAccessToken"\s*:\s*"([a-f0-9]{32})"', html)
                tokens_js = re.findall(r'storefrontAccessToken\s*:\s*["\']([a-f0-9]{32})["\']', html)
                tokens_raw = re.findall(r'accessToken["\']?\s*:\s*["\']([a-f0-9]{32})["\']', html, re.IGNORECASE)
                found = tokens + tokens_js + tokens_raw
                if found:
                    return found[0]
        except Exception as e:
            print(f"[{domain}] Token harvest error on {host}: {e}")

    return get_fallback_token(domain)

def post_graphql_query(url, headers, payload, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=25)
            if r.status_code == 429:
                wait = 5 * attempt
                print(f"  [GraphQL] HTTP 429 rate limited (attempt {attempt}/{max_retries}), sleeping {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    time.sleep(3 * attempt)
                    continue
                errors = data.get('errors', [])
                if errors:
                    throttled = any(err.get('extensions', {}).get('code') == 'THROTTLED' for err in errors)
                    if throttled:
                        wait = 6 * attempt
                        print(f"  [GraphQL] Throttled (attempt {attempt}/{max_retries}), sleeping {wait}s...")
                        time.sleep(wait)
                        continue
                    if not data.get('data'):
                        wait = 4 * attempt
                        print(f"  [GraphQL] Error without data (attempt {attempt}/{max_retries}): {errors[:1]}, sleeping {wait}s...")
                        time.sleep(wait)
                        continue
                if data.get('data') is not None:
                    return data
                time.sleep(3 * attempt)
                continue
            else:
                wait = 4 * attempt
                print(f"  [GraphQL] HTTP {r.status_code} (attempt {attempt}/{max_retries}), sleeping {wait}s...")
                time.sleep(wait)
        except Exception as e:
            wait = 3 * attempt
            print(f"  [GraphQL] Request exception (attempt {attempt}/{max_retries}): {e}, sleeping {wait}s...")
            time.sleep(wait)
    return None

def verify_single_variant(url, headers, global_id):
    """
    Verifies a single variant that was marked availableForSale=True in catalog pagination
    but was omitted or returned <= 0 in a batch cartCreate call.
    Returns:
      > 0 : Verified clamped stock quantity
        0 : Confirmed availableForSale is now False (genuinely sold out)
       -1 : Could not verify exact stock while still availableForSale=True (skip updating to prevent false sale)
    """
    check_query = """
    query checkVariant($id: ID!) {
      node(id: $id) {
        ... on ProductVariant {
          id
          availableForSale
        }
      }
    }
    """
    data = post_graphql_query(url, headers, {'query': check_query, 'variables': {'id': global_id}}, max_retries=3)
    if not data or not isinstance(data, dict):
        return -1
    node = (data.get('data') or {}).get('node')
    if not node:
        return -1
    if not node.get('availableForSale', True):
        return 0

    single_cart_mutation = """
    mutation cartCreate($input: CartInput!) {
      cartCreate(input: $input) {
        cart {
          lines(first: 10) {
            edges {
              node {
                quantity
                merchandise { ... on ProductVariant { id } }
              }
            }
          }
        }
      }
    }
    """
    cart_resp = post_graphql_query(
        url,
        headers,
        {'query': single_cart_mutation, 'variables': {'input': {'lines': [{'merchandiseId': global_id, 'quantity': 9999}]}}},
        max_retries=3
    )
    if cart_resp and isinstance(cart_resp, dict):
        cart_data = ((cart_resp.get('data') or {}).get('cartCreate') or {}).get('cart') or {}
        for edge in (cart_data.get('lines') or {}).get('edges', []):
            n = edge.get('node') or {}
            merch = n.get('merchandise') or {}
            if merch.get('id') == global_id:
                qty = n.get('quantity', -1)
                if qty > 0:
                    return qty
    return -1

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
    page_num = 0

    while has_next:
        page_num += 1
        payload = {'query': query, 'variables': {"cursor": cursor}}
        data = post_graphql_query(url, headers, payload)

        # If page 1 fails with harvested token, try fallback token if different
        if (not data or not isinstance(data, dict) or not (data.get('data') or {}).get('products')) and page_num == 1:
            fb_token = get_fallback_token(domain)
            if fb_token and fb_token != token:
                print(f"[{domain}] Primary token failed on page 1, retrying with fallback token...")
                headers['X-Shopify-Storefront-Access-Token'] = fb_token
                data = post_graphql_query(url, headers, payload)

        if not data or not isinstance(data, dict):
            print(f"[{domain}] Failed to fetch catalog page {page_num} (cursor={cursor}). Aborting domain scan to prevent partial catalog.")
            return None

        data_obj = data.get('data') or {}
        products_conn = data_obj.get('products')
        if not products_conn or not isinstance(products_conn, dict):
            print(f"[{domain}] Missing products object on page {page_num}. Aborting domain scan to prevent partial catalog.")
            return None

        for edge in products_conn.get('edges', []):
            node = edge['node']
            p_title = node['title']
            p_handle = node['handle']

            image_url = None
            img_edges = node.get('images', {}).get('edges', [])
            if img_edges:
                image_url = img_edges[0]['node']['url']

            for v_edge in node.get('variants', {}).get('edges', []):
                v_node = v_edge['node']
                global_id = v_node['id']
                try:
                    decoded = base64.b64decode(global_id).decode('utf-8')
                    variant_id = str(int(decoded.split('/')[-1]))
                except Exception:
                    variant_id = global_id
                numeric_vid = str(global_id).split('/')[-1]

                v_info = {
                    'product_title': p_title,
                    'product_handle': p_handle,
                    'variant_id': variant_id,
                    'global_id': global_id,
                    'variant_title': v_node['title'],
                    'sku': v_node['sku'],
                    'price': float(v_node['price']['amount']),
                    'url': f"https://www.{domain}/products/{p_handle}?variant={numeric_vid}",
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
    batch_size = 50
    for i in range(0, len(active_variants), batch_size):
        batch = active_variants[i:i + batch_size]
        lines = [{'merchandiseId': v['global_id'], 'quantity': 9999} for v in batch]

        data = post_graphql_query(url, headers, {'query': mutation, 'variables': {"input": {"lines": lines}}})
        if data and isinstance(data, dict):
            data_obj = data.get('data') or {}
            cart_create = data_obj.get('cartCreate') or {}
            cart_data = cart_create.get('cart') or {}
            if cart_data:
                quantities = {}
                for edge in cart_data.get('lines', {}).get('edges', []):
                    n = edge.get('node') or {}
                    merch = n.get('merchandise') or {}
                    if merch.get('id'):
                        quantities[merch['id']] = n.get('quantity', -1)
                for v in batch:
                    qty = quantities.get(v['global_id'], -1)
                    stock_results[v['variant_id']] = qty if qty > 0 else -1
            else:
                for v in batch:
                    stock_results[v['variant_id']] = -1
        else:
            for v in batch:
                stock_results[v['variant_id']] = -1
        time.sleep(1.2)

    # Verify any active_variants that were omitted from batch cartCreate or returned <= 0
    unverified = [v for v in active_variants if stock_results.get(v['variant_id'], -1) <= 0]
    if unverified:
        print(f"[{domain}] Verifying {len(unverified)} active variant(s) individually...")
        for v in unverified[:25]:
            verified_qty = verify_single_variant(url, headers, v['global_id'])
            stock_results[v['variant_id']] = verified_qty
            time.sleep(0.4)

    current_inventory = {}
    resolved_active = 0
    for v in active_variants:
        stock = stock_results.get(v['variant_id'], -1)
        if stock >= 0:
            v['stock'] = stock
            current_inventory[v['variant_id']] = v
            if stock > 0:
                resolved_active += 1

    if len(active_variants) > 0 and resolved_active == 0:
        print(f"[{domain}] Cart clamping resolved 0/{len(active_variants)} active variants. Aborting domain scan.")
        return None

    for v in out_of_stock_variants:
        v['stock'] = 0
        current_inventory[v['variant_id']] = v

    for k in current_inventory:
        current_inventory[k]['product_title'] = clean_string(current_inventory[k]['product_title'])
        current_inventory[k]['variant_title'] = clean_string(current_inventory[k]['variant_title'])

    print(f"[{domain}] Scan complete: {resolved_active}/{len(active_variants)} active variants clamped, {len(out_of_stock_variants)} out-of-stock variants.")
    return current_inventory
