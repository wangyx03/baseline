"""TikTok Shop product catalog persistence (product, SKU, sync, webhook)."""
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db


def get_connection():
    return get_db()


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
# Local staging list for new listings. A row lives here only until it is
# created as a TikTok draft; on success the row is deleted (the product is
# then in tkproducts / tkproducts_skus). Rows never hold TikTok-side truth.
'''CREATE TABLE IF NOT EXISTS tkproducts_listing (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 store_id SMALLINT UNSIGNED NOT NULL,
 seller_sku VARCHAR(128) NOT NULL,
 title VARCHAR(255) NOT NULL,
 price DECIMAL(12,2) NOT NULL,
 quantity INT UNSIGNED NOT NULL,
 isbn VARCHAR(20) NULL,
 image VARCHAR(1000) NULL,
 status ENUM('NEW','DUPLICATE','QUEUED','PROCESSING','FAILED','SUGGESTED')
   NOT NULL DEFAULT 'NEW',
 problem_stage ENUM('DUPLICATE','CHECK','DIAGNOSIS','IMAGE','CREATE') NULL,
 error_message VARCHAR(1000) NULL,
 issues JSON NULL,
 ignore_suggestions TINYINT(1) NOT NULL DEFAULT 0,
 duplicate_product_id VARCHAR(64) NULL,
 request_id VARCHAR(128) NULL,
 attempts INT UNSIGNED NOT NULL DEFAULT 0,
 locked_at DATETIME NULL,
 upload_tag VARCHAR(255) NULL,
 created_by VARCHAR(64) NULL,
 submitted_at DATETIME NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uq_tkproducts_listing_sku (store_id, seller_sku),
 KEY idx_tkproducts_listing_status (store_id, status),
 KEY idx_tkproducts_listing_queue (status, id),
 CONSTRAINT fk_tkproducts_listing_store FOREIGN KEY (store_id) REFERENCES stores(store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci'''
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


# ======================================================================
# Listing staging list (tkproducts_listing)
#
# Lifecycle of a row:
#   CSV upload / UI edit      -> NEW        (or rejected if SKU already on TikTok)
#   submit (no DUPLICATE left)-> QUEUED
#   worker claims             -> PROCESSING
#   worker result             -> FAILED | SUGGESTED | DUPLICATE  (stays, editable)
#                             -> row DELETED                     (TikTok draft created)
#
# All listing functions below commit their own transaction.
# ======================================================================

LISTING_COLUMNS = ('seller_sku', 'title', 'price', 'quantity', 'isbn', 'image')
LISTING_EDITABLE = ('NEW', 'DUPLICATE', 'FAILED', 'SUGGESTED')     # not QUEUED/PROCESSING
LISTING_SUBMITTABLE = ('NEW', 'FAILED', 'SUGGESTED')
LISTING_PROBLEMS = ('DUPLICATE', 'FAILED', 'SUGGESTED')
TITLE_MAX = 255
_STALE_MINUTES = 10


def _json_load(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8')
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _listing_row(row):
    if row is None:
        return None
    row = dict(row)
    row['issues'] = _json_load(row.get('issues'))
    row['ignore_suggestions'] = bool(row.get('ignore_suggestions'))
    return row


def _placeholders(values):
    return ','.join(['%s'] * len(values))


def validate_listing_row(row):
    """Normalize one CSV/UI row. Returns (clean_dict, None) or (None, error_text)."""
    def text(key):
        value = row.get(key)
        return '' if value is None else str(value).strip()

    sku = text('seller_sku')
    if not sku:
        return None, 'seller_sku is empty'
    if len(sku) > 128:
        return None, 'seller_sku longer than 128 characters'
    title = text('title')
    if not title:
        return None, 'title is empty'
    if len(title) > TITLE_MAX:
        return None, f'title longer than {TITLE_MAX} characters ({len(title)})'
    try:
        price = Decimal(text('price')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None, f"price is not a number: {text('price')!r}"
    if price <= 0:
        return None, 'price must be greater than 0'
    try:
        qty_dec = Decimal(text('quantity'))
        if qty_dec != qty_dec.to_integral_value():
            raise ValueError
        quantity = int(qty_dec)
    except (InvalidOperation, ValueError):
        return None, f"quantity is not a whole number: {text('quantity')!r}"
    if quantity < 0:
        return None, 'quantity must be 0 or more'
    isbn = text('isbn').replace('-', '').replace(' ', '') or None
    if isbn and len(isbn) > 20:
        return None, 'isbn longer than 20 characters'
    image = text('image') or None
    if image and len(image) > 1000:
        return None, 'image longer than 1000 characters'
    return {'seller_sku': sku, 'title': title, 'price': price, 'quantity': quantity,
            'isbn': isbn, 'image': image}, None


def find_existing_skus(conn, store_id, seller_skus):
    """{seller_sku: product_id} for SKUs already on TikTok per the local mirror.

    Products whose status is DELETED do not count.
    """
    skus = list(dict.fromkeys(s for s in seller_skus if s))
    found = {}
    cur = conn.cursor()
    try:
        for start in range(0, len(skus), 500):
            chunk = skus[start:start + 500]
            cur.execute(f"""SELECT s.seller_sku, s.product_id
                FROM tkproducts_skus s
                JOIN tkproducts p ON p.store_id=s.store_id AND p.product_id=s.product_id
                WHERE s.store_id=%s AND s.seller_sku IN ({_placeholders(chunk)})
                  AND COALESCE(UPPER(p.status),'') <> 'DELETED'""", (store_id, *chunk))
            for sku, product_id in cur.fetchall():
                found.setdefault(sku, product_id)
        return found
    finally:
        cur.close()


def import_listing_rows(conn, store_id, rows, upload_tag=None, created_by=None):
    """Import CSV rows into the staging list.

    - invalid rows             -> not imported, reported in 'invalid'
    - same SKU twice in file   -> last one wins, earlier lines reported in 'file_duplicates'
    - SKU already on TikTok    -> not imported, reported in 'duplicates'
    - SKU already in the list  -> row overwritten and reset to NEW ('updated'),
                                  unless it is QUEUED/PROCESSING ('busy')
    """
    result = {'imported': 0, 'updated': 0, 'invalid': [], 'file_duplicates': [],
              'duplicates': [], 'busy': []}
    clean_by_sku = {}
    line_by_sku = {}
    for line_no, raw in enumerate(rows, start=2):        # line 1 is the CSV header
        clean, error = validate_listing_row(raw)
        if error:
            result['invalid'].append({'line': line_no,
                                      'seller_sku': str(raw.get('seller_sku') or '').strip(),
                                      'error': error})
            continue
        sku = clean['seller_sku']
        if sku in clean_by_sku:
            result['file_duplicates'].append({'line': line_by_sku[sku], 'seller_sku': sku,
                                              'error': f'same seller_sku again on line {line_no}; '
                                                       'the later line was used'})
        clean_by_sku[sku] = clean
        line_by_sku[sku] = line_no

    try:
        existing = find_existing_skus(conn, store_id, clean_by_sku.keys())
        for sku, product_id in existing.items():
            result['duplicates'].append({'line': line_by_sku[sku], 'seller_sku': sku,
                                         'product_id': product_id})
        pending = [sku for sku in clean_by_sku if sku not in existing]

        cur = conn.cursor()
        try:
            current = {}
            for start in range(0, len(pending), 500):
                chunk = pending[start:start + 500]
                cur.execute(f"""SELECT seller_sku, status FROM tkproducts_listing
                    WHERE store_id=%s AND seller_sku IN ({_placeholders(chunk)}) FOR UPDATE""",
                    (store_id, *chunk))
                current.update(dict(cur.fetchall()))
            for sku in pending:
                row = clean_by_sku[sku]
                status = current.get(sku)
                if status in ('QUEUED', 'PROCESSING'):
                    result['busy'].append({'line': line_by_sku[sku], 'seller_sku': sku,
                                           'status': status})
                    continue
                cur.execute("""INSERT INTO tkproducts_listing
                    (store_id,seller_sku,title,price,quantity,isbn,image,upload_tag,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE title=VALUES(title), price=VALUES(price),
                      quantity=VALUES(quantity), isbn=VALUES(isbn), image=VALUES(image),
                      upload_tag=VALUES(upload_tag), created_by=VALUES(created_by),
                      status='NEW', problem_stage=NULL, error_message=NULL, issues=NULL,
                      ignore_suggestions=0, duplicate_product_id=NULL, request_id=NULL,
                      attempts=0, locked_at=NULL, submitted_at=NULL""",
                    (store_id, sku, row['title'], row['price'], row['quantity'],
                     row['isbn'], row['image'], upload_tag, created_by))
                result['updated' if status else 'imported'] += 1
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def list_listing(conn, store_id=None, statuses=None, upload_tag=None, limit=2000):
    where, args = [], []
    if store_id is not None:
        where.append('store_id=%s')
        args.append(store_id)
    if statuses:
        where.append(f'status IN ({_placeholders(statuses)})')
        args.extend(statuses)
    if upload_tag:
        where.append('upload_tag=%s')
        args.append(upload_tag)
    sql = 'SELECT * FROM tkproducts_listing'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id LIMIT %s'
    args.append(int(limit))
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, args)
        return [_listing_row(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.commit()          # end the read snapshot so the next read sees fresh data


def listing_counts(conn, store_id=None):
    cur = conn.cursor()
    try:
        if store_id is None:
            cur.execute('SELECT status, COUNT(*) FROM tkproducts_listing GROUP BY status')
        else:
            cur.execute('SELECT status, COUNT(*) FROM tkproducts_listing WHERE store_id=%s '
                        'GROUP BY status', (store_id,))
        return {status: count for status, count in cur.fetchall()}
    finally:
        cur.close()
        conn.commit()


def update_listing_row(conn, listing_id, changes):
    """Edit one row from the UI. Resets it to NEW (or DUPLICATE if the new SKU
    is already on TikTok) and clears any previous result and 'ignore' choice.
    Raises ValueError for invalid input, busy rows or a SKU clash in the list."""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute('SELECT * FROM tkproducts_listing WHERE id=%s FOR UPDATE', (listing_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f'listing row {listing_id} not found')
        if row['status'] not in LISTING_EDITABLE:
            raise ValueError(f"row is {row['status']}; wait until processing finishes")
        merged = {k: row[k] for k in LISTING_COLUMNS}
        merged.update({k: v for k, v in changes.items() if k in LISTING_COLUMNS})
        clean, error = validate_listing_row(merged)
        if error:
            raise ValueError(error)
        if clean['seller_sku'] != row['seller_sku']:
            cur.execute('SELECT id FROM tkproducts_listing WHERE store_id=%s AND seller_sku=%s',
                        (row['store_id'], clean['seller_sku']))
            if cur.fetchone():
                raise ValueError(f"seller_sku {clean['seller_sku']} is already in the list")
        dup = find_existing_skus(conn, row['store_id'], [clean['seller_sku']])
        dup_pid = dup.get(clean['seller_sku'])
        cur.execute("""UPDATE tkproducts_listing SET seller_sku=%s, title=%s, price=%s,
              quantity=%s, isbn=%s, image=%s, status=%s, problem_stage=%s,
              error_message=%s, issues=NULL, ignore_suggestions=0,
              duplicate_product_id=%s, request_id=NULL, attempts=0, submitted_at=NULL
            WHERE id=%s""",
            (clean['seller_sku'], clean['title'], clean['price'], clean['quantity'],
             clean['isbn'], clean['image'],
             'DUPLICATE' if dup_pid else 'NEW', 'DUPLICATE' if dup_pid else None,
             f'seller_sku already exists on TikTok (product {dup_pid})' if dup_pid else None,
             dup_pid, listing_id))
        cur.execute('SELECT * FROM tkproducts_listing WHERE id=%s', (listing_id,))
        updated = _listing_row(cur.fetchone())
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def delete_listing_rows(conn, listing_ids):
    """Manual delete from the UI. QUEUED/PROCESSING rows are not deleted."""
    ids = [int(i) for i in listing_ids]
    if not ids:
        return 0
    cur = conn.cursor()
    try:
        cur.execute(f"""DELETE FROM tkproducts_listing WHERE id IN ({_placeholders(ids)})
            AND status NOT IN ('QUEUED','PROCESSING')""", ids)
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def apply_duplicate_check(conn, store_id, duplicates):
    """Set the duplicate flag for a store's editable rows from a full check.

    duplicates: {seller_sku: product_id} = every SKU currently on TikTok
    (mirror lookup plus live TikTok search, done by the caller).
    Rows in the map -> DUPLICATE. Rows previously DUPLICATE but no longer in
    the map (e.g. product deleted on TikTok) -> back to NEW.
    Returns the number of DUPLICATE rows now in the store's list.
    """
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, seller_sku, status FROM tkproducts_listing
            WHERE store_id=%s AND status IN ('NEW','DUPLICATE','FAILED','SUGGESTED') FOR UPDATE""",
            (store_id,))
        for listing_id, sku, status in cur.fetchall():
            pid = duplicates.get(sku)
            if pid:
                cur.execute("""UPDATE tkproducts_listing SET status='DUPLICATE',
                    problem_stage='DUPLICATE', duplicate_product_id=%s,
                    error_message=%s WHERE id=%s""",
                    (pid, f'seller_sku already exists on TikTok (product {pid})', listing_id))
            elif status == 'DUPLICATE':
                cur.execute("""UPDATE tkproducts_listing SET status='NEW', problem_stage=NULL,
                    duplicate_product_id=NULL, error_message=NULL WHERE id=%s""", (listing_id,))
        cur.execute("SELECT COUNT(*) FROM tkproducts_listing WHERE store_id=%s AND status='DUPLICATE'",
                    (store_id,))
        remaining = cur.fetchone()[0]
        conn.commit()
        return remaining
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def submit_listing(conn, store_id, listing_ids, ignore_suggestions=False):
    """Queue rows for the worker. Refused while any DUPLICATE row remains in
    the store's list. Only NEW / FAILED / SUGGESTED rows are queued.
    Returns {'queued': n, 'duplicates': [sku, ...], 'skipped': [{id, status}]}.
    """
    ids = [int(i) for i in listing_ids]
    result = {'queued': 0, 'duplicates': [], 'skipped': []}
    cur = conn.cursor()
    try:
        cur.execute("""SELECT seller_sku FROM tkproducts_listing
            WHERE store_id=%s AND status='DUPLICATE' FOR UPDATE""", (store_id,))
        result['duplicates'] = [r[0] for r in cur.fetchall()]
        if result['duplicates'] or not ids:
            conn.commit()
            return result
        cur.execute(f"""SELECT id, status FROM tkproducts_listing
            WHERE store_id=%s AND id IN ({_placeholders(ids)}) FOR UPDATE""", (store_id, *ids))
        rows = cur.fetchall()
        ok = [rid for rid, status in rows if status in LISTING_SUBMITTABLE]
        result['skipped'] = [{'id': rid, 'status': status} for rid, status in rows
                             if status not in LISTING_SUBMITTABLE]
        if ok:
            cur.execute(f"""UPDATE tkproducts_listing SET status='QUEUED',
                ignore_suggestions=GREATEST(ignore_suggestions,%s),
                attempts=0, locked_at=NULL, submitted_at=UTC_TIMESTAMP()
                WHERE id IN ({_placeholders(ok)})""", (1 if ignore_suggestions else 0, *ok))
            result['queued'] = len(ok)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def claim_listing(conn):
    """Worker: claim one QUEUED row (or a PROCESSING row whose worker died)."""
    cur = conn.cursor(dictionary=True)
    try:
        if conn.in_transaction:
            conn.rollback()
        conn.start_transaction()
        cur.execute(f"""SELECT * FROM tkproducts_listing
            WHERE status='QUEUED'
               OR (status='PROCESSING' AND locked_at < UTC_TIMESTAMP() - INTERVAL {_STALE_MINUTES} MINUTE)
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""")
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE tkproducts_listing SET status='PROCESSING',
                locked_at=UTC_TIMESTAMP(), attempts=attempts+1 WHERE id=%s""", (row['id'],))
            row['status'] = 'PROCESSING'
            row['attempts'] += 1
        conn.commit()
        return _listing_row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def finish_listing_problem(conn, listing_id, status, stage, message,
                           issues=None, request_id=None, duplicate_product_id=None):
    """Worker: leave the row in the list as FAILED / SUGGESTED / DUPLICATE."""
    if status not in LISTING_PROBLEMS:
        raise ValueError(f'invalid problem status {status}')
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE tkproducts_listing SET status=%s, problem_stage=%s,
              error_message=%s, issues=%s, request_id=%s, duplicate_product_id=%s,
              locked_at=NULL
            WHERE id=%s AND status='PROCESSING'""",
            (status, stage, (str(message)[:1000] if message else None),
             json.dumps(issues, ensure_ascii=False) if issues is not None else None,
             request_id, duplicate_product_id, listing_id))
        changed = cur.rowcount
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def finish_listing_success(conn, listing_id):
    """Worker: TikTok draft created -> remove the row from the staging list."""
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM tkproducts_listing WHERE id=%s AND status='PROCESSING'",
                    (listing_id,))
        changed = cur.rowcount
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def export_problem_rows(conn, store_id, statuses=LISTING_PROBLEMS):
    """Rows for the 'download problems' CSV: upload columns + problem_type + reason."""
    out = []
    for row in list_listing(conn, store_id=store_id, statuses=list(statuses)):
        out.append({
            'seller_sku': row['seller_sku'],
            'title': row['title'],
            'price': str(row['price']),
            'quantity': row['quantity'],
            'isbn': row['isbn'] or '',
            'image': row['image'] or '',
            'problem_type': row['status'],
            'reason': row['error_message'] or '',
        })
    return out
