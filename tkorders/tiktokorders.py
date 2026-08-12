#!/usr/bin/env python3
"""
tiktok_order_sync.py

Poll TikTok Shop for new orders, fetch full order details, and store them in MySQL.
Also runs a backfill pass each cycle to fill in buyer info (~1hr delay) and
tracking info (~1-3 day delay) on orders that didn't have them yet at capture time.
See db.py's module docstring for the full design rationale.

Flow:
1. Poll the Order Search API, filtering by create_time since the last check (stored
   in last_check.json, next to this script — see STATE_FILE below)
2. Batch-call the Order Detail API to get full order info
3. Upsert each order + its line_items into MySQL in one transaction per batch,
   advancing last_check only after the write commits (so a crash mid-batch
   doesn't lose data — see db.py's upsert_order docstring)
4. Separately, run a backfill pass: query orders still missing buyer info or
   tracking info, re-fetch just those, and update them (with backoff + a
   retry cap so permanently-stuck orders don't get hammered forever)

CLI:
  python tiktokorders.py                 # run once (poll + backfill) and exit
  python tiktokorders.py --backfill      # run only the backfill pass, once
  python tiktokorders.py --refresh <id>  # force-refresh one order immediately,
                                          #   bypassing the backfill's retry cap/backoff

Before running you need:
1. A TikTok Shop Partner Center App -> APP_KEY / APP_SECRET
2. Completed OAuth authorization -> ACCESS_TOKEN / SHOP_CIPHER
   (access_token expires; for long-running use, add refresh_token auto-renewal —
   not implemented in this version)
3. Fill in all values via a .env file next to this script — never hardcode
   secrets in code that gets committed to git. Also needs DB_HOST / DB_PORT /
   DB_USER / DB_PASSWORD / DB_NAME — see db.py's module docstring.

Note: sign_request() implements TikTok Shop's public signing algorithm
(sorted params + path + body, wrapped with app_secret, HMAC-SHA256).
Before relying on this in production, verify against the Partner Center's
API Testing Tool with the same parameters to confirm signatures match —
some endpoint versions handle POST bodies slightly differently.

Also unverified against a live response (flagged in db.py too — check a real
order's raw JSON before trusting these blindly):
- exact sub-field names inside recipient_address
- exact cancelled-status string (CANCELLED vs something else)
- whether tracking_number lives at order level, package level, or both
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import db

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)  # .env lives one level up, in the repo root (shared with inventory_snapshot.py etc), not inside tkorders/ itself

# ---------------------- Config ----------------------
CONFIG = {
    "app_key": os.environ.get("TTS_APP_KEY", "YOUR_APP_KEY"),
    "app_secret": os.environ.get("TTS_APP_SECRET", "YOUR_APP_SECRET"),
    "access_token": os.environ.get("TTS_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN"),
    "refresh_token": os.environ.get("TTS_REFRESH_TOKEN", ""),
    "shop_cipher": os.environ.get("TTS_SHOP_CIPHER", "YOUR_SHOP_CIPHER"),
    "base_url": "https://open-api.tiktokglobalshop.com",
    "token_refresh_url": "https://auth.tiktok-shops.com/api/v2/token/refresh",
}

# How many seconds to look back on the very first run (before last_check.json exists).
# Default 3600 = 1 hour. To look back 7 days, set TTS_INITIAL_LOOKBACK_SECONDS=604800 in .env
INITIAL_LOOKBACK_SECONDS = int(os.environ.get("TTS_INITIAL_LOOKBACK_SECONDS", "3600"))

STATE_FILE = Path(__file__).parent / "last_check.json"  # tracks the last polled timestamp — NOT in MySQL, see db.py's docstring for the trade-off


def load_last_check() -> int:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())["last_check"]
    return int(time.time()) - INITIAL_LOOKBACK_SECONDS


def save_last_check(ts: int):
    STATE_FILE.write_text(json.dumps({"last_check": ts}))


# ---------------------- Signing ----------------------
def sign_request(path: str, params: dict, body_str: str = "") -> str:
    """
    1. Drop sign / access_token, sort remaining params by key
    2. Concatenate as key1value1key2value2...
    3. Prepend path; for POST requests, append the raw request body
    4. Wrap with app_secret, then HMAC-SHA256 using app_secret as the key
    """
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    param_str = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    base_str = f"{path}{param_str}{body_str}"
    base_str = f"{CONFIG['app_secret']}{base_str}{CONFIG['app_secret']}"
    return hmac.new(
        CONFIG["app_secret"].encode("utf-8"),
        base_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_common_params(extra: dict) -> dict:
    params = {
        "app_key": CONFIG["app_key"],
        "timestamp": int(time.time()),
        "shop_cipher": CONFIG["shop_cipher"],
    }
    params.update(extra)
    return params


def update_env_file(updates: dict):
    """Rewrite specific KEY=value lines in .env in place, preserving everything
    else (comments, ordering, unrelated keys). Appends the key if it's not
    already present. Used to persist a refreshed access_token/refresh_token
    so they survive a process restart, not just this run."""
    if not ENV_PATH.exists():
        print(f"Warning: {ENV_PATH} not found, can't persist refreshed tokens to disk")
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        new_lines.append(line)

    for key, value in updates.items():
        if key not in written:
            new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def refresh_access_token():
    """Exchange the current refresh_token for a new access_token. TikTok
    ROTATES the refresh_token on every use too — the old one won't work for
    the next refresh — so both get updated here, in memory and in .env."""
    print("Access token expired — refreshing via refresh_token...")
    params = {
        "app_key": CONFIG["app_key"],
        "app_secret": CONFIG["app_secret"],
        "refresh_token": CONFIG["refresh_token"],
        "grant_type": "refresh_token",
    }
    resp = requests.get(CONFIG["token_refresh_url"], params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"Token refresh failed — refresh_token may itself be expired/revoked, "
            f"in which case you'll need to redo the OAuth flow via oauth_callback.py: {data}"
        )

    token_data = data["data"]
    CONFIG["access_token"] = token_data["access_token"]
    CONFIG["refresh_token"] = token_data["refresh_token"]

    update_env_file({
        "TTS_ACCESS_TOKEN": token_data["access_token"],
        "TTS_REFRESH_TOKEN": token_data["refresh_token"],
    })
    print("Access token refreshed and saved to .env")


def call_api(method: str, path: str, params: dict, body: dict = None, _retried: bool = False):
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    all_params = build_common_params(params)
    all_params["sign"] = sign_request(path, all_params, body_str)
    all_params["access_token"] = CONFIG["access_token"]  # TikTok's own generated cURL sends this as a query param too

    url = f"{CONFIG['base_url']}{path}"
    headers = {
        "x-tts-access-token": CONFIG["access_token"],
        "Content-Type": "application/json",
    }

    if method == "GET":
        resp = requests.get(url, params=all_params, headers=headers, timeout=15)
    else:
        # Send the exact same body string we signed — letting requests re-serialize
        # the dict itself can produce different bytes and break the signature.
        resp = requests.post(url, params=all_params, headers=headers, data=body_str.encode("utf-8"), timeout=15)

    if not resp.ok:
        # raise_for_status() below discards the response body, which is where
        # TikTok puts the actual error code/message — print it first so we can tell
        # an expired access_token apart from a bad signature, wrong shop_cipher, etc.
        print(f"TikTok API error response body: {resp.text}")

        if resp.status_code == 401 and not _retried:
            try:
                error_code = resp.json().get("code")
            except ValueError:
                error_code = None
            if error_code == 105002:  # "Expired credentials"
                refresh_access_token()
                return call_api(method, path, params, body, _retried=True)

    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok API error: {data}")
    return data["data"]


# ---------------------- Business logic ----------------------
def search_new_orders(create_time_ge: int, create_time_le: int, page_size: int = 50):
    """Fetch new orders by creation time, returns a list of order summaries (incl. id)"""
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

        data = call_api("POST", path, query_params, body)
        orders.extend(data.get("orders", []))
        page_token = data.get("next_page_token", "")
        if not page_token:
            break
    return orders


def get_order_detail(order_ids: list):
    """Batch-fetch order details (incl. sku_name / seller_sku in line_items), max 50 ids per call"""
    path = "/order/202309/orders"
    all_orders = []
    batch_size = 50
    for i in range(0, len(order_ids), batch_size):
        batch = order_ids[i:i + batch_size]
        params = {"ids": ",".join(batch)}
        data = call_api("GET", path, params)
        all_orders.extend(data.get("orders", []))
    return all_orders


def extract_line_items(order: dict) -> list:
    return [
        {
            "product_name": item.get("product_name"),
            "sku_id": item.get("sku_id"),
            "sku_name": item.get("sku_name"),
            "seller_sku": item.get("seller_sku"),
            "sale_price": item.get("sale_price"),
            "quantity": item.get("quantity") or 1,
        }
        for item in order.get("line_items", [])
    ]


def _print_order_line(order: dict):
    line_items = extract_line_items(order)
    skus = ", ".join(li["sku_name"] or "" for li in line_items)
    print(f"- {order.get('id')} | {order.get('status')} | SKU: {skus}")


def store_orders(conn, orders: list, is_refresh: bool):
    """Upsert a batch of full order-detail dicts into MySQL, all in one transaction."""
    for order in orders:
        db.upsert_order(conn, order, extract_line_items(order), is_refresh=is_refresh)


def poll_once(conn):
    """Fetch anything new since the last watermark and write it to MySQL. The
    watermark itself lives in last_check.json (not the DB) — see db.py's
    module docstring for the trade-off this implies."""
    now = int(time.time())
    last_check = load_last_check()

    print(f"Checking for new orders between {last_check} and {now}...")
    orders = search_new_orders(last_check, now)

    if not orders:
        print("No new orders")
        save_last_check(now)
        return []

    order_ids = [o["id"] for o in orders]
    print(f"Found {len(order_ids)} new order(s), fetching details...")
    details = get_order_detail(order_ids)

    store_orders(conn, details, is_refresh=False)
    conn.commit()
    save_last_check(now)

    for order in details:
        _print_order_line(order)
    return details


def run_backfill(conn):
    """Re-fetch orders still missing buyer info and/or tracking info (subject to
    the retry cap + backoff in db.py), and update just those rows."""
    order_ids = db.get_orders_needing_refresh(conn)
    if not order_ids:
        print("Backfill: nothing pending")
        return

    print(f"Backfill: re-checking {len(order_ids)} order(s) for buyer/tracking info...")
    details = get_order_detail(order_ids)
    store_orders(conn, details, is_refresh=True)
    conn.commit()

    for order in details:
        _print_order_line(order)

    stuck = db.get_stuck_orders(conn)
    if stuck:
        print(f"Backfill: {len(stuck)} order(s) still incomplete after {db.MAX_REFRESH_ATTEMPTS} attempts — worth a manual look:")
        for row in stuck:
            missing = []
            if not row["buyer_email"] and not row["recipient_name"]:
                missing.append("buyer info")
            if not row["tracking_number"]:
                missing.append("tracking")
            print(f"  - {row['order_id']} | status={row['status']} | missing: {', '.join(missing)}")


def manual_refresh(conn, order_id: str):
    """Force-refresh one order right now, bypassing the backfill's retry cap and backoff."""
    print(f"Refreshing {order_id}...")
    details = get_order_detail([order_id])
    if not details:
        print(f"No such order returned by the API: {order_id}")
        return

    store_orders(conn, details, is_refresh=True)
    conn.commit()

    row = db.get_order_summary(conn, order_id)
    if row:
        buyer_info = "captured" if (row["buyer_email"] or row["recipient_name"]) else "still missing"
        tracking = "captured" if row["tracking_number"] else "still missing"
        print(
            f"order_id={row['order_id']} status={row['status']} "
            f"buyer_info={buyer_info} tracking={tracking} "
            f"recipient={row['recipient_name']} tracking_number={row['tracking_number']} "
            f"attempts={row['refresh_attempts']}"
        )


def main():
    args = sys.argv[1:]
    conn = db.get_connection()
    db.init_schema(conn)

    try:
        if "--refresh" in args:
            idx = args.index("--refresh")
            if idx + 1 >= len(args):
                print("Usage: python tiktokorders.py --refresh <order_id>")
                return
            manual_refresh(conn, args[idx + 1])
            return

        if "--backfill" in args:
            run_backfill(conn)
            return

        poll_interval = int(os.environ.get("TTS_POLL_INTERVAL_SECONDS", "300"))
        loop_forever = os.environ.get("TTS_LOOP_FOREVER", "false").lower() == "true"

        if not loop_forever:
            poll_once(conn)
            run_backfill(conn)
            return

        print(f"Starting continuous polling — checking every {poll_interval}s. Press Ctrl+C to stop.")
        while True:
            try:
                poll_once(conn)
                run_backfill(conn)
            except Exception as e:
                conn.rollback()
                print(f"Error during this check, skipping and continuing next round: {e}")
            time.sleep(poll_interval)
    finally:
        conn.close()


if __name__ == "__main__":
    main()