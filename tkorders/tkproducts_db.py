"""TikTok Shop product catalog persistence (product, SKU, sync, webhook)."""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from tkorders_db import get_connection

SCHEMA = [
'''CREATE TABLE IF NOT EXISTS tkproducts (
 store_id SMALLINT UNSIGNED NOT NULL,
 product_id VARCHAR(64) NOT NULL,
 product_name VARCHAR(500),
 category_id VARCHAR(64),
 status VARCHAR(64),
 selling_format VARCHAR(64),
 image_url TEXT,
 create_time DATETIME NULL,
 update_time DATETIME NULL,
 synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 PRIMARY KEY (store_id, product_id),
 KEY idx_tkproducts_status (store_id,status),
 KEY idx_tkproducts_updated (store_id,update_time),
 CONSTRAINT fk_tkproducts_store FOREIGN KEY (store_id) REFERENCES stores(store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci''',
'''CREATE TABLE IF NOT EXISTS tkproducts_skus (
 store_id SMALLINT UNSIGNED NOT NULL,
 product_id VARCHAR(64) NOT NULL,
 sku_id VARCHAR(64) NOT NULL,
 seller_sku VARCHAR(128),
 sku_name VARCHAR(255),
 inventory JSON NULL,
 retail_price DECIMAL(12,2),
 starting_bid_price DECIMAL(12,2),
 stock INT UNSIGNED,
 currency VARCHAR(10),
 synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 PRIMARY KEY (store_id, sku_id),
 KEY idx_tkproducts_skus_product (store_id,product_id),
 KEY idx_tkproducts_skus_seller (store_id,seller_sku),
 CONSTRAINT fk_tkproducts_skus_product FOREIGN KEY (store_id,product_id)
 REFERENCES tkproducts(store_id,product_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci'''
,
'''CREATE TABLE IF NOT EXISTS tkproducts_sync_state (
 store_id SMALLINT UNSIGNED NOT NULL PRIMARY KEY,
 last_full_sync_at DATETIME NULL,
 last_sync_at DATETIME NULL,
 last_product_count INT UNSIGNED NOT NULL DEFAULT 0,
 last_sku_count INT UNSIGNED NOT NULL DEFAULT 0,
 CONSTRAINT fk_tkproducts_sync_store FOREIGN KEY (store_id) REFERENCES stores(store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
'''CREATE TABLE IF NOT EXISTS tkproducts_webhook_inbox (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 store_id SMALLINT UNSIGNED NOT NULL,
 notification_id VARCHAR(128) NULL,
 event_type VARCHAR(128) NULL,
 product_id VARCHAR(64) NULL,
 payload JSON NOT NULL,
 status ENUM('PENDING','PROCESSING','DONE','IGNORED','FAILED') NOT NULL DEFAULT 'PENDING',
 attempts INT UNSIGNED NOT NULL DEFAULT 0,
 next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 locked_at DATETIME NULL,
 error_message VARCHAR(500) NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uq_tkproducts_notification (store_id,notification_id),
 KEY idx_tkproducts_ready (status,next_attempt_at),
 CONSTRAINT fk_tkproducts_webhook_store FOREIGN KEY (store_id) REFERENCES stores(store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''
]

def init_schema(conn):
    cur = conn.cursor()
    try:
        for statement in SCHEMA:
            cur.execute(statement)
        conn.commit()
    finally:
        cur.close()

def dt(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None) if ts else None
    except (ValueError, TypeError, OverflowError):
        return None

def money(value):
    try:
        return Decimal(str(value)) if value is not None and value != '' else None
    except (InvalidOperation, ValueError):
        return None

def image_url(product):
    images = product.get('main_images') or product.get('images') or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get('url') or first.get('uri')
        if isinstance(first, str):
            return first
    return None

def save_product(conn, store_id, product):
    product_id = str(product['id'])
    category = product.get('category') or {}
    if not isinstance(category, dict):
        category = {}
    cur = conn.cursor()
    try:
        cur.execute('''INSERT INTO tkproducts
            (store_id,product_id,product_name,category_id,status,selling_format,image_url,create_time,update_time)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE product_name=VALUES(product_name),
            category_id=VALUES(category_id),status=VALUES(status),
            selling_format=VALUES(selling_format),image_url=COALESCE(VALUES(image_url),image_url),
            create_time=COALESCE(VALUES(create_time),create_time),
            update_time=COALESCE(VALUES(update_time),update_time)''',
            (store_id, product_id, product.get('title'),
             str(product.get('category_id') or category.get('id') or '') or None,
             product.get('status'), product.get('selling_format'), image_url(product),
             dt(product.get('create_time')), dt(product.get('update_time'))))
        skus = product.get('skus') or []
        for sku in skus:
            if not sku.get('id'):
                continue
            price = sku.get('price') or {}
            inv = sku.get('inventory') or []
            if not isinstance(inv, list):
                raise ValueError(f"Unexpected inventory structure for SKU {sku['id']}: expected list")
            inventory_rows = [x for x in inv if isinstance(x, dict)]
            qty = sum(int(x.get('quantity') or 0) for x in inventory_rows)
            inventory_json = json.dumps(inv, ensure_ascii=False)

            cur.execute('''INSERT INTO tkproducts_skus
                (store_id,product_id,sku_id,seller_sku,sku_name,inventory,
                 retail_price,starting_bid_price,stock,currency)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE product_id=VALUES(product_id),seller_sku=VALUES(seller_sku),
                sku_name=VALUES(sku_name),inventory=VALUES(inventory),
                retail_price=VALUES(retail_price),
                starting_bid_price=VALUES(starting_bid_price),stock=VALUES(stock),currency=VALUES(currency)''',
                (store_id,product_id,str(sku['id']),sku.get('seller_sku'),sku.get('sku_name'),
                 inventory_json,
                 money(price.get('sale_price') or price.get('tax_exclusive_price')),
                 money(price.get('starting_bid_price')),qty,price.get('currency')))
        # Keep SKU rows not returned in a partial response; no destructive reconciliation.
        return len(skus)
    finally:
        cur.close()


def mark_sync(conn, store_id, products, skus):
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO tkproducts_sync_state
            (store_id,last_full_sync_at,last_sync_at,last_product_count,last_sku_count)
            VALUES (%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s,%s)
            ON DUPLICATE KEY UPDATE last_full_sync_at=UTC_TIMESTAMP(),
            last_sync_at=UTC_TIMESTAMP(),last_product_count=VALUES(last_product_count),
            last_sku_count=VALUES(last_sku_count)""",(store_id,products,skus))
    finally:
        cur.close()

def enqueue_webhook(conn, store_id, payload):
    import json
    data = payload.get('data') or {}
    if not isinstance(data,dict): data = {}
    notification = payload.get('tts_notification_id')
    event_type = str(payload.get('type') or payload.get('event_type') or '')
    product_id = data.get('product_id') or data.get('id')
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO tkproducts_webhook_inbox
            (store_id,notification_id,event_type,product_id,payload)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE id=id""",
            (store_id,str(notification) if notification else None,event_type,
             str(product_id) if product_id else None,json.dumps(payload,ensure_ascii=False)))
    finally:
        cur.close()

def claim_webhook(conn):
    cur = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()
        cur.execute("""SELECT id,store_id,product_id,payload,attempts
            FROM tkproducts_webhook_inbox
            WHERE (status IN ('PENDING','FAILED') AND next_attempt_at<=UTC_TIMESTAMP())
               OR (status='PROCESSING' AND locked_at<UTC_TIMESTAMP()-INTERVAL 10 MINUTE)
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""")
        row=cur.fetchone()
        if row:
            cur.execute("""UPDATE tkproducts_webhook_inbox SET status='PROCESSING',
                locked_at=UTC_TIMESTAMP(),attempts=attempts+1 WHERE id=%s""",(row['id'],))
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

def finish_webhook(conn, inbox_id, status, error=None, retry_seconds=0):
    cur=conn.cursor()
    try:
        cur.execute("""UPDATE tkproducts_webhook_inbox SET status=%s,error_message=%s,
            locked_at=NULL,next_attempt_at=UTC_TIMESTAMP()+INTERVAL %s SECOND
            WHERE id=%s""",(status,str(error)[:500] if error else None,retry_seconds,inbox_id))
        conn.commit()
    finally:
        cur.close()
