#!/usr/bin/env python3
"""
TikTok Shop shipping documents (labels / packing slips) by package.

API: GET /fulfillment/202309/packages/{package_id}/shipping_documents
     scope: seller.fulfillment.basic (Fulfillment Basic)
The API returns a temporary doc_url; download the PDF right away, don't store the URL.

Package IDs come from tkorders_items.package_id (filled by tiktokorders.py).

Test from the baseline root (read-only, nothing is written to the DB):
    python tk/tkorders/shipping_docs.py --store VB --tracking 9234690394227203142628
    python tk/tkorders/shipping_docs.py --store VB --order 577711344705049074
    python tk/tkorders/shipping_docs.py --store VB --package 1158061721751688105
Options:
    --type label | slip | both | picture   (default: label and both, one file each)
    --size A6 | A5                          (default A6)
    --out  DIR                              (default /tmp/tk_labels)
"""
import argparse
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parents[1]   # baseline/ (this file is in tk/tkorders/)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from api.tkapi.client import TikTokAPIError, call_api, resolve_shop, shop_is_configured
from tk.tkorders import tkorders_db as db

DOC_TYPES = {
    'label': 'SHIPPING_LABEL',
    'slip': 'PACKING_SLIP',
    'both': 'SHIPPING_LABEL_AND_PACKING_SLIP',
    'picture': 'SHIPPING_LABEL_PICTURE',
}


def get_document_url(shop, package_id, doc_type='SHIPPING_LABEL', size='A6', fmt='PDF'):
    """-> {'doc_url': ..., 'tracking_number': ...} for one package."""
    params = {'document_type': doc_type}
    if doc_type != 'SHIPPING_LABEL_PICTURE':
        params['document_size'] = size
        params['document_format'] = fmt
    data = call_api(shop, 'GET', f'/fulfillment/202309/packages/{package_id}/shipping_documents', params)
    return {'doc_url': data.get('doc_url'), 'tracking_number': data.get('tracking_number')}


def download(url, path):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    Path(path).write_bytes(resp.content)
    return len(resp.content)


def package_ids_for(conn, store_id, tracking=None, order_id=None):
    """Distinct package_ids from tkorders_items for a tracking number or an order."""
    cur = conn.cursor()
    try:
        if tracking:
            cur.execute("""SELECT DISTINCT package_id FROM tkorders_items
                WHERE store_id=%s AND TRIM(tracking_number)=%s AND package_id IS NOT NULL""",
                (store_id, tracking.strip()))
        else:
            cur.execute("""SELECT DISTINCT package_id FROM tkorders_items
                WHERE store_id=%s AND order_id=%s AND package_id IS NOT NULL""",
                (store_id, str(order_id).strip()))
        return [str(r[0]) for r in cur.fetchall()]
    finally:
        cur.close()


def main():
    p = argparse.ArgumentParser(description='Download TikTok shipping label / packing slip PDFs (test).')
    p.add_argument('--store', required=True, help='TU | VB')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--tracking')
    g.add_argument('--order')
    g.add_argument('--package')
    p.add_argument('--type', choices=list(DOC_TYPES), action='append',
                   help='repeatable; default: label and both')
    p.add_argument('--size', default='A6', choices=['A6', 'A5'])
    p.add_argument('--out', default='/tmp/tk_labels')
    a = p.parse_args()

    shop = resolve_shop(a.store)
    if not shop_is_configured(shop):
        sys.exit(f"[{shop['short_name']}] Missing APP_KEY / APP_SECRET / ACCESS_TOKEN / SHOP_CIPHER in .env")

    if a.package:
        packages = [a.package.strip()]
    else:
        conn = db.get_connection()
        try:
            packages = package_ids_for(conn, shop['store_id'], a.tracking, a.order)
        finally:
            conn.close()
        if not packages:
            sys.exit('No package_id found in tkorders_items (no tracking yet, or not backfilled).')

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for package_id in packages:
        for key in (a.type or ['label', 'both']):
            doc_type = DOC_TYPES[key]
            try:
                doc = get_document_url(shop, package_id, doc_type, a.size)
            except TikTokAPIError as exc:
                failed += 1
                print(f'FAIL  package {package_id} {doc_type}: {exc}')
                continue
            if not doc['doc_url']:
                failed += 1
                print(f'FAIL  package {package_id} {doc_type}: no doc_url returned')
                continue
            ext = 'png' if key == 'picture' else 'pdf'
            path = out / f"{shop['short_name']}_{doc['tracking_number'] or package_id}_{key}.{ext}"
            size = download(doc['doc_url'], path)
            print(f"OK    package {package_id} tracking {doc['tracking_number']} {doc_type}"
                  f" -> {path} ({size:,} bytes)")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
