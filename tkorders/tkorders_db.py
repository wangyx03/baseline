#!/usr/bin/env python3
"""
TikTok Orders database layer.

This file contains TikTok-order-specific schema and SQL only.
The actual MySQL connection comes from the shared baseline/db.py pool.

Expected layout:

    baseline/
        db.py
        .env
        tkorders/
            tiktokorders.py
            tkorders_db.py
            get_shop_cipher.py
            oauth_callback.py
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# =========================================================
# Import shared baseline/db.py
# =========================================================

BASE_DIR = Path(__file__).resolve().parent.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db


# =========================================================
# Config
# =========================================================

MAX_REFRESH_ATTEMPTS = 20

BACKOFF_FAST_ATTEMPTS = 5
BACKOFF_FAST_MINUTES = 25

BACKOFF_SLOW_HOURS = 24

CANCELLED_STATUSES = {
    "CANCELLED",
    "CANCELED",
}


# =========================================================
# Safe table names
# =========================================================

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table_name(env_var: str, default: str) -> str:
    name = os.environ.get(env_var, default)

    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{env_var}={name!r} is not a valid table name"
        )

    return name


ORDERS_TABLE = _safe_table_name(
    "TKORDERS_TABLE",
    "tkorders_tu",
)

ITEMS_TABLE = _safe_table_name(
    "TKORDERS_ITEMS_TABLE",
    "tkorders_items_tu",
)


# =========================================================
# Shared connection wrapper
# =========================================================

def get_connection():
    """
    Return a pooled MySQL connection from baseline/db.py.

    tiktokorders.py historically calls db.get_connection(), so this
    compatibility wrapper lets the rest of that script stay unchanged.
    """
    return get_db()


# =========================================================
# Time helpers
# =========================================================

def unix_to_utc_datetime(value):
    if value is None or value == "":
        return None

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).replace(tzinfo=None)


# =========================================================
# Schema
# =========================================================

CREATE_ORDERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {ORDERS_TABLE} (
    order_id              VARCHAR(64) PRIMARY KEY,
    status                VARCHAR(64),
    create_time           DATETIME,
    update_time           DATETIME,
    warehouse_id          VARCHAR(64),
    warehouse_name        VARCHAR(255),
    shipping_type         VARCHAR(64),
    delivery_option_name  VARCHAR(128),
    rts_sla_time          DATETIME,
    auto_cancel_time      DATETIME,
    buyer_email           VARCHAR(255),
    buyer_message         TEXT,
    recipient_name        VARCHAR(255),
    recipient_phone       VARCHAR(64),
    recipient_address     TEXT,
    recipient_country     VARCHAR(100),
    recipient_state       VARCHAR(100),
    recipient_county      VARCHAR(100),
    recipient_city        VARCHAR(100),
    recipient_postal_code VARCHAR(30),
    recipient_address_line1 VARCHAR(255),
    recipient_address_line2 VARCHAR(255),
    delivery_instruction  TEXT,
    tracking_number       VARCHAR(128),
    shipping_provider     VARCHAR(128),
    shipping_provider_id  VARCHAR(64),
    refresh_attempts      INT NOT NULL DEFAULT 0,
    synced_at             DATETIME
                          DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_create_time (create_time)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
"""

CREATE_ITEMS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id        VARCHAR(64) NOT NULL,
    product_name    VARCHAR(255),
    sku_id          VARCHAR(64),
    sku_name        VARCHAR(64),
    seller_sku      VARCHAR(128),
    sale_price      VARCHAR(32),
    quantity        INT,
    FOREIGN KEY (order_id)
        REFERENCES {ORDERS_TABLE}(order_id)
        ON DELETE CASCADE,
    INDEX idx_order_id (order_id),
    INDEX idx_seller_sku (seller_sku)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
"""


def init_schema(conn):
    cur = conn.cursor()
    try:
        cur.execute(CREATE_ORDERS_TABLE)
        cur.execute(CREATE_ITEMS_TABLE)
        conn.commit()
    finally:
        cur.close()


# =========================================================
# Upsert order
# =========================================================

def upsert_order(
    conn,
    order: dict,
    line_items: list,
    is_refresh: bool,
):
    addr = order.get("recipient_address") or {}

    if isinstance(addr, dict):
        recipient_name = addr.get("name")
        recipient_phone = addr.get("phone_number")
        recipient_address_text = (
            addr.get("full_address")
            or addr.get("address_detail")
            or (str(addr) if addr else None)
        )
    else:
        recipient_name = None
        recipient_phone = None
        recipient_address_text = str(addr) if addr else None

    # Read structured address fields directly from TikTok API.
    recipient_country = None
    recipient_state = None
    recipient_county = None
    recipient_city = None
    recipient_postal_code = None
    recipient_address_line1 = None
    recipient_address_line2 = None
    delivery_instruction = None

    if isinstance(addr, dict):
        recipient_postal_code = addr.get("postal_code")
        recipient_address_line1 = addr.get("address_line1")
        recipient_address_line2 = addr.get("address_line2")

        # TikTok has used both district_info and district_info_list naming
        # across address payload/document versions. Read either directly.
        districts = (
            addr.get("district_info")
            or addr.get("district_info_list")
            or []
        )
        for district in districts:
            if not isinstance(district, dict):
                continue
            level = district.get("address_level")
            name = district.get("address_name")
            if level == "L0":
                recipient_country = name
            elif level == "L1":
                recipient_state = name
            elif level == "L2":
                recipient_county = name
            elif level == "L3":
                recipient_city = name

        # Delivery preferences is an object in TikTok's API, e.g.
        # {"drop_off_location": "Front Door"}. MySQL TEXT cannot store a
        # Python dict directly, so extract the actual instruction/value.
        delivery_preferences = addr.get("delivery_preferences")

        delivery_instruction = addr.get("delivery_instruction")

        if not delivery_instruction and isinstance(delivery_preferences, dict):
            delivery_instruction = (
                delivery_preferences.get("delivery_instruction")
                or delivery_preferences.get("instruction")
                or delivery_preferences.get("drop_off_location")
            )

        if not delivery_instruction and isinstance(delivery_preferences, str):
            delivery_instruction = delivery_preferences

    packages = order.get("packages") or []
    first_package = packages[0] if packages else {}

    params = {
        "order_id": order.get("id"),
        "status": order.get("status"),
        "create_time": unix_to_utc_datetime(order.get("create_time")),
        "update_time": unix_to_utc_datetime(order.get("update_time")),
        "warehouse_id": order.get("warehouse_id"),
        "warehouse_name": order.get("warehouse_name"),
        "shipping_type": order.get("shipping_type"),
        "delivery_option_name": order.get("delivery_option_name"),
        "rts_sla_time": unix_to_utc_datetime(order.get("rts_sla_time")),
        "auto_cancel_time": unix_to_utc_datetime(order.get("auto_cancel_time")),
        "buyer_email": order.get("buyer_email"),
        "buyer_message": order.get("buyer_message"),
        "recipient_name": recipient_name,
        "recipient_phone": recipient_phone,
        "recipient_address": recipient_address_text,
        "recipient_country": recipient_country,
        "recipient_state": recipient_state,
        "recipient_county": recipient_county,
        "recipient_city": recipient_city,
        "recipient_postal_code": recipient_postal_code,
        "recipient_address_line1": recipient_address_line1,
        "recipient_address_line2": recipient_address_line2,
        "delivery_instruction": delivery_instruction,
        "tracking_number": (
            order.get("tracking_number")
            or first_package.get("tracking_number")
        ),
        "shipping_provider": (
            order.get("shipping_provider")
            or first_package.get("shipping_provider_name")
        ),
        "shipping_provider_id": order.get("shipping_provider_id"),
        "refresh_attempts": 1 if is_refresh else 0,
    }

    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            INSERT INTO {ORDERS_TABLE} (
                order_id,
                status,
                create_time,
                update_time,
                warehouse_id,
                warehouse_name,
                shipping_type,
                delivery_option_name,
                rts_sla_time,
                auto_cancel_time,
                buyer_email,
                buyer_message,
                recipient_name,
                recipient_phone,
                recipient_address,
                recipient_country,
                recipient_state,
                recipient_county,
                recipient_city,
                recipient_postal_code,
                recipient_address_line1,
                recipient_address_line2,
                delivery_instruction,
                tracking_number,
                shipping_provider,
                shipping_provider_id,
                refresh_attempts
            ) VALUES (
                %(order_id)s,
                %(status)s,
                %(create_time)s,
                %(update_time)s,
                %(warehouse_id)s,
                %(warehouse_name)s,
                %(shipping_type)s,
                %(delivery_option_name)s,
                %(rts_sla_time)s,
                %(auto_cancel_time)s,
                %(buyer_email)s,
                %(buyer_message)s,
                %(recipient_name)s,
                %(recipient_phone)s,
                %(recipient_address)s,
                %(recipient_country)s,
                %(recipient_state)s,
                %(recipient_county)s,
                %(recipient_city)s,
                %(recipient_postal_code)s,
                %(recipient_address_line1)s,
                %(recipient_address_line2)s,
                %(delivery_instruction)s,
                %(tracking_number)s,
                %(shipping_provider)s,
                %(shipping_provider_id)s,
                %(refresh_attempts)s
            )
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                update_time = VALUES(update_time),
                warehouse_id = VALUES(warehouse_id),
                warehouse_name = VALUES(warehouse_name),
                shipping_type = VALUES(shipping_type),
                delivery_option_name = VALUES(delivery_option_name),
                rts_sla_time = VALUES(rts_sla_time),
                auto_cancel_time = VALUES(auto_cancel_time),

                buyer_email = COALESCE(
                    VALUES(buyer_email),
                    {ORDERS_TABLE}.buyer_email
                ),
                buyer_message = COALESCE(
                    VALUES(buyer_message),
                    {ORDERS_TABLE}.buyer_message
                ),
                recipient_name = COALESCE(
                    VALUES(recipient_name),
                    {ORDERS_TABLE}.recipient_name
                ),
                recipient_phone = COALESCE(
                    VALUES(recipient_phone),
                    {ORDERS_TABLE}.recipient_phone
                ),
                recipient_address = COALESCE(
                    VALUES(recipient_address),
                    {ORDERS_TABLE}.recipient_address
                ),
                recipient_country = COALESCE(
                    VALUES(recipient_country),
                    {ORDERS_TABLE}.recipient_country
                ),
                recipient_state = COALESCE(
                    VALUES(recipient_state),
                    {ORDERS_TABLE}.recipient_state
                ),
                recipient_county = COALESCE(
                    VALUES(recipient_county),
                    {ORDERS_TABLE}.recipient_county
                ),
                recipient_city = COALESCE(
                    VALUES(recipient_city),
                    {ORDERS_TABLE}.recipient_city
                ),
                recipient_postal_code = COALESCE(
                    VALUES(recipient_postal_code),
                    {ORDERS_TABLE}.recipient_postal_code
                ),
                recipient_address_line1 = COALESCE(
                    VALUES(recipient_address_line1),
                    {ORDERS_TABLE}.recipient_address_line1
                ),
                recipient_address_line2 = COALESCE(
                    VALUES(recipient_address_line2),
                    {ORDERS_TABLE}.recipient_address_line2
                ),
                delivery_instruction = COALESCE(
                    VALUES(delivery_instruction),
                    {ORDERS_TABLE}.delivery_instruction
                ),
                tracking_number = COALESCE(
                    VALUES(tracking_number),
                    {ORDERS_TABLE}.tracking_number
                ),
                shipping_provider = COALESCE(
                    VALUES(shipping_provider),
                    {ORDERS_TABLE}.shipping_provider
                ),
                shipping_provider_id = COALESCE(
                    VALUES(shipping_provider_id),
                    {ORDERS_TABLE}.shipping_provider_id
                ),

                refresh_attempts =
                    {ORDERS_TABLE}.refresh_attempts
                    + VALUES(refresh_attempts)
            """,
            params,
        )

        # Replace line items with TikTok's current version.
        cur.execute(
            f"""
            DELETE FROM {ITEMS_TABLE}
            WHERE order_id = %s
            """,
            (order.get("id"),),
        )

        if line_items:
            cur.executemany(
                f"""
                INSERT INTO {ITEMS_TABLE} (
                    order_id,
                    product_name,
                    sku_id,
                    sku_name,
                    seller_sku,
                    sale_price,
                    quantity
                ) VALUES (
                    %(order_id)s,
                    %(product_name)s,
                    %(sku_id)s,
                    %(sku_name)s,
                    %(seller_sku)s,
                    %(sale_price)s,
                    %(quantity)s
                )
                """,
                [
                    {
                        **li,
                        "order_id": order.get("id"),
                    }
                    for li in line_items
                ],
            )
    finally:
        cur.close()


# =========================================================
# Backfill query
# =========================================================

def get_orders_needing_refresh(
    conn,
    limit: int = 200,
) -> list:
    cancelled_placeholders = ",".join(
        ["%s"] * len(CANCELLED_STATUSES)
    )

    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT order_id
            FROM {ORDERS_TABLE}
            WHERE UPPER(status) NOT IN ({cancelled_placeholders})
              AND (
                    (
                        buyer_email IS NULL
                        AND recipient_name IS NULL
                    )
                    OR tracking_number IS NULL
                  )
              AND refresh_attempts < %s
              AND (
                    synced_at IS NULL
                    OR (
                        refresh_attempts < %s
                        AND synced_at <
                            DATE_SUB(
                                NOW(),
                                INTERVAL %s MINUTE
                            )
                    )
                    OR (
                        refresh_attempts >= %s
                        AND synced_at <
                            DATE_SUB(
                                NOW(),
                                INTERVAL %s HOUR
                            )
                    )
                  )
            ORDER BY create_time ASC
            LIMIT %s
            """,
            (
                *CANCELLED_STATUSES,
                MAX_REFRESH_ATTEMPTS,
                BACKOFF_FAST_ATTEMPTS,
                BACKOFF_FAST_MINUTES,
                BACKOFF_FAST_ATTEMPTS,
                BACKOFF_SLOW_HOURS,
                limit,
            ),
        )

        return [
            row["order_id"]
            for row in cur.fetchall()
        ]
    finally:
        cur.close()


# =========================================================
# Stuck orders
# =========================================================

def get_stuck_orders(conn) -> list:
    cancelled_placeholders = ",".join(
        ["%s"] * len(CANCELLED_STATUSES)
    )

    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT
                order_id,
                status,
                buyer_email,
                recipient_name,
                tracking_number,
                refresh_attempts,
                synced_at
            FROM {ORDERS_TABLE}
            WHERE UPPER(status) NOT IN ({cancelled_placeholders})
              AND (
                    (
                        buyer_email IS NULL
                        AND recipient_name IS NULL
                    )
                    OR tracking_number IS NULL
                  )
              AND refresh_attempts >= %s
            ORDER BY create_time ASC
            """,
            (
                *CANCELLED_STATUSES,
                MAX_REFRESH_ATTEMPTS,
            ),
        )

        return cur.fetchall()
    finally:
        cur.close()


# =========================================================
# One order summary
# =========================================================

def get_order_summary(
    conn,
    order_id: str,
):
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT
                order_id,
                status,
                buyer_email,
                recipient_name,
                recipient_phone,
                tracking_number,
                refresh_attempts,
                synced_at
            FROM {ORDERS_TABLE}
            WHERE order_id = %s
            """,
            (order_id,),
        )

        return cur.fetchone()
    finally:
        cur.close()
