#!/usr/bin/env python3
"""
Multi-store TikTok Shop order sync.

Default behavior (run from the baseline root):
    python tkorders/tiktokorders.py
    python -m tkorders.tiktokorders

This syncs every configured shop (TU / VB), fetches new orders, stores them
in shared MySQL tables `tkorders` / `tkorders_items`, then runs backfill.

Useful CLI:
    python tkorders/tiktokorders.py --store TU
    python tkorders/tiktokorders.py --store VB
    python tkorders/tiktokorders.py --backfill --store TU
    python tkorders/tiktokorders.py --refresh <order_id> --store TU
    python tkorders/tiktokorders.py --history --from 2026-10-01 --to 2026-10-30 --store TU
    python tkorders/tiktokorders.py --webhook-worker

Credentials, signing and call_api() live in tkapi/client.py.

Optional .env:
    TTS_INITIAL_LOOKBACK_SECONDS=3600
    TTS_POLL_INTERVAL_SECONDS=300
    TTS_LOOP_FOREVER=false
    TTS_WEBHOOK_BACKFILL_SECONDS=300
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------
# Project paths (baseline root must come first on sys.path)
# ---------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tkapi.client import (
    SHOPS,
    TikTokAPIError,
    call_api,
    resolve_shop,
    selected_shops,
    shop_is_configured,
)
from tkorders import tkorders_db as db


INITIAL_LOOKBACK_SECONDS = int(
    os.environ.get("TTS_INITIAL_LOOKBACK_SECONDS", "3600")
)

# ---------------------------------------------------------
# TikTok order API
# ---------------------------------------------------------
def search_new_orders(
    shop: dict,
    create_time_ge: int,
    create_time_le: int,
    page_size: int = 50,
):
    path = "/order/202309/orders/search"
    orders = []
    page_token = ""

    while True:
        query_params = {
            "page_size": page_size,
            "sort_field": "create_time",
            "sort_order": "ASC",
        }

        if page_token:
            query_params["page_token"] = page_token

        body = {
            "create_time_ge": create_time_ge,
            "create_time_le": create_time_le,
        }

        data = call_api(
            shop,
            "POST",
            path,
            query_params,
            body,
        )

        orders.extend(data.get("orders", []))
        page_token = data.get("next_page_token", "")

        if not page_token:
            break

    return orders


def iter_order_pages(shop: dict, start_ts: int, end_ts: int, page_size: int = 100):
    """Stream UTC-day search pages, stopping at the date boundary if ASC order holds.

    TikTok has been observed returning next_page_token even after the requested
    date range. We still yield the boundary page so in-range orders are saved.
    Early stop assumes the API honors its requested ascending create_time sort.
    """
    page_token = ""
    seen_tokens = set()
    last_create_time = None
    page_number = 0
    sorting_reliable = True
    while True:
        page_number += 1
        params = {"page_size": page_size, "sort_field": "create_time", "sort_order": "ASC"}
        if page_token:
            params["page_token"] = page_token
        data = call_api(shop, "POST", "/order/202309/orders/search", params,
                        {"create_time_ge": start_ts, "create_time_le": end_ts})
        orders = data.get("orders", []) or []
        past_end = False
        for order in orders:
            try:
                created = int(order["create_time"])
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(f"Search result missing valid create_time: {order.get('id')}")
            if last_create_time is not None and created < last_create_time:
                if sorting_reliable:
                    print(f"[{shop['short_name']}] page={page_number}: ASC ordering violated; "
                          "early stop disabled for this run", flush=True)
                sorting_reliable = False
            last_create_time = created
            if created > end_ts:
                past_end = True

        yield orders

        # Stop only after yielding the boundary page. If the observed sort
        # order breaks, fall back to reading all pages with date filtering.
        if past_end and sorting_reliable:
            print(f"[{shop['short_name']}] page={page_number}: UTC date boundary reached; "
                  "stopping pagination early", flush=True)
            return

        next_token = data.get("next_page_token") or ""
        if not next_token:
            return
        if next_token in seen_tokens:
            raise RuntimeError("TikTok returned a repeated page token")
        seen_tokens.add(next_token)
        page_token = next_token


def history_import(conn, shop: dict, start_date, end_date):
    """UTC calendar-day windows, inclusive dates. Restart unfinished day safely."""
    if end_date < start_date:
        raise ValueError("--to must not precede --from")
    current = start_date
    while current <= end_date:
        day = current.isoformat()
        if db.history_day_done(conn, shop["store_id"], day):
            print(f"[{shop['short_name']}] {day}: already completed", flush=True)
            current += timedelta(days=1)
            continue
        start_ts = int(datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        end_ts = start_ts + 86400 - 1
        pages = imported = skipped = 0
        try:
            for results in iter_order_pages(shop, start_ts, end_ts):
                pages += 1
                # The API may continue pagination beyond the requested date window.
                # Enforce the UTC window locally before fetching order details.
                valid_results = []
                outside_count = 0
                for order in results:
                    try:
                        created = int(order["create_time"])
                    except (KeyError, TypeError, ValueError):
                        raise RuntimeError(f"Search result missing valid create_time: {order.get('id')}")
                    if start_ts <= created <= end_ts:
                        valid_results.append(order)
                    else:
                        outside_count += 1
                if outside_count:
                    print(f"[{shop['short_name']}] {day} page={pages}: ignored {outside_count} outside-date orders", flush=True)
                ids = list(dict.fromkeys(str(o["id"]) for o in valid_results if o.get("id")))
                existing = db.existing_order_ids(conn, shop["store_id"], ids)
                missing = [oid for oid in ids if oid not in existing]
                skipped += len(ids) - len(missing)
                if missing:
                    details = get_order_detail(shop, missing, save_debug=False)
                    received = {str(o.get("id")) for o in details}
                    absent = set(missing) - received
                    if absent:
                        raise RuntimeError(f"Detail API omitted {len(absent)} order(s): {sorted(absent)[:3]}")
                    for order in details:
                        try:
                            detail_ts = int(order["create_time"])
                        except (KeyError, TypeError, ValueError):
                            raise RuntimeError(f"Detail missing valid create_time: {order.get('id')}")
                        if not start_ts <= detail_ts <= end_ts:
                            raise RuntimeError(f"Detail outside requested UTC date: {order.get('id')}")
                        if db.insert_historical_if_absent(conn, shop["store_id"], order,
                                                           extract_line_items(order)):
                            imported += 1
                    conn.commit()
                print(f"[{shop['short_name']}] {day} page={pages} new={imported} existing={skipped}", flush=True)
            db.mark_history_day_done(conn, shop["store_id"], day, imported, skipped)
            conn.commit()
            print(f"[{shop['short_name']}] {day} COMPLETE new={imported} existing={skipped}", flush=True)
        except Exception:
            conn.rollback()
            raise
        current += timedelta(days=1)


def get_order_detail(shop: dict, order_ids: list[str], save_debug: bool = True):
    """Fetch order details; split failed batches without losing any order IDs."""
    path = "/order/202309/orders"

    def fetch_batch(batch):
        try:
            data = call_api(shop, "GET", path, {"ids": ",".join(batch)})
        except TikTokAPIError as exc:
            # A persistent server-side error for 50 IDs may be caused by a
            # single problematic order. Split recursively until isolated.
            if (exc.status_code in (429, 500, 502, 503, 504)
                    or exc.api_code == 36009003) and len(batch) > 1:
                middle = len(batch) // 2
                print(f"[{shop['short_name']}] Detail batch of {len(batch)} failed "
                      f"after retries; splitting into {middle} + {len(batch)-middle}",
                      flush=True)
                return fetch_batch(batch[:middle]) + fetch_batch(batch[middle:])
            print(f"[{shop['short_name']}] Detail request still failed "
                  f"for {len(batch)} order(s); IDs: {', '.join(batch[:3])}", flush=True)
            raise

        if save_debug:
            output_file = SCRIPT_DIR / f"order_detail_raw_{shop['short_name']}.json"
            output_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return data.get("orders", [])

    all_orders = []
    for i in range(0, len(order_ids), 50):
        all_orders.extend(fetch_batch(order_ids[i:i + 50]))
    return all_orders


def _sales_tax(item: dict) -> tuple:
    for tax in item.get("item_tax") or []:
        if not isinstance(tax, dict):
            continue
        if tax.get("tax_type") == "SALES_TAX":
            return (
                tax.get("tax_amount"),
                tax.get("tax_rate"),
            )
    return (None, None)


def extract_line_items(order: dict) -> list[dict]:
    result = []

    for item in order.get("line_items", []) or []:
        tax_amount, tax_rate = _sales_tax(item)

        result.append({
            "line_item_id": item.get("id"),
            "product_id": item.get("product_id"),
            "product_name": item.get("product_name"),
            "sku_id": item.get("sku_id"),
            "sku_name": item.get("sku_name"),
            "seller_sku": item.get("seller_sku"),
            "sku_type": item.get("sku_type"),
            "quantity": item.get("quantity") or 1,
            "currency": item.get("currency"),
            "original_price": item.get("original_price"),
            "sale_price": item.get("sale_price"),
            "platform_discount": item.get("platform_discount"),
            "seller_discount": item.get("seller_discount"),
            "sales_tax_amount": tax_amount,
            "sales_tax_rate": tax_rate,
            "room_id": item.get("room_id"),
            "display_status": item.get("display_status"),
            "is_gift": item.get("is_gift"),
            "is_dangerous_good": item.get("is_dangerous_good"),
            "is_pod_customized": item.get("is_pod_customized"),
            "tracking_number": item.get("tracking_number"),
        })

    return result


def _print_order_line(shop: dict, order: dict):
    skus = ", ".join(
        (li["sku_name"] or "")
        for li in extract_line_items(order)
    )
    print(
        f"[{shop['short_name']}] "
        f"{order.get('id')} | {order.get('status')} | SKU: {skus}"
    )


def store_orders(
    conn,
    shop: dict,
    orders: list[dict],
    is_refresh: bool,
):
    for order in orders:
        db.upsert_order(
            conn,
            store_id=shop["store_id"],
            order=order,
            line_items=extract_line_items(order),
            is_refresh=is_refresh,
        )


# ---------------------------------------------------------
# Sync / backfill
# ---------------------------------------------------------
def poll_once(conn, shop: dict):
    now = int(time.time())
    last_check = db.get_last_check(conn, shop["store_id"])

    if last_check is None:
        last_check = now - INITIAL_LOOKBACK_SECONDS

    print(
        f"[{shop['short_name']}] Checking new orders "
        f"between {last_check} and {now}..."
    )

    orders = search_new_orders(
        shop,
        last_check,
        now,
    )

    if not orders:
        print(f"[{shop['short_name']}] No new orders")
        db.set_last_check(conn, shop["store_id"], now)
        conn.commit()
        return []

    order_ids = [o["id"] for o in orders]

    print(
        f"[{shop['short_name']}] Found {len(order_ids)} new order(s), "
        f"fetching details..."
    )

    details = get_order_detail(shop, order_ids)

    store_orders(
        conn,
        shop,
        details,
        is_refresh=False,
    )

    # Only advance this shop's watermark after order writes succeed.
    db.set_last_check(conn, shop["store_id"], now)
    conn.commit()

    for order in details:
        _print_order_line(shop, order)

    return details


def run_backfill(conn, shop: dict):
    order_ids = db.get_orders_needing_refresh(
        conn,
        shop["store_id"],
    )

    if not order_ids:
        print(f"[{shop['short_name']}] Backfill: nothing pending")
        return

    print(
        f"[{shop['short_name']}] Backfill: re-checking "
        f"{len(order_ids)} order(s)..."
    )

    details = get_order_detail(shop, order_ids)
    store_orders(
        conn,
        shop,
        details,
        is_refresh=True,
    )
    conn.commit()

    for order in details:
        _print_order_line(shop, order)

    stuck = db.get_stuck_orders(
        conn,
        shop["store_id"],
    )

    if stuck:
        print(
            f"[{shop['short_name']}] Backfill: "
            f"{len(stuck)} order(s) still incomplete after "
            f"{db.MAX_REFRESH_ATTEMPTS} attempts."
        )


def manual_refresh(
    conn,
    shop: dict,
    order_id: str,
):
    print(
        f"[{shop['short_name']}] Refreshing {order_id}..."
    )

    details = get_order_detail(shop, [order_id])

    if not details:
        print(
            f"[{shop['short_name']}] No such order returned: "
            f"{order_id}"
        )
        return

    store_orders(
        conn,
        shop,
        details,
        is_refresh=True,
    )
    conn.commit()

    row = db.get_order_summary(
        conn,
        shop["store_id"],
        order_id,
    )

    if row:
        print(
            f"[{shop['short_name']}] "
            f"order_id={row['order_id']} "
            f"status={row['status']} "
            f"buyer_nickname={row['buyer_nickname']} "
            f"recipient={row['recipient_name']} "
            f"attempts={row['refresh_attempts']}"
        )


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
def webhook_order_id(event):
    """Extract order ID from recognized TikTok Shop order events."""
    event_type = str(event.get('type') or event.get('event_type') or '').upper()

    allowed_types = {
        '1',                    # TikTok Shop order status change
        'ORDER_STATUS_CHANGE',
        'ORDER_CREATE',
        'ORDER_CREATED',
    }

    if event_type not in allowed_types:
        return None

    data = event.get('data') or {}
    if not isinstance(data, dict):
        return None

    order_id = data.get('order_id')
    return str(order_id) if order_id else None


def webhook_status_update(event):
    """Return status and timestamp only when the event supplies both.

    Never guess an event timestamp: a status-only event cannot be safely
    ordered against an already-saved order update.
    """
    data = event.get('data') or {}
    if not isinstance(data, dict):
        return None
    status = data.get('order_status') or data.get('status')
    timestamp = data.get('update_time') or data.get('update_timestamp')
    if not status or timestamp is None:
        return None
    try:
        timestamp = int(timestamp)
        if timestamp > 10**12:  # milliseconds
            timestamp //= 1000
        if timestamp <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return str(status), timestamp


def package_event_order_ids(event):
    """Return distinct order IDs linked to a Type 4 package notification."""
    if str(event.get('type') or event.get('event_type') or '').upper() not in ('4', 'PACKAGE_UPDATE'):
        return None
    data = event.get('data') or {}
    if not isinstance(data, dict):
        raise ValueError('Package event data is not an object')
    packages = data.get('package_list') or []
    if not isinstance(packages, list):
        raise ValueError('Package event package_list is not a list')
    ids = []
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError('Package event contains invalid package')
        order_ids = package.get('order_id_list') or []
        if not isinstance(order_ids, list):
            raise ValueError('Package event order_id_list is not a list')
        ids.extend(str(oid) for oid in order_ids if oid)
    return list(dict.fromkeys(ids))


def sync_package_tracking(conn, shop, order_ids):
    """Update tracking per order; isolate unavailable package-linked IDs.

    A 21008111 response is not proof of deletion. Never delete local data.
    Successful orders are committed individually so retries are idempotent.
    """
    changed = 0
    skipped = 0
    for oid in dict.fromkeys(str(value) for value in order_ids if value):
        try:
            details = get_order_detail(shop, [oid], save_debug=False)
        except TikTokAPIError as exc:
            if str(exc.api_code) == '21008111':
                existing = db.get_order_summary(conn, shop['store_id'], oid)
                print(f"[{shop['short_name']}] Package order {oid}: "
                      f"API 21008111, local={'EXISTS' if existing else 'NOT_FOUND'}; "
                      "retained existing data, skipped tracking refresh", flush=True)
                skipped += 1
                continue
            raise

        matching = [order for order in details if str(order.get('id')) == oid]
        if len(matching) != 1:
            raise RuntimeError(f'Package detail missing/ambiguous order: {oid}')

        cur = conn.cursor()
        try:
            for item in extract_line_items(matching[0]):
                tracking = item.get('tracking_number')
                line_id = item.get('line_item_id')
                if not tracking or not line_id:
                    continue
                cur.execute("""UPDATE tkorders_items
                    SET tracking_number=%s
                    WHERE store_id=%s AND order_id=%s AND line_item_id=%s
                      AND (tracking_number IS NULL OR tracking_number<>%s)""",
                    (tracking, shop['store_id'], oid, str(line_id), tracking))
                changed += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    if skipped:
        print(f"[{shop['short_name']}] Package update: "
              f"skipped_unavailable_orders={skipped}", flush=True)
    return changed


def run_webhook_worker():
    """Process durable inbox without searching for new orders on a schedule."""
    conn = db.get_connection()
    db.init_schema(conn)
    db.init_webhook_schema(conn)
    print('Webhook worker started (Ctrl+C to stop)', flush=True)
    # Backfill only when the inbox is idle; no separate polling service needed.
    # Set TTS_WEBHOOK_BACKFILL_SECONDS=0 to disable.
    backfill_interval = max(0, int(os.environ.get('TTS_WEBHOOK_BACKFILL_SECONDS', '300')))
    next_backfill = time.monotonic() + backfill_interval
    try:
        while True:
            row = db.claim_webhook(conn)
            if not row:
                if backfill_interval and time.monotonic() >= next_backfill:
                    next_backfill = time.monotonic() + backfill_interval
                    for shop in SHOPS.values():
                        if not shop_is_configured(shop):
                            continue
                        try:
                            run_backfill(conn, shop)
                        except Exception as exc:
                            conn.rollback()
                            print(f"[{shop['short_name']}] Webhook idle backfill failed: {exc}", flush=True)
                    next_backfill = time.monotonic() + backfill_interval
                time.sleep(2)
                continue
            try:
                event = row['payload']
                if isinstance(event, str):
                    event = json.loads(event)
                package_ids = package_event_order_ids(event)
                if package_ids is not None:
                    shop = SHOPS[row['store_id']]
                    changed = sync_package_tracking(conn, shop, package_ids)
                    print(f"[{shop['short_name']}] Package update: "
                          f"orders={len(package_ids)} tracking_rows_updated={changed}", flush=True)
                    db.finish_webhook(conn, row['id'], 'DONE')
                    continue
                order_id = webhook_order_id(event)
                if not order_id:
                    db.finish_webhook(conn, row['id'], 'IGNORED')
                    continue
                shop = SHOPS[row['store_id']]
                # Type 1: fetch the latest complete order on every notification.
                # This also syncs tracking when the order moves to AWAITING_COLLECTION.
                details = get_order_detail(shop, [order_id], save_debug=False)
                matching = [order for order in details if str(order.get('id')) == order_id]
                if len(matching) != 1:
                    raise RuntimeError(f'Webhook detail missing/ambiguous order: {order_id}')
                order = matching[0]

                # Do not let an older API snapshot regress a newer stored order.
                cur = conn.cursor(dictionary=True)
                try:
                    cur.execute(
                        "SELECT update_time FROM tkorders WHERE store_id=%s AND order_id=%s",
                        (shop['store_id'], order_id))
                    stored = cur.fetchone()
                finally:
                    cur.close()
                incoming_time = db.unix_to_utc_datetime(order.get('update_time'))
                if (stored and stored['update_time'] is not None and incoming_time is not None
                        and incoming_time < stored['update_time']):
                    print(f"[{shop['short_name']}] Webhook {order_id}: "
                          "stale API snapshot; skipped", flush=True)
                else:
                    store_orders(conn, shop, [order], is_refresh=False)
                    conn.commit()
                    print(f"[{shop['short_name']}] Webhook full sync {order_id}: "
                          f"status={order.get('status')}", flush=True)
                db.finish_webhook(conn, row['id'], 'DONE')
            except Exception as exc:
                conn.rollback()
                delay = min(3600, 30 * 2 ** min(row['attempts'], 7))
                db.finish_webhook(conn, row['id'], 'FAILED', exc, delay)
                print(f"Webhook {row['id']} failed: {exc}", flush=True)
    finally:
        conn.close()


def main():
    args = sys.argv[1:]
    if "--webhook-worker" in args:
        run_webhook_worker()
        return

    try:
        shops = selected_shops(args)
    except ValueError as exc:
        print(exc)
        return

    if not shops:
        print(
            "No TikTok shops are configured. "
            "Add TU/VB token + shop_cipher values to .env."
        )
        return

    for shop in shops:
        if not shop_is_configured(shop):
            print(
                f"[{shop['short_name']}] Missing APP_KEY / APP_SECRET / ACCESS_TOKEN / SHOP_CIPHER."
            )
            return

    if "--history" in args:
        parser = argparse.ArgumentParser(description="Historical TikTok order import (UTC dates)")
        parser.add_argument("--history", action="store_true")
        parser.add_argument("--store", choices=["TU", "VB", "1", "2"])
        parser.add_argument("--from", dest="start", required=True, type=lambda x: datetime.strptime(x, "%Y-%m-%d").date())
        parser.add_argument("--to", dest="end", required=True, type=lambda x: datetime.strptime(x, "%Y-%m-%d").date())
        opts = parser.parse_args(args)
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            db.init_history_schema(conn)
            for shop in shops:
                history_import(conn, shop, opts.start, opts.end)
        finally:
            conn.close()
        return

    conn = db.get_connection()
    db.init_schema(conn)

    try:
        if "--refresh" in args:
            if "--store" not in args:
                print(
                    "With multiple shops, --refresh requires --store.\n"
                    "Example: python tiktokorders.py --refresh 123456 --store TU"
                )
                return

            idx = args.index("--refresh")
            if idx + 1 >= len(args):
                print(
                    "Usage: python tiktokorders.py "
                    "--refresh <order_id> --store TU|VB"
                )
                return

            manual_refresh(
                conn,
                shops[0],
                args[idx + 1],
            )
            return

        if "--backfill" in args:
            for shop in shops:
                run_backfill(conn, shop)
            return

        poll_interval = int(
            os.environ.get("TTS_POLL_INTERVAL_SECONDS", "300")
        )
        loop_forever = (
            os.environ.get("TTS_LOOP_FOREVER", "false").lower()
            == "true"
        )

        if not loop_forever:
            for shop in shops:
                poll_once(conn, shop)
                run_backfill(conn, shop)
            return

        names = ", ".join(s["short_name"] for s in shops)
        print(
            f"Starting continuous polling for {names} — "
            f"every {poll_interval}s. Press Ctrl+C to stop."
        )

        while True:
            for shop in shops:
                try:
                    poll_once(conn, shop)
                    run_backfill(conn, shop)
                except Exception as exc:
                    conn.rollback()
                    print(
                        f"[{shop['short_name']}] Error during this check; "
                        f"will retry next round: {exc}"
                    )

            time.sleep(poll_interval)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
