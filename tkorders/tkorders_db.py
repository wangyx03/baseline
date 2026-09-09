#!/usr/bin/env python3
"""
Database layer for multi-store TikTok Shop orders.

Shared tables:
    tkorders
    tkorders_items

Store master:
    stores

Sync watermark:
    tkorders_sync_state

The order primary key is (store_id, order_id), so TU and VB share the same
tables without mixing their data.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db


ORDERS_TABLE = "tkorders"
ITEMS_TABLE = "tkorders_items"
SYNC_STATE_TABLE = "tkorders_sync_state"

MAX_REFRESH_ATTEMPTS = 20
BACKOFF_FAST_ATTEMPTS = 5
BACKOFF_FAST_MINUTES = 25
BACKOFF_SLOW_HOURS = 24

CANCELLED_STATUSES = {
    "CANCELLED",
    "CANCELED",
}


def get_connection():
    return get_db()


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


def _blank_to_none(value):
    if value == "":
        return None
    return value


# ---------------------------------------------------------
# Schema
# ---------------------------------------------------------
CREATE_ORDERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {ORDERS_TABLE} (
    store_id SMALLINT UNSIGNED NOT NULL,
    order_id VARCHAR(64) NOT NULL,

    status VARCHAR(64) DEFAULT NULL,
    order_type VARCHAR(64) DEFAULT NULL,
    fulfillment_type VARCHAR(64) DEFAULT NULL,
    delivery_type VARCHAR(64) DEFAULT NULL,
    shipping_type VARCHAR(64) DEFAULT NULL,

    create_time DATETIME DEFAULT NULL,
    paid_time DATETIME DEFAULT NULL,
    update_time DATETIME DEFAULT NULL,
    rts_sla_time DATETIME DEFAULT NULL,
    shipping_due_time DATETIME DEFAULT NULL,
    delivery_sla_time DATETIME DEFAULT NULL,
    cancel_order_sla_time DATETIME DEFAULT NULL,

    buyer_user_id VARCHAR(64) DEFAULT NULL,
    buyer_nickname VARCHAR(255) DEFAULT NULL,
    buyer_email VARCHAR(255) DEFAULT NULL,
    buyer_message TEXT,

    recipient_name VARCHAR(255) DEFAULT NULL,
    recipient_phone VARCHAR(64) DEFAULT NULL,
    recipient_country VARCHAR(100) DEFAULT NULL,
    recipient_state VARCHAR(100) DEFAULT NULL,
    recipient_county VARCHAR(100) DEFAULT NULL,
    recipient_city VARCHAR(100) DEFAULT NULL,
    recipient_postal_code VARCHAR(30) DEFAULT NULL,
    recipient_address_line1 VARCHAR(255) DEFAULT NULL,
    recipient_address_line2 VARCHAR(255) DEFAULT NULL,
    recipient_full_address TEXT,
    delivery_instruction TEXT,

    delivery_option_id VARCHAR(64) DEFAULT NULL,
    delivery_option_name VARCHAR(128) DEFAULT NULL,

    warehouse_id VARCHAR(64) DEFAULT NULL,

    currency VARCHAR(10) DEFAULT NULL,
    payment_method_name VARCHAR(100) DEFAULT NULL,

    original_total_product_price DECIMAL(12,2) DEFAULT NULL,
    original_shipping_fee DECIMAL(12,2) DEFAULT NULL,
    platform_discount DECIMAL(12,2) DEFAULT NULL,
    seller_discount DECIMAL(12,2) DEFAULT NULL,
    shipping_fee DECIMAL(12,2) DEFAULT NULL,
    shipping_fee_cofunded_discount DECIMAL(12,2) DEFAULT NULL,
    shipping_fee_platform_discount DECIMAL(12,2) DEFAULT NULL,
    shipping_fee_seller_discount DECIMAL(12,2) DEFAULT NULL,
    product_tax DECIMAL(12,2) DEFAULT NULL,
    shipping_fee_tax DECIMAL(12,2) DEFAULT NULL,
    tax DECIMAL(12,2) DEFAULT NULL,
    sub_total DECIMAL(12,2) DEFAULT NULL,
    total_amount DECIMAL(12,2) DEFAULT NULL,

    auto_combine_group_id VARCHAR(64) DEFAULT NULL,

    is_cod BOOLEAN DEFAULT NULL,
    is_on_hold_order BOOLEAN DEFAULT NULL,
    is_exchange_order BOOLEAN DEFAULT NULL,
    is_replacement_order BOOLEAN DEFAULT NULL,
    is_sample_order BOOLEAN DEFAULT NULL,
    is_subscription_order BOOLEAN DEFAULT NULL,

    refresh_attempts INT NOT NULL DEFAULT 0,

    synced_at DATETIME NOT NULL
        DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (store_id, order_id),

    KEY idx_tkorders_order_id (order_id),
    KEY idx_tkorders_create_time (store_id, create_time),
    KEY idx_tkorders_status (store_id, status),
    KEY idx_tkorders_buyer_user_id (buyer_user_id),

    CONSTRAINT fk_tkorders_store
        FOREIGN KEY (store_id)
        REFERENCES stores(store_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
"""

CREATE_ITEMS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    store_id SMALLINT UNSIGNED NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    line_item_id VARCHAR(64) NOT NULL,

    product_id VARCHAR(64) DEFAULT NULL,
    product_name VARCHAR(500) DEFAULT NULL,

    sku_id VARCHAR(64) DEFAULT NULL,
    sku_name VARCHAR(255) DEFAULT NULL,
    seller_sku VARCHAR(128) DEFAULT NULL,
    sku_type VARCHAR(64) DEFAULT NULL,

    quantity INT NOT NULL DEFAULT 1,

    currency VARCHAR(10) DEFAULT NULL,
    original_price DECIMAL(12,2) DEFAULT NULL,
    sale_price DECIMAL(12,2) DEFAULT NULL,
    platform_discount DECIMAL(12,2) DEFAULT NULL,
    seller_discount DECIMAL(12,2) DEFAULT NULL,

    sales_tax_amount DECIMAL(12,2) DEFAULT NULL,
    sales_tax_rate DECIMAL(12,6) DEFAULT NULL,

    room_id VARCHAR(64) DEFAULT NULL,
    display_status VARCHAR(64) DEFAULT NULL,

    is_gift BOOLEAN DEFAULT NULL,
    is_dangerous_good BOOLEAN DEFAULT NULL,
    is_pod_customized BOOLEAN DEFAULT NULL,

    tracking_number VARCHAR(128) DEFAULT NULL,

    PRIMARY KEY (id),

    UNIQUE KEY uk_tkorders_items (
        store_id,
        order_id,
        line_item_id
    ),

    KEY idx_tkorders_items_order (
        store_id,
        order_id
    ),

    KEY idx_tkorders_items_seller_sku (
        seller_sku
    ),

    KEY idx_tkorders_items_sku_id (
        sku_id
    ),

    CONSTRAINT fk_tkorders_items_order
        FOREIGN KEY (store_id, order_id)
        REFERENCES tkorders(store_id, order_id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
"""

CREATE_SYNC_STATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SYNC_STATE_TABLE} (
    store_id SMALLINT UNSIGNED NOT NULL,
    last_check BIGINT UNSIGNED DEFAULT NULL,
    updated_at DATETIME NOT NULL
        DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (store_id),

    CONSTRAINT fk_tkorders_sync_state_store
        FOREIGN KEY (store_id)
        REFERENCES stores(store_id)
        ON DELETE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
"""


def init_schema(conn):
    cur = conn.cursor()

    try:
        cur.execute(CREATE_ORDERS_TABLE)
        cur.execute(CREATE_ITEMS_TABLE)
        cur.execute(CREATE_SYNC_STATE_TABLE)
        conn.commit()
    finally:
        cur.close()


# ---------------------------------------------------------
# Sync state
# ---------------------------------------------------------
def get_last_check(conn, store_id: int):
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT last_check
            FROM {SYNC_STATE_TABLE}
            WHERE store_id = %s
            """,
            (store_id,),
        )

        row = cur.fetchone()

        if not row:
            return None

        return row["last_check"]
    finally:
        cur.close()


def set_last_check(
    conn,
    store_id: int,
    timestamp: int,
):
    cur = conn.cursor()

    try:
        cur.execute(
            f"""
            INSERT INTO {SYNC_STATE_TABLE} (
                store_id,
                last_check
            ) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                last_check = VALUES(last_check)
            """,
            (store_id, timestamp),
        )
    finally:
        cur.close()


# ---------------------------------------------------------
# Order parsing / upsert
# ---------------------------------------------------------
def upsert_order(
    conn,
    store_id: int,
    order: dict,
    line_items: list,
    is_refresh: bool,
):
    addr = order.get("recipient_address") or {}
    payment = order.get("payment") or {}

    recipient_name = None
    recipient_phone = None
    recipient_country = None
    recipient_state = None
    recipient_county = None
    recipient_city = None
    recipient_postal_code = None
    recipient_address_line1 = None
    recipient_address_line2 = None
    recipient_full_address = None
    delivery_instruction = None

    if isinstance(addr, dict):
        recipient_name = _blank_to_none(addr.get("name"))
        recipient_phone = _blank_to_none(addr.get("phone_number"))
        recipient_postal_code = _blank_to_none(addr.get("postal_code"))
        recipient_address_line1 = _blank_to_none(addr.get("address_line1"))
        recipient_address_line2 = _blank_to_none(addr.get("address_line2"))
        recipient_full_address = _blank_to_none(
            addr.get("full_address")
            or addr.get("address_detail")
        )

        districts = (
            addr.get("district_info")
            or addr.get("district_info_list")
            or []
        )

        for district in districts:
            if not isinstance(district, dict):
                continue

            level = district.get("address_level")
            name = _blank_to_none(district.get("address_name"))

            if level == "L0":
                recipient_country = name
            elif level == "L1":
                recipient_state = name
            elif level == "L2":
                recipient_county = name
            elif level == "L3":
                recipient_city = name

        delivery_preferences = addr.get("delivery_preferences")

        delivery_instruction = _blank_to_none(
            addr.get("delivery_instruction")
        )

        if not delivery_instruction and isinstance(
            delivery_preferences,
            dict,
        ):
            delivery_instruction = _blank_to_none(
                delivery_preferences.get("delivery_instruction")
                or delivery_preferences.get("instruction")
                or delivery_preferences.get("drop_off_location")
            )

        if not delivery_instruction and isinstance(
            delivery_preferences,
            str,
        ):
            delivery_instruction = _blank_to_none(delivery_preferences)

    params = {
        "store_id": store_id,
        "order_id": order.get("id"),

        "status": order.get("status"),
        "order_type": order.get("order_type"),
        "fulfillment_type": order.get("fulfillment_type"),
        "delivery_type": order.get("delivery_type"),
        "shipping_type": order.get("shipping_type"),

        "create_time": unix_to_utc_datetime(order.get("create_time")),
        "paid_time": unix_to_utc_datetime(order.get("paid_time")),
        "update_time": unix_to_utc_datetime(order.get("update_time")),
        "rts_sla_time": unix_to_utc_datetime(order.get("rts_sla_time")),
        "shipping_due_time": unix_to_utc_datetime(order.get("shipping_due_time")),
        "delivery_sla_time": unix_to_utc_datetime(order.get("delivery_sla_time")),
        "cancel_order_sla_time": unix_to_utc_datetime(order.get("cancel_order_sla_time")),

        "buyer_user_id": order.get("user_id"),
        "buyer_nickname": _blank_to_none(order.get("buyer_nickname")),
        "buyer_email": _blank_to_none(order.get("buyer_email")),
        "buyer_message": _blank_to_none(order.get("buyer_message")),

        "recipient_name": recipient_name,
        "recipient_phone": recipient_phone,
        "recipient_country": recipient_country,
        "recipient_state": recipient_state,
        "recipient_county": recipient_county,
        "recipient_city": recipient_city,
        "recipient_postal_code": recipient_postal_code,
        "recipient_address_line1": recipient_address_line1,
        "recipient_address_line2": recipient_address_line2,
        "recipient_full_address": recipient_full_address,
        "delivery_instruction": delivery_instruction,

        "delivery_option_id": order.get("delivery_option_id"),
        "delivery_option_name": order.get("delivery_option_name"),
        "warehouse_id": order.get("warehouse_id"),

        "currency": payment.get("currency"),
        "payment_method_name": _blank_to_none(
            order.get("payment_method_name")
        ),

        "original_total_product_price": payment.get("original_total_product_price"),
        "original_shipping_fee": payment.get("original_shipping_fee"),
        "platform_discount": payment.get("platform_discount"),
        "seller_discount": payment.get("seller_discount"),
        "shipping_fee": payment.get("shipping_fee"),
        "shipping_fee_cofunded_discount": payment.get("shipping_fee_cofunded_discount"),
        "shipping_fee_platform_discount": payment.get("shipping_fee_platform_discount"),
        "shipping_fee_seller_discount": payment.get("shipping_fee_seller_discount"),
        "product_tax": payment.get("product_tax"),
        "shipping_fee_tax": payment.get("shipping_fee_tax"),
        "tax": payment.get("tax"),
        "sub_total": payment.get("sub_total"),
        "total_amount": payment.get("total_amount"),

        "auto_combine_group_id": order.get("auto_combine_group_id"),

        "is_cod": order.get("is_cod"),
        "is_on_hold_order": order.get("is_on_hold_order"),
        "is_exchange_order": order.get("is_exchange_order"),
        "is_replacement_order": order.get("is_replacement_order"),
        "is_sample_order": order.get("is_sample_order"),
        "is_subscription_order": order.get("is_subscription_order"),

        "refresh_attempts": 1 if is_refresh else 0,
    }

    cur = conn.cursor()

    try:
        cur.execute(
            f"""
            INSERT INTO {ORDERS_TABLE} (
                store_id,
                order_id,

                status,
                order_type,
                fulfillment_type,
                delivery_type,
                shipping_type,

                create_time,
                paid_time,
                update_time,
                rts_sla_time,
                shipping_due_time,
                delivery_sla_time,
                cancel_order_sla_time,

                buyer_user_id,
                buyer_nickname,
                buyer_email,
                buyer_message,

                recipient_name,
                recipient_phone,
                recipient_country,
                recipient_state,
                recipient_county,
                recipient_city,
                recipient_postal_code,
                recipient_address_line1,
                recipient_address_line2,
                recipient_full_address,
                delivery_instruction,

                delivery_option_id,
                delivery_option_name,
                warehouse_id,

                currency,
                payment_method_name,

                original_total_product_price,
                original_shipping_fee,
                platform_discount,
                seller_discount,
                shipping_fee,
                shipping_fee_cofunded_discount,
                shipping_fee_platform_discount,
                shipping_fee_seller_discount,
                product_tax,
                shipping_fee_tax,
                tax,
                sub_total,
                total_amount,

                auto_combine_group_id,

                is_cod,
                is_on_hold_order,
                is_exchange_order,
                is_replacement_order,
                is_sample_order,
                is_subscription_order,

                refresh_attempts
            ) VALUES (
                %(store_id)s,
                %(order_id)s,

                %(status)s,
                %(order_type)s,
                %(fulfillment_type)s,
                %(delivery_type)s,
                %(shipping_type)s,

                %(create_time)s,
                %(paid_time)s,
                %(update_time)s,
                %(rts_sla_time)s,
                %(shipping_due_time)s,
                %(delivery_sla_time)s,
                %(cancel_order_sla_time)s,

                %(buyer_user_id)s,
                %(buyer_nickname)s,
                %(buyer_email)s,
                %(buyer_message)s,

                %(recipient_name)s,
                %(recipient_phone)s,
                %(recipient_country)s,
                %(recipient_state)s,
                %(recipient_county)s,
                %(recipient_city)s,
                %(recipient_postal_code)s,
                %(recipient_address_line1)s,
                %(recipient_address_line2)s,
                %(recipient_full_address)s,
                %(delivery_instruction)s,

                %(delivery_option_id)s,
                %(delivery_option_name)s,
                %(warehouse_id)s,

                %(currency)s,
                %(payment_method_name)s,

                %(original_total_product_price)s,
                %(original_shipping_fee)s,
                %(platform_discount)s,
                %(seller_discount)s,
                %(shipping_fee)s,
                %(shipping_fee_cofunded_discount)s,
                %(shipping_fee_platform_discount)s,
                %(shipping_fee_seller_discount)s,
                %(product_tax)s,
                %(shipping_fee_tax)s,
                %(tax)s,
                %(sub_total)s,
                %(total_amount)s,

                %(auto_combine_group_id)s,

                %(is_cod)s,
                %(is_on_hold_order)s,
                %(is_exchange_order)s,
                %(is_replacement_order)s,
                %(is_sample_order)s,
                %(is_subscription_order)s,

                %(refresh_attempts)s
            )
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                order_type = VALUES(order_type),
                fulfillment_type = VALUES(fulfillment_type),
                delivery_type = VALUES(delivery_type),
                shipping_type = VALUES(shipping_type),

                paid_time = COALESCE(
                    VALUES(paid_time),
                    {ORDERS_TABLE}.paid_time
                ),
                update_time = VALUES(update_time),
                rts_sla_time = COALESCE(
                    VALUES(rts_sla_time),
                    {ORDERS_TABLE}.rts_sla_time
                ),
                shipping_due_time = COALESCE(
                    VALUES(shipping_due_time),
                    {ORDERS_TABLE}.shipping_due_time
                ),
                delivery_sla_time = COALESCE(
                    VALUES(delivery_sla_time),
                    {ORDERS_TABLE}.delivery_sla_time
                ),
                cancel_order_sla_time = COALESCE(
                    VALUES(cancel_order_sla_time),
                    {ORDERS_TABLE}.cancel_order_sla_time
                ),

                buyer_user_id = COALESCE(
                    VALUES(buyer_user_id),
                    {ORDERS_TABLE}.buyer_user_id
                ),
                buyer_nickname = COALESCE(
                    VALUES(buyer_nickname),
                    {ORDERS_TABLE}.buyer_nickname
                ),
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
                recipient_full_address = COALESCE(
                    VALUES(recipient_full_address),
                    {ORDERS_TABLE}.recipient_full_address
                ),
                delivery_instruction = COALESCE(
                    VALUES(delivery_instruction),
                    {ORDERS_TABLE}.delivery_instruction
                ),

                delivery_option_id = COALESCE(
                    VALUES(delivery_option_id),
                    {ORDERS_TABLE}.delivery_option_id
                ),
                delivery_option_name = COALESCE(
                    VALUES(delivery_option_name),
                    {ORDERS_TABLE}.delivery_option_name
                ),
                warehouse_id = COALESCE(
                    VALUES(warehouse_id),
                    {ORDERS_TABLE}.warehouse_id
                ),

                currency = COALESCE(
                    VALUES(currency),
                    {ORDERS_TABLE}.currency
                ),
                payment_method_name = COALESCE(
                    VALUES(payment_method_name),
                    {ORDERS_TABLE}.payment_method_name
                ),

                original_total_product_price = COALESCE(
                    VALUES(original_total_product_price),
                    {ORDERS_TABLE}.original_total_product_price
                ),
                original_shipping_fee = COALESCE(
                    VALUES(original_shipping_fee),
                    {ORDERS_TABLE}.original_shipping_fee
                ),
                platform_discount = COALESCE(
                    VALUES(platform_discount),
                    {ORDERS_TABLE}.platform_discount
                ),
                seller_discount = COALESCE(
                    VALUES(seller_discount),
                    {ORDERS_TABLE}.seller_discount
                ),
                shipping_fee = COALESCE(
                    VALUES(shipping_fee),
                    {ORDERS_TABLE}.shipping_fee
                ),
                shipping_fee_cofunded_discount = COALESCE(
                    VALUES(shipping_fee_cofunded_discount),
                    {ORDERS_TABLE}.shipping_fee_cofunded_discount
                ),
                shipping_fee_platform_discount = COALESCE(
                    VALUES(shipping_fee_platform_discount),
                    {ORDERS_TABLE}.shipping_fee_platform_discount
                ),
                shipping_fee_seller_discount = COALESCE(
                    VALUES(shipping_fee_seller_discount),
                    {ORDERS_TABLE}.shipping_fee_seller_discount
                ),
                product_tax = COALESCE(
                    VALUES(product_tax),
                    {ORDERS_TABLE}.product_tax
                ),
                shipping_fee_tax = COALESCE(
                    VALUES(shipping_fee_tax),
                    {ORDERS_TABLE}.shipping_fee_tax
                ),
                tax = COALESCE(
                    VALUES(tax),
                    {ORDERS_TABLE}.tax
                ),
                sub_total = COALESCE(
                    VALUES(sub_total),
                    {ORDERS_TABLE}.sub_total
                ),
                total_amount = COALESCE(
                    VALUES(total_amount),
                    {ORDERS_TABLE}.total_amount
                ),

                auto_combine_group_id = COALESCE(
                    VALUES(auto_combine_group_id),
                    {ORDERS_TABLE}.auto_combine_group_id
                ),

                is_cod = VALUES(is_cod),
                is_on_hold_order = VALUES(is_on_hold_order),
                is_exchange_order = VALUES(is_exchange_order),
                is_replacement_order = VALUES(is_replacement_order),
                is_sample_order = VALUES(is_sample_order),
                is_subscription_order = VALUES(is_subscription_order),

                refresh_attempts =
                    {ORDERS_TABLE}.refresh_attempts
                    + VALUES(refresh_attempts)
            """,
            params,
        )

        # The Order Detail API returns the current complete line-item list.
        # Replace this order's items atomically inside the same transaction.
        cur.execute(
            f"""
            DELETE FROM {ITEMS_TABLE}
            WHERE store_id = %s
              AND order_id = %s
            """,
            (
                store_id,
                order.get("id"),
            ),
        )

        if line_items:
            cur.executemany(
                f"""
                INSERT INTO {ITEMS_TABLE} (
                    store_id,
                    order_id,
                    line_item_id,

                    product_id,
                    product_name,
                    sku_id,
                    sku_name,
                    seller_sku,
                    sku_type,
                    quantity,

                    currency,
                    original_price,
                    sale_price,
                    platform_discount,
                    seller_discount,

                    sales_tax_amount,
                    sales_tax_rate,

                    room_id,
                    display_status,

                    is_gift,
                    is_dangerous_good,
                    is_pod_customized,

                    tracking_number
                ) VALUES (
                    %(store_id)s,
                    %(order_id)s,
                    %(line_item_id)s,

                    %(product_id)s,
                    %(product_name)s,
                    %(sku_id)s,
                    %(sku_name)s,
                    %(seller_sku)s,
                    %(sku_type)s,
                    %(quantity)s,

                    %(currency)s,
                    %(original_price)s,
                    %(sale_price)s,
                    %(platform_discount)s,
                    %(seller_discount)s,

                    %(sales_tax_amount)s,
                    %(sales_tax_rate)s,

                    %(room_id)s,
                    %(display_status)s,

                    %(is_gift)s,
                    %(is_dangerous_good)s,
                    %(is_pod_customized)s,

                    %(tracking_number)s
                )
                """,
                [
                    {
                        **li,
                        "store_id": store_id,
                        "order_id": order.get("id"),
                    }
                    for li in line_items
                ],
            )
    finally:
        cur.close()


# ---------------------------------------------------------
# Backfill
# ---------------------------------------------------------
def get_orders_needing_refresh(
    conn,
    store_id: int,
    limit: int = 200,
) -> list:
    cancelled_placeholders = ",".join(
        ["%s"] * len(CANCELLED_STATUSES)
    )

    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT o.order_id
            FROM {ORDERS_TABLE} o
            WHERE o.store_id = %s
              AND UPPER(o.status) NOT IN ({cancelled_placeholders})

              AND (
                    (
                        o.buyer_email IS NULL
                        AND o.recipient_name IS NULL
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM {ITEMS_TABLE} i
                        WHERE i.store_id = o.store_id
                          AND i.order_id = o.order_id
                          AND (
                                i.tracking_number IS NULL
                                OR i.tracking_number = ''
                              )
                    )
                  )

              AND o.refresh_attempts < %s

              AND (
                    o.synced_at IS NULL

                    OR (
                        o.refresh_attempts < %s
                        AND o.synced_at <
                            DATE_SUB(
                                NOW(),
                                INTERVAL %s MINUTE
                            )
                    )

                    OR (
                        o.refresh_attempts >= %s
                        AND o.synced_at <
                            DATE_SUB(
                                NOW(),
                                INTERVAL %s HOUR
                            )
                    )
                  )

            ORDER BY o.create_time ASC
            LIMIT %s
            """,
            (
                store_id,
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


def get_stuck_orders(
    conn,
    store_id: int,
) -> list:
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
                buyer_nickname,
                buyer_email,
                recipient_name,
                refresh_attempts,
                synced_at
            FROM {ORDERS_TABLE}
            WHERE store_id = %s
              AND UPPER(status) NOT IN ({cancelled_placeholders})
              AND refresh_attempts >= %s
            ORDER BY create_time ASC
            """,
            (
                store_id,
                *CANCELLED_STATUSES,
                MAX_REFRESH_ATTEMPTS,
            ),
        )

        return cur.fetchall()
    finally:
        cur.close()


def get_order_summary(
    conn,
    store_id: int,
    order_id: str,
):
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            f"""
            SELECT
                store_id,
                order_id,
                status,
                buyer_user_id,
                buyer_nickname,
                buyer_email,
                recipient_name,
                recipient_phone,
                refresh_attempts,
                synced_at
            FROM {ORDERS_TABLE}
            WHERE store_id = %s
              AND order_id = %s
            """,
            (
                store_id,
                order_id,
            ),
        )

        return cur.fetchone()
    finally:
        cur.close()
