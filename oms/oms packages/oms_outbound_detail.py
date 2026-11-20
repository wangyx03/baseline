#!/usr/bin/env python3
"""Show what is inside an OMS parcel outbound order, straight from Lingxing OMS.

Run from the baseline root:
    python "oms/oms packages/oms_outbound_detail.py" 9234690393549009416095    # tracking number
    python "oms/oms packages/oms_outbound_detail.py" OBS52726111613R           # OMS order number
    python "oms/oms packages/oms_outbound_detail.py" OBS52726111613R --raw     # full JSON response

A tracking number is looked up in oms_outbound_orders (run the sync first);
every OMS order with that tracking is shown.
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]   # baseline/
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
except ImportError:
    pass

from api.omsapi.client import OmsClient   # noqa: E402

DETAIL_PATH = '/v1/outboundOrder/detail'


def fetch_details(client, order_nos):
    """Detail records for OMS order numbers in one call (request field: outboundOrderNoList)."""
    detail = client._post(DETAIL_PATH, {'outboundOrderNoList': list(order_nos)})
    return detail if isinstance(detail, list) else [detail] if detail else []


def oms_orders_for(ref):
    """OMS order numbers for a tracking number (from oms_outbound_orders), or the ref itself."""
    if ref.upper().startswith('OBS'):
        return [ref]
    from db import get_db
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT oms_order_no FROM oms_outbound_orders "
                    "WHERE tracking_number = %s AND oms_order_no NOT LIKE 'PUSH:%%' "
                    "ORDER BY oms_create_time", (ref.strip().upper(),))
        return [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Show the books in an OMS parcel outbound order')
    parser.add_argument('ref', help='tracking number or OMS order number (OBS...)')
    parser.add_argument('--raw', action='store_true', help='print the full JSON response')
    opts = parser.parse_args()

    orders = oms_orders_for(opts.ref)
    if not orders:
        print(f'No OMS order found for {opts.ref} in oms_outbound_orders. Run the OMS sync first, '
              'or pass the OMS order number (OBS...).')
        return

    client = OmsClient(os.environ.get('OMS_APP_KEY', ''), os.environ.get('OMS_APP_SECRET', ''))
    records = fetch_details(client, orders)
    if opts.raw:
        print(json.dumps(records, ensure_ascii=False, indent=2, default=str))
        return
    if not records:
        print(f'OMS returned nothing for {", ".join(orders)}.')
        return

    for r in records:
        print(f"\n{r.get('outboundOrderNo')}  {r.get('statusName') or r.get('status')}  "
              f"tracking={r.get('logisticsTrackNo')}  warehouse={r.get('whCode')}")
        if (r.get('exceptionDesc') or '').strip():
            print(f"  异常: {r['exceptionDesc'].strip()}")
        products = r.get('productList') or []
        total = 0
        for p in products:
            qty = int(p.get('quantity') or 0)
            total += qty
            stock = p.get('availableAmount')
            flag = '  <-- not enough stock' if stock is not None and stock < qty else ''
            print(f"  {p.get('sku'):<15} x{qty:<3} stock {str(stock):>4}  "
                  f"{(p.get('productName') or '')[:60]}{flag}")
        cost = (f", cost {r.get('costTotal')} {r.get('costCurrencyCode') or ''}"
                if r.get('costTotal') is not None else '')
        print(f"  {len(products)} titles, {total} books{cost}")


if __name__ == '__main__':
    main()
