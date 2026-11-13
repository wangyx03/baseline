#!/usr/bin/env python3
"""Sync TikTok Shop product catalog for TU/VB without touching order services.

Run (from the baseline root):
     python tkproducts/tkproducts.py --store ALL
     python tkproducts/tkproducts.py --store TU
     python tkproducts/tkproducts.py --store VB
     python tkproducts/tkproducts.py --webhook-worker
  or python -m tkproducts.tkproducts ...
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tkapi import client as api
from tkproducts import tkproducts_db as db

SEARCH_PATH = '/product/202502/products/search'

def sync_shop(conn, shop):
    page_token = ''
    seen = set()
    pages = products = skus = 0
    while True:
        params = {'page_size': 100}
        if page_token:
            params['page_token'] = page_token
        data = api.call_api(shop, 'POST', SEARCH_PATH, params, {'status': 'ALL', 'locale': 'en-US'})
        batch = data.get('products') or []
        for product in batch:
            if not product.get('id'):
                raise RuntimeError('Product response missing id; stopping before committing this page')
            skus += db.save_product(conn, shop['store_id'], product)
            products += 1
        conn.commit()
        pages += 1
        print(f"[{shop['short_name']}] page={pages} products={products} skus={skus}", flush=True)
        next_token = data.get('next_page_token') or ''
        if not next_token:
            break
        if next_token in seen:
            raise RuntimeError('Repeated TikTok page token; aborting pagination')
        seen.add(next_token)
        page_token = next_token
        time.sleep(0.15)
    db.mark_sync(conn, shop['store_id'], products, skus)
    conn.commit()
    return products, skus

def fetch_product(shop, product_id):
    # Product detail endpoint, using the same signed/token-refreshing API client.
    path = f'/product/202309/products/{product_id}'
    data = api.call_api(shop, 'GET', path, {})
    product = data.get('product') if isinstance(data,dict) else None
    if product is None and isinstance(data,dict) and data.get('id'):
        product = data
    if not isinstance(product,dict) or str(product.get('id')) != str(product_id):
        raise RuntimeError(f'Product detail missing or mismatched: {product_id}')
    return product

def run_worker():
    conn=db.get_connection()
    db.init_schema(conn)
    print('Product webhook worker started',flush=True)
    try:
        while True:
            row=db.claim_webhook(conn)
            if not row:
                time.sleep(2)
                continue
            try:
                event=row['payload']
                if isinstance(event,str): event=json.loads(event)
                product_id=row['product_id']
                if not product_id:
                    db.finish_webhook(conn,row['id'],'IGNORED')
                    continue
                shop=api.SHOPS[row['store_id']]
                product=fetch_product(shop,product_id)
                db.save_product(conn,shop['store_id'],product)
                conn.commit()
                db.finish_webhook(conn,row['id'],'DONE')
                print(f"[{shop['short_name']}] product {product_id} updated",flush=True)
            except Exception as exc:
                conn.rollback()
                delay=min(3600,30*2**min(row['attempts'],7))
                db.finish_webhook(conn,row['id'],'FAILED',exc,delay)
                print(f"Product webhook {row['id']} failed: {exc}",flush=True)
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description='TikTok Shop product catalog sync')
    parser.add_argument('--webhook-worker', action='store_true')
    parser.add_argument('--store', choices=['ALL','TU','VB'], default='ALL')
    args = parser.parse_args()
    if args.webhook_worker:
        run_worker()
        return
    shops = [api.resolve_shop(args.store)] if args.store != 'ALL' else list(api.SHOPS.values())
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        for shop in shops:
            if not api.shop_is_configured(shop):
                print(f"[{shop['short_name']}] Skipped: credentials not configured", flush=True)
                continue
            try:
                sync_shop(conn, shop)
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

if __name__ == '__main__':
    main()
