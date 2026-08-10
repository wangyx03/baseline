#!/usr/bin/env python3
"""
db.py

MySQL layer for the TikTok order sync pipeline. Writes into your existing
"olselling" database, alongside oms_inventory.py's inventory_snapshot table —
does not create a new database, just two new tables:

  tkorders_tu       - one row per order (20 columns, order_id primary key)
  tkorders_items_tu - one row per SKU line item (order_id foreign key, since an
                   order could in principle contain more than one line item
                   even though today it's always 1)

The polling watermark (last_check) is NOT in this database — it's a local
last_check.json file next to the scripts (see tiktokorders.py). Trade-off:
writing the file and writing the DB are two separate operations, not one
transaction, so a crash between them could leave them slightly out of sync.
In practice this is low-risk because every DB write here is an idempotent
upsert — worst case after a crash is re-fetching a batch of orders that were
already stored, which just re-writes the same rows, not duplicates or data loss.

Design summary (from design discussion):
- One database, never deleted, tables split by "what" not by month. If it
  ever needs archiving, use MySQL's native PARTITION BY RANGE on create_time
  instead of separate per-month databases.
- Buyer info (buyer_email / recipient_name / recipient_phone / recipient_address)
  is only visible ~1 hour after order creation (buyer return window). Tracking
  info (tracking_number / shipping_provider) only appears ~1-3 days later,
  after the buyer confirms. Rather than a separate status column, "still
  missing" is just detected by these columns being NULL — except for
  cancelled orders, which will NEVER get this data, so the backfill query
  excludes them by status instead of endlessly retrying.
- Every write (initial poll capture, backfill refresh, or manual --refresh)
  goes through upsert_order(), which:
    - upserts the order row + replaces its line_items in one transaction
    - uses COALESCE so a refresh that comes back empty-handed again doesn't
      erase buyer/tracking data captured on a previous pass
    - only increments refresh_attempts when is_refresh=True, so the very
      first capture at poll time doesn't count as a "retry"
    - synced_at auto-updates on every write (MySQL's ON UPDATE
      CURRENT_TIMESTAMP) and doubles as the backoff clock for the backfill
      query below — no separate "last_refreshed_at" column needed

Needs these in .env (next to the scripts) — reusing your existing DB:
  DB_HOST=127.0.0.1
  DB_PORT=3306
  DB_USER=root
  DB_PASSWORD=...
  DB_NAME=olselling

pip install pymysql --break-system-packages
"""

import os

import re

import pymysql
import pymysql.cursors

# How many failed refresh attempts before we stop auto-retrying an order and
# mark it "stuck" (still visible in queries, just excluded from the backfill loop).
MAX_REFRESH_ATTEMPTS = 20

# Two-tier backoff for the backfill query: retry frequently at first, then
# fall back to once/day for orders that have been pending a long time.
BACKOFF_FAST_ATTEMPTS = 5
BACKOFF_FAST_MINUTES = 25   # must be >= your poll/backfill cadence or it'll always match
BACKOFF_SLOW_HOURS = 24

CANCELLED_STATUSES = {"CANCELLED", "CANCELED"}  # TikTok's exact casing unverified — check against a real response

# Table names come from .env, not hardcoded — set TKORDERS_TABLE / TKORDERS_ITEMS_TABLE
# to rename them (e.g. for a second environment sharing the same DB, like the
# "_tu" suffix used here). Table names can't go through pymysql's %s parameter
# placeholders (those only work for values, not identifiers), so they're inserted
# via plain string formatting below — validated against a strict pattern first
# since unlike query parameters, this text lands directly in the SQL.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table_name(env_var: str, default: str) -> str:
    name = os.environ.get(env_var, default)
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"{env_var}={name!r} is not a valid table name (letters/digits/underscore only)")
    return name


ORDERS_TABLE = _safe_table_name("TKORDERS_TABLE", "tkorders_tu")
ITEMS_TABLE = _safe_table_name("TKORDERS_ITEMS_TABLE", "tkorders_items_tu")


def get_connection():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "olselling"),
        charset="utf8mb4",
        autocommit=False,  # we manage transactions explicitly — see upsert_order()
        cursorclass=pymysql.cursors.DictCursor,
    )


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ORDERS_TABLE} (
    order_id              VARCHAR(64)  PRIMARY KEY,
    status                VARCHAR(64),
    create_time           BIGINT,
    update_time           BIGINT,
    warehouse_id          VARCHAR(64),
    warehouse_name        VARCHAR(255),
    shipping_type         VARCHAR(64),
    delivery_option_name  VARCHAR(128),
    rts_sla_time          BIGINT,
    auto_cancel_time      BIGINT,

    -- buyer info: only visible ~1hr after order creation (buyer return window)
    buyer_email           VARCHAR(255),
    buyer_message         TEXT,
    recipient_name        VARCHAR(255),
    recipient_phone       VARCHAR(64),
    recipient_address     TEXT,

    -- tracking info: only appears ~1-3 days later, after buyer confirms
    tracking_number       VARCHAR(128),
    shipping_provider     VARCHAR(128),
    shipping_provider_id  VARCHAR(64),

    refresh_attempts      INT NOT NULL DEFAULT 0,
    synced_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id          VARCHAR(64) NOT NULL,
    product_name      VARCHAR(255),
    sku_id            VARCHAR(64),
    sku_name          VARCHAR(64),   -- e.g. "292" — the batch/round number, not the product name
    seller_sku        VARCHAR(128),
    sale_price        VARCHAR(32),   -- stored as string, matching the API's raw format — cast to DECIMAL when summing
    quantity          INT,
    creator_username  VARCHAR(128),  -- LIVE/affiliate creator, e.g. "vesper_books" — field name unverified, see raw response
    creator_type      VARCHAR(64),   -- e.g. "Seller-signed creator" — field name unverified, see raw response
    FOREIGN KEY (order_id) REFERENCES {ORDERS_TABLE}(order_id) ON DELETE CASCADE,
    INDEX idx_order_id (order_id),
    INDEX idx_seller_sku (seller_sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_schema(conn):
    """Idempotent — safe to call on every startup. Only creates tkorders_tu/tkorders_items_tu;
    never touches other tables already in this shared database (e.g. inventory_snapshot)."""
    with conn.cursor() as cur:
        for statement in SCHEMA.split(";\n\n"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
    conn.commit()


# ---------------------- status helpers ----------------------
def _is_cancelled(order: dict) -> bool:
    return str(order.get("status", "")).upper() in CANCELLED_STATUSES


# ---------------------- upsert ----------------------
def upsert_order(conn, order: dict, line_items: list, is_refresh: bool):
    """Upsert one order + replace its line_items, within the CALLER's transaction
    (this function does not commit — call conn.commit() yourself once you're
    done with the whole batch, so a crash mid-batch rolls back cleanly).

    is_refresh=False -> initial capture at poll time; refresh_attempts stays 0.
    is_refresh=True  -> backfill or manual --refresh call; increments refresh_attempts.

    Buyer/tracking fields use COALESCE(new, old) so a refresh that comes back
    empty-handed again (data still not visible yet) doesn't erase what a
    previous pass already captured.
    """
    addr = order.get("recipient_address") or {}
    # Defensive: exact sub-field names for recipient_address are unverified against
    # a live response — check a few likely candidates.
    recipient_name = addr.get("name") if isinstance(addr, dict) else None
    recipient_phone = addr.get("phone_number") if isinstance(addr, dict) else None
    recipient_address_text = (
        addr.get("full_address") or addr.get("address_detail") or str(addr) if addr else None
    )

    packages = order.get("packages") or []
    first_package = packages[0] if packages else {}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ORDERS_TABLE} (
                order_id, status, create_time, update_time, warehouse_id, warehouse_name,
                shipping_type, delivery_option_name, rts_sla_time, auto_cancel_time,
                buyer_email, buyer_message, recipient_name, recipient_phone, recipient_address,
                tracking_number, shipping_provider, shipping_provider_id,
                refresh_attempts
            ) VALUES (
                %(order_id)s, %(status)s, %(create_time)s, %(update_time)s, %(warehouse_id)s, %(warehouse_name)s,
                %(shipping_type)s, %(delivery_option_name)s, %(rts_sla_time)s, %(auto_cancel_time)s,
                %(buyer_email)s, %(buyer_message)s, %(recipient_name)s, %(recipient_phone)s, %(recipient_address)s,
                %(tracking_number)s, %(shipping_provider)s, %(shipping_provider_id)s,
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
                -- COALESCE: don't let a refresh that found nothing new erase previously captured data
                buyer_email = COALESCE(VALUES(buyer_email), {ORDERS_TABLE}.buyer_email),
                buyer_message = COALESCE(VALUES(buyer_message), {ORDERS_TABLE}.buyer_message),
                recipient_name = COALESCE(VALUES(recipient_name), {ORDERS_TABLE}.recipient_name),
                recipient_phone = COALESCE(VALUES(recipient_phone), {ORDERS_TABLE}.recipient_phone),
                recipient_address = COALESCE(VALUES(recipient_address), {ORDERS_TABLE}.recipient_address),
                tracking_number = COALESCE(VALUES(tracking_number), {ORDERS_TABLE}.tracking_number),
                shipping_provider = COALESCE(VALUES(shipping_provider), {ORDERS_TABLE}.shipping_provider),
                shipping_provider_id = COALESCE(VALUES(shipping_provider_id), {ORDERS_TABLE}.shipping_provider_id),
                refresh_attempts = {ORDERS_TABLE}.refresh_attempts + VALUES(refresh_attempts)
            """,
            {
                "order_id": order.get("id"),
                "status": order.get("status"),
                "create_time": order.get("create_time"),
                "update_time": order.get("update_time"),
                "warehouse_id": order.get("warehouse_id"),
                "warehouse_name": order.get("warehouse_name"),
                "shipping_type": order.get("shipping_type"),
                "delivery_option_name": order.get("delivery_option_name"),
                "rts_sla_time": order.get("rts_sla_time"),
                "auto_cancel_time": order.get("auto_cancel_time"),
                "buyer_email": order.get("buyer_email"),
                "buyer_message": order.get("buyer_message"),
                "recipient_name": recipient_name,
                "recipient_phone": recipient_phone,
                "recipient_address": recipient_address_text,
                "tracking_number": order.get("tracking_number") or first_package.get("tracking_number"),
                "shipping_provider": order.get("shipping_provider") or first_package.get("shipping_provider_name"),
                "shipping_provider_id": order.get("shipping_provider_id"),
                "refresh_attempts": 1 if is_refresh else 0,
            },
        )

        # Line items: delete-then-insert is simplest way to keep them in sync
        # without needing a stable natural key to upsert against.
        cur.execute(f"DELETE FROM {ITEMS_TABLE} WHERE order_id = %s", (order.get("id"),))
        if line_items:
            cur.executemany(
                f"""
                INSERT INTO {ITEMS_TABLE} (
                    order_id, product_name, sku_id, sku_name, seller_sku, sale_price, quantity,
                    creator_username, creator_type
                ) VALUES (
                    %(order_id)s, %(product_name)s, %(sku_id)s, %(sku_name)s, %(seller_sku)s, %(sale_price)s, %(quantity)s,
                    %(creator_username)s, %(creator_type)s
                )
                """,
                [{**li, "order_id": order.get("id")} for li in line_items],
            )


# ---------------------- backfill query ----------------------
def get_orders_needing_refresh(conn, limit: int = 200) -> list:
    """Orders where buyer info and/or tracking info still looks missing (NULL),
    excluding:
    - cancelled orders (they will NEVER get this data — no point retrying)
    - orders that hit MAX_REFRESH_ATTEMPTS (considered 'stuck' — see get_stuck_orders)
    - orders refreshed too recently (two-tier backoff via synced_at, which
      auto-updates on every write — see upsert_order)
    """
    cancelled_placeholders = ",".join(["%s"] * len(CANCELLED_STATUSES))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT order_id FROM {ORDERS_TABLE}
            WHERE UPPER(status) NOT IN ({cancelled_placeholders})
              AND (
                    (buyer_email IS NULL AND recipient_name IS NULL)
                    OR tracking_number IS NULL
                  )
              AND refresh_attempts < %s
              AND (
                    synced_at IS NULL
                    OR (refresh_attempts < %s AND synced_at < DATE_SUB(NOW(), INTERVAL %s MINUTE))
                    OR (refresh_attempts >= %s AND synced_at < DATE_SUB(NOW(), INTERVAL %s HOUR))
                  )
            ORDER BY create_time ASC
            LIMIT %s
            """,
            (*CANCELLED_STATUSES, MAX_REFRESH_ATTEMPTS, BACKOFF_FAST_ATTEMPTS, BACKOFF_FAST_MINUTES,
             BACKOFF_FAST_ATTEMPTS, BACKOFF_SLOW_HOURS, limit),
        )
        return [row["order_id"] for row in cur.fetchall()]


def get_stuck_orders(conn) -> list:
    """Orders that gave up automatic retrying — worth a human glance."""
    cancelled_placeholders = ",".join(["%s"] * len(CANCELLED_STATUSES))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT order_id, status, buyer_email, recipient_name, tracking_number, refresh_attempts, synced_at
            FROM {ORDERS_TABLE}
            WHERE UPPER(status) NOT IN ({cancelled_placeholders})
              AND (
                    (buyer_email IS NULL AND recipient_name IS NULL)
                    OR tracking_number IS NULL
                  )
              AND refresh_attempts >= %s
            ORDER BY create_time ASC
            """,
            (*CANCELLED_STATUSES, MAX_REFRESH_ATTEMPTS),
        )
        return cur.fetchall()


def get_order_summary(conn, order_id: str):
    """For printing after a manual --refresh."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT order_id, status, buyer_email, recipient_name, recipient_phone,
                   tracking_number, refresh_attempts, synced_at
            FROM {ORDERS_TABLE} WHERE order_id = %s
            """,
            (order_id,),
        )
        return cur.fetchone()