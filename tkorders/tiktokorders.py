#!/usr/bin/env python3
"""
Multi-store TikTok Shop order sync.

Default behavior:
    python tiktokorders.py

This syncs every configured shop (TU / VB), fetches new orders, stores them
in shared MySQL tables `tkorders` / `tkorders_items`, then runs backfill.

Useful CLI:
    python tiktokorders.py --store TU
    python tiktokorders.py --store VB
    python tiktokorders.py --backfill --store TU
    python tiktokorders.py --refresh <order_id> --store TU

Required .env keys:
    TTS_TU_APP_KEY=...
    TTS_TU_APP_SECRET=...
    TTS_TU_ACCESS_TOKEN=...
    TTS_TU_REFRESH_TOKEN=...
    TTS_TU_SHOP_CIPHER=...

    TTS_VB_APP_KEY=...
    TTS_VB_APP_SECRET=...
    TTS_VB_ACCESS_TOKEN=...
    TTS_VB_REFRESH_TOKEN=...
    TTS_VB_SHOP_CIPHER=...

Optional:
    TTS_INITIAL_LOOKBACK_SECONDS=3600
    TTS_POLL_INTERVAL_SECONDS=300
    TTS_LOOP_FOREVER=false
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

# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH, override=True)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import tkorders_db as db


# ---------------------------------------------------------
# Per-store app config
# ---------------------------------------------------------
BASE_URL = "https://open-api.tiktokglobalshop.com"
TOKEN_REFRESH_URL = "https://auth.tiktok-shops.com/api/v2/token/refresh"

INITIAL_LOOKBACK_SECONDS = int(
    os.environ.get("TTS_INITIAL_LOOKBACK_SECONDS", "3600")
)


def _shop_config(store_id: int, short_name: str) -> dict:
    prefix = f"TTS_{short_name}_"
    return {
        "store_id": store_id,
        "short_name": short_name,
        "app_key": os.environ.get(prefix + "APP_KEY", "").strip(),
        "app_secret": os.environ.get(prefix + "APP_SECRET", "").strip(),
        "access_token": os.environ.get(prefix + "ACCESS_TOKEN", "").strip(),
        "refresh_token": os.environ.get(prefix + "REFRESH_TOKEN", "").strip(),
        "shop_cipher": os.environ.get(prefix + "SHOP_CIPHER", "").strip(),
        "env_prefix": prefix,
    }


SHOPS = {
    1: _shop_config(1, "TU"),
    2: _shop_config(2, "VB"),
}


def shop_is_configured(shop: dict) -> bool:
    return bool(
        shop.get("app_key")
        and shop.get("app_secret")
        and shop.get("access_token")
        and shop.get("shop_cipher")
    )


def resolve_shop(value: str) -> dict:
    value = str(value).strip().upper()

    for shop in SHOPS.values():
        if value in {str(shop["store_id"]), shop["short_name"].upper()}:
            return shop

    raise ValueError(f"Unknown store: {value}. Use TU, VB, 1, or 2.")


def selected_shops(args: list[str]) -> list[dict]:
    if "--store" in args:
        idx = args.index("--store")
        if idx + 1 >= len(args):
            raise ValueError("Usage: --store TU|VB|1|2")
        return [resolve_shop(args[idx + 1])]

    return [shop for shop in SHOPS.values() if shop_is_configured(shop)]


# ---------------------------------------------------------
# Signing / API
# ---------------------------------------------------------
def sign_request(shop: dict, path: str, params: dict, body_str: str = "") -> str:
    filtered = {
        k: v for k, v in params.items()
        if k not in ("sign", "access_token")
    }
    param_str = "".join(
        f"{k}{v}" for k, v in sorted(filtered.items())
    )
    app_secret = shop["app_secret"]
    base_str = f"{app_secret}{path}{param_str}{body_str}{app_secret}"

    return hmac.new(
        app_secret.encode("utf-8"),
        base_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_common_params(shop: dict, extra: dict) -> dict:
    params = {
        "app_key": shop["app_key"],
        "timestamp": int(time.time()),
        "shop_cipher": shop["shop_cipher"],
    }
    params.update(extra)
    return params


def update_env_file(updates: dict):
    if not ENV_PATH.exists():
        print(f"Warning: {ENV_PATH} not found; refreshed token was not persisted.")
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

    ENV_PATH.write_text(
        "\n".join(new_lines) + "\n",
        encoding="utf-8",
    )


def refresh_access_token(shop: dict):
    if not shop.get("refresh_token"):
        raise RuntimeError(
            f"{shop['short_name']}: access token expired but no refresh token is configured."
        )

    print(f"[{shop['short_name']}] Access token expired — refreshing...")

    params = {
        "app_key": shop["app_key"],
        "app_secret": shop["app_secret"],
        "refresh_token": shop["refresh_token"],
        "grant_type": "refresh_token",
    }

    resp = requests.get(
        TOKEN_REFRESH_URL,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"{shop['short_name']}: token refresh failed: {data}"
        )

    token_data = data["data"]
    shop["access_token"] = token_data["access_token"]
    shop["refresh_token"] = token_data["refresh_token"]

    prefix = shop["env_prefix"]
    update_env_file({
        prefix + "ACCESS_TOKEN": token_data["access_token"],
        prefix + "REFRESH_TOKEN": token_data["refresh_token"],
    })

    print(f"[{shop['short_name']}] Token refreshed and saved to .env")


def call_api(
    shop: dict,
    method: str,
    path: str,
    params: dict,
    body: dict | None = None,
    _retried: bool = False,
):
    body_str = json.dumps(
        body,
        separators=(",", ":"),
    ) if body else ""

    all_params = build_common_params(shop, params)
    all_params["sign"] = sign_request(shop, path, all_params, body_str)
    all_params["access_token"] = shop["access_token"]

    url = f"{BASE_URL}{path}"
    headers = {
        "x-tts-access-token": shop["access_token"],
        "Content-Type": "application/json",
    }

    if method == "GET":
        resp = requests.get(
            url,
            params=all_params,
            headers=headers,
            timeout=15,
        )
    else:
        resp = requests.post(
            url,
            params=all_params,
            headers=headers,
            data=body_str.encode("utf-8"),
            timeout=15,
        )

    if not resp.ok:
        print(
            f"[{shop['short_name']}] TikTok API error response body: "
            f"{resp.text}"
        )

        if resp.status_code == 401 and not _retried:
            try:
                error_code = resp.json().get("code")
            except ValueError:
                error_code = None

            if error_code == 105002:
                refresh_access_token(shop)
                return call_api(
                    shop,
                    method,
                    path,
                    params,
                    body,
                    _retried=True,
                )

    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"[{shop['short_name']}] TikTok API error: {data}"
        )

    return data["data"]


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


def get_order_detail(shop: dict, order_ids: list[str]):
    path = "/order/202309/orders"
    all_orders = []
    batch_size = 50

    for i in range(0, len(order_ids), batch_size):
        batch = order_ids[i:i + batch_size]
        params = {"ids": ",".join(batch)}

        data = call_api(shop, "GET", path, params)

        # Debug copy. Separate file per store so TU/VB do not overwrite each other.
        output_file = (
            SCRIPT_DIR
            / f"order_detail_raw_{shop['short_name']}.json"
        )
        output_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        all_orders.extend(data.get("orders", []))

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
def main():
    args = sys.argv[1:]

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
