#!/usr/bin/env python3
"""
db.py

MySQL layer for the TikTok order sync pipeline.

Tables:

    tkorders_tu
        One row per TikTok order.

    tkorders_items_tu
        One row per SKU / line item.

All DATETIME values in this database are stored as UTC.

Required .env values:

    DB_HOST=127.0.0.1
    DB_PORT=3306
    DB_USER=root
    DB_PASSWORD=...
    DB_NAME=olselling

Optional:

    TKORDERS_TABLE=tkorders_tu
    TKORDERS_ITEMS_TABLE=tkorders_items_tu
"""

import os
import re

from datetime import datetime, timezone

import pymysql
import pymysql.cursors


# =========================================================
# Config
# =========================================================

MAX_REFRESH_ATTEMPTS = 20

BACKOFF_FAST_ATTEMPTS = 5
BACKOFF_FAST_MINUTES = 25

BACKOFF_SLOW_HOURS = 24

CANCELLED_STATUSES = {
    "CANCELLED",
    "CANCELED"
}


# =========================================================
# Safe table names
# =========================================================

_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*$"
)


def _safe_table_name(
    env_var: str,
    default: str
) -> str:

    name = os.environ.get(
        env_var,
        default
    )

    if not _IDENTIFIER_RE.match(name):

        raise ValueError(
            f"{env_var}={name!r} "
            "is not a valid table name"
        )

    return name


ORDERS_TABLE = _safe_table_name(
    "TKORDERS_TABLE",
    "tkorders_tu"
)

ITEMS_TABLE = _safe_table_name(
    "TKORDERS_ITEMS_TABLE",
    "tkorders_items_tu"
)


# =========================================================
# Time helpers
# =========================================================

def unix_to_utc_datetime(value):
    """
    Convert TikTok Unix timestamp into a naive Python datetime
    representing UTC.

    Example:

        1786413206
            ↓
        datetime(2026, ..., ...)

    MySQL DATETIME does not store timezone information,
    so by convention every DATETIME in these tables is UTC.
    """

    if value is None:
        return None

    if value == "":
        return None

    try:

        timestamp = int(value)

    except (TypeError, ValueError):

        return None

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc
    ).replace(
        tzinfo=None
    )


# =========================================================
# Database connection
# =========================================================

def get_connection():

    conn = pymysql.connect(
        host=os.environ.get(
            "DB_HOST",
            "127.0.0.1"
        ),

        port=int(
            os.environ.get(
                "DB_PORT",
                "3306"
            )
        ),

        user=os.environ.get(
            "DB_USER",
            "root"
        ),

        password=os.environ.get(
            "DB_PASSWORD",
            ""
        ),

        database=os.environ.get(
            "DB_NAME",
            "olselling"
        ),

        charset="utf8mb4",

        autocommit=False,

        cursorclass=
            pymysql.cursors.DictCursor
    )

    # -----------------------------------------------------
    # Force this MySQL session to UTC.
    #
    # This makes:
    # CURRENT_TIMESTAMP
    # NOW()
    # DATE_SUB(NOW(), ...)
    #
    # all operate in UTC.
    # -----------------------------------------------------

    with conn.cursor() as cur:

        cur.execute(
            "SET time_zone = '+00:00'"
        )

    return conn


# =========================================================
# Schema
# =========================================================

SCHEMA = f"""
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

    tracking_number       VARCHAR(128),
    shipping_provider     VARCHAR(128),
    shipping_provider_id  VARCHAR(64),

    refresh_attempts      INT NOT NULL DEFAULT 0,

    synced_at             DATETIME
                          DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_create_time (create_time)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (

    id              BIGINT
                    AUTO_INCREMENT
                    PRIMARY KEY,

    order_id        VARCHAR(64)
                    NOT NULL,

    product_name    VARCHAR(255),

    sku_id          VARCHAR(64),

    sku_name        VARCHAR(64),

    seller_sku      VARCHAR(128),

    sale_price      VARCHAR(32),

    quantity        INT,

    FOREIGN KEY (order_id)
        REFERENCES {ORDERS_TABLE}(order_id)
        ON DELETE CASCADE,

    INDEX idx_order_id (
        order_id
    ),

    INDEX idx_seller_sku (
        seller_sku
    )

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4;
"""


# =========================================================
# Initialize schema
# =========================================================

def init_schema(conn):

    with conn.cursor() as cur:

        for statement in SCHEMA.split(
            ";\n\n"
        ):

            statement = (
                statement.strip()
            )

            if statement:

                cur.execute(
                    statement
                )

    conn.commit()


# =========================================================
# Status helpers
# =========================================================

def _is_cancelled(order: dict) -> bool:

    return str(
        order.get(
            "status",
            ""
        )
    ).upper() in CANCELLED_STATUSES


# =========================================================
# Upsert order
# =========================================================

def upsert_order(
    conn,
    order: dict,
    line_items: list,
    is_refresh: bool
):

    # -----------------------------------------------------
    # Recipient information
    # -----------------------------------------------------

    addr = (
        order.get(
            "recipient_address"
        )
        or {}
    )


    if isinstance(
        addr,
        dict
    ):

        recipient_name = (
            addr.get(
                "name"
            )
        )

        recipient_phone = (
            addr.get(
                "phone_number"
            )
        )

        recipient_address_text = (

            addr.get(
                "full_address"
            )

            or

            addr.get(
                "address_detail"
            )

            or

            (
                str(addr)
                if addr
                else None
            )
        )

    else:

        recipient_name = None
        recipient_phone = None

        recipient_address_text = (
            str(addr)
            if addr
            else None
        )


    # -----------------------------------------------------
    # Package information
    # -----------------------------------------------------

    packages = (
        order.get(
            "packages"
        )
        or []
    )


    first_package = (
        packages[0]
        if packages
        else {}
    )


    # -----------------------------------------------------
    # Convert TikTok timestamps
    # Unix timestamp → UTC DATETIME
    # -----------------------------------------------------

    create_time_utc = (
        unix_to_utc_datetime(
            order.get(
                "create_time"
            )
        )
    )

    update_time_utc = (
        unix_to_utc_datetime(
            order.get(
                "update_time"
            )
        )
    )

    rts_sla_time_utc = (
        unix_to_utc_datetime(
            order.get(
                "rts_sla_time"
            )
        )
    )

    auto_cancel_time_utc = (
        unix_to_utc_datetime(
            order.get(
                "auto_cancel_time"
            )
        )
    )


    # =====================================================
    # Database transaction
    # =====================================================

    with conn.cursor() as cur:

        # -------------------------------------------------
        # Order
        # -------------------------------------------------

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

                %(tracking_number)s,
                %(shipping_provider)s,
                %(shipping_provider_id)s,

                %(refresh_attempts)s

            )

            ON DUPLICATE KEY UPDATE

                status =
                    VALUES(status),

                update_time =
                    VALUES(update_time),

                warehouse_id =
                    VALUES(warehouse_id),

                warehouse_name =
                    VALUES(warehouse_name),

                shipping_type =
                    VALUES(shipping_type),

                delivery_option_name =
                    VALUES(delivery_option_name),

                rts_sla_time =
                    VALUES(rts_sla_time),

                auto_cancel_time =
                    VALUES(auto_cancel_time),

                buyer_email =
                    COALESCE(
                        VALUES(buyer_email),
                        {ORDERS_TABLE}.buyer_email
                    ),

                buyer_message =
                    COALESCE(
                        VALUES(buyer_message),
                        {ORDERS_TABLE}.buyer_message
                    ),

                recipient_name =
                    COALESCE(
                        VALUES(recipient_name),
                        {ORDERS_TABLE}.recipient_name
                    ),

                recipient_phone =
                    COALESCE(
                        VALUES(recipient_phone),
                        {ORDERS_TABLE}.recipient_phone
                    ),

                recipient_address =
                    COALESCE(
                        VALUES(recipient_address),
                        {ORDERS_TABLE}.recipient_address
                    ),

                tracking_number =
                    COALESCE(
                        VALUES(tracking_number),
                        {ORDERS_TABLE}.tracking_number
                    ),

                shipping_provider =
                    COALESCE(
                        VALUES(shipping_provider),
                        {ORDERS_TABLE}.shipping_provider
                    ),

                shipping_provider_id =
                    COALESCE(
                        VALUES(shipping_provider_id),
                        {ORDERS_TABLE}.shipping_provider_id
                    ),

                refresh_attempts =
                    {ORDERS_TABLE}.refresh_attempts
                    +
                    VALUES(refresh_attempts)

            """,

            {

                "order_id":
                    order.get(
                        "id"
                    ),

                "status":
                    order.get(
                        "status"
                    ),

                # -----------------------------
                # All UTC DATETIME
                # -----------------------------

                "create_time":
                    create_time_utc,

                "update_time":
                    update_time_utc,

                "rts_sla_time":
                    rts_sla_time_utc,

                "auto_cancel_time":
                    auto_cancel_time_utc,

                # -----------------------------

                "warehouse_id":
                    order.get(
                        "warehouse_id"
                    ),

                "warehouse_name":
                    order.get(
                        "warehouse_name"
                    ),

                "shipping_type":
                    order.get(
                        "shipping_type"
                    ),

                "delivery_option_name":
                    order.get(
                        "delivery_option_name"
                    ),

                "buyer_email":
                    order.get(
                        "buyer_email"
                    ),

                "buyer_message":
                    order.get(
                        "buyer_message"
                    ),

                "recipient_name":
                    recipient_name,

                "recipient_phone":
                    recipient_phone,

                "recipient_address":
                    recipient_address_text,

                "tracking_number":
                    (
                        order.get(
                            "tracking_number"
                        )
                        or
                        first_package.get(
                            "tracking_number"
                        )
                    ),

                "shipping_provider":
                    (
                        order.get(
                            "shipping_provider"
                        )
                        or
                        first_package.get(
                            "shipping_provider_name"
                        )
                    ),

                "shipping_provider_id":
                    order.get(
                        "shipping_provider_id"
                    ),

                "refresh_attempts":
                    (
                        1
                        if is_refresh
                        else 0
                    )
            }
        )


        # -------------------------------------------------
        # Line items
        #
        # Refresh 时先删除旧的 line items，
        # 再按照 TikTok 当前数据重新写入。
        # -------------------------------------------------

        cur.execute(
            f"""
            DELETE FROM {ITEMS_TABLE}
            WHERE order_id = %s
            """,
            (
                order.get(
                    "id"
                ),
            )
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

                        "order_id":
                            order.get(
                                "id"
                            )
                    }

                    for li
                    in line_items
                ]
            )


# =========================================================
# Backfill query
# =========================================================

def get_orders_needing_refresh(
    conn,
    limit: int = 200
) -> list:

    cancelled_placeholders = (
        ",".join(
            ["%s"]
            *
            len(
                CANCELLED_STATUSES
            )
        )
    )


    with conn.cursor() as cur:

        cur.execute(
            f"""
            SELECT order_id

            FROM {ORDERS_TABLE}

            WHERE UPPER(status)
                NOT IN (
                    {cancelled_placeholders}
                )

              AND (

                    (
                        buyer_email IS NULL
                        AND
                        recipient_name IS NULL
                    )

                    OR

                    tracking_number IS NULL
                  )

              AND refresh_attempts < %s

              AND (

                    synced_at IS NULL

                    OR

                    (
                        refresh_attempts < %s
                        AND
                        synced_at <
                        DATE_SUB(
                            NOW(),
                            INTERVAL %s MINUTE
                        )
                    )

                    OR

                    (
                        refresh_attempts >= %s
                        AND
                        synced_at <
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

                limit
            )
        )


        return [
            row["order_id"]
            for row
            in cur.fetchall()
        ]


# =========================================================
# Stuck orders
# =========================================================

def get_stuck_orders(
    conn
) -> list:

    cancelled_placeholders = (
        ",".join(
            ["%s"]
            *
            len(
                CANCELLED_STATUSES
            )
        )
    )


    with conn.cursor() as cur:

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

            WHERE UPPER(status)
                NOT IN (
                    {cancelled_placeholders}
                )

              AND (

                    (
                        buyer_email IS NULL
                        AND
                        recipient_name IS NULL
                    )

                    OR

                    tracking_number IS NULL
                  )

              AND refresh_attempts >= %s

            ORDER BY create_time ASC
            """,

            (
                *CANCELLED_STATUSES,
                MAX_REFRESH_ATTEMPTS
            )
        )


        return cur.fetchall()


# =========================================================
# Get one order summary
# =========================================================

def get_order_summary(
    conn,
    order_id: str
):

    with conn.cursor() as cur:

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

            (
                order_id,
            )
        )


        return cur.fetchone()