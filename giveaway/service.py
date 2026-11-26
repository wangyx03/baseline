"""Giveaway book pool: random books for TikTok ZERO_LOTTERY order lines.

Tables (created by hand, see the CREATE statements in giveaway_web.py's docstring):
    giveaway_pool         isbn (-> book_sku), total_qty, active, note
    giveaway_assignments  one row per lottery book: (store_id, order_id, line_item_id,
                          unit_no) -> isbn; locked = 1 once pushed to OMS / exported

A lottery line is an order line of an order with order_type ZERO_LOTTERY and no
seller SKU. Each unit of its quantity gets one book (unit_no 1..quantity).

Rules
- remaining = total_qty - books assigned to orders that are not cancelled
- a book can be drawn only when its pool Left > 0 AND OMS still has a sellable copy
  (inventory_snapshot 'Good' available_stock, minus books drawn but not yet pushed)
- random draw weighted by Left (like drawing one book from a bag of what is left)
- never two copies of the same book in one package (tracking number, else the order)
- avoid books the buyer already received or bought; relaxed when nothing else is left
- manual choice: any active pool book (remaining 0 is allowed after confirmation)
- unlocked assignments whose book left the pool (deleted / inactive) are drawn again
- locked assignments never change here (unlock in the database if really needed)

Every function that writes opens its own connection and commits, so callers can use
it from inside a read-only request.
"""
import random
import re
from collections import defaultdict

from db import get_db

LOTTERY_TYPE = 'ZERO_LOTTERY'
CANCELLED = ('CANCELLED', 'CANCELED')
DRAW_LOCK = 'giveaway_draw'

# Lottery line = ZERO_LOTTERY order + no seller SKU (aliases o = tkorders, i = tkorders_items).
LOTTERY_LINE_SQL = (f"o.order_type = '{LOTTERY_TYPE}' "
                    "AND COALESCE(TRIM(i.seller_sku), '') = ''")
NOT_CANCELLED_SQL = "UPPER(COALESCE(o.status, '')) NOT IN ('CANCELLED', 'CANCELED')"


class GiveawayError(Exception):
    pass


def _ph(values):
    return ','.join(['%s'] * len(values))


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

def pool_rows(cur):
    """Pool with book_sku details, used and remaining counts."""
    return _rows(cur, f"""
        SELECT p.isbn, p.total_qty, p.active, p.note, p.created_by, p.created_at, p.updated_at,
               b.book_title, b.book_author, b.book_format, b.spice_type,
               COALESCE(u.used, 0) AS used,
               CAST(p.total_qty AS SIGNED) - COALESCE(u.used, 0) AS remaining
        FROM giveaway_pool p
        LEFT JOIN book_sku b ON b.isbn = p.isbn
        LEFT JOIN ({_used_sql()}) u ON u.isbn = p.isbn
        ORDER BY p.active DESC, remaining DESC, p.isbn""")


def _used_sql():
    """isbn -> books assigned to orders that are not cancelled."""
    return f"""SELECT a.isbn, COUNT(*) AS used
               FROM giveaway_assignments a
               JOIN tkorders o ON o.store_id = a.store_id AND o.order_id = a.order_id
               WHERE {NOT_CANCELLED_SQL}
               GROUP BY a.isbn"""


def _remaining(cur):
    """{isbn: remaining} for active pool books (can be 0 or negative)."""
    rows = _rows(cur, f"""
        SELECT p.isbn, CAST(p.total_qty AS SIGNED) - COALESCE(u.used, 0) AS remaining
        FROM giveaway_pool p LEFT JOIN ({_used_sql()}) u ON u.isbn = p.isbn
        WHERE p.active = 1""")
    return {r['isbn']: int(r['remaining']) for r in rows}


def _oms_free(cur):
    """{isbn: OMS copies still free} for active pool books: sellable OMS stock ('Good',
    all warehouses, from inventory_snapshot) minus books drawn but not pushed yet
    (unlocked; a pushed book is already counted by OMS). Missing in OMS = 0."""
    rows = _rows(cur, f"""
        SELECT p.isbn,
               COALESCE(s.available, 0) - COALESCE(u.pending, 0) AS free
        FROM giveaway_pool p
        LEFT JOIN (SELECT sku, SUM(available_stock) AS available FROM inventory_snapshot
                   WHERE stock_type = 'Good' GROUP BY sku) s ON s.sku = p.isbn
        LEFT JOIN (SELECT a.isbn, COUNT(*) AS pending FROM giveaway_assignments a
                   JOIN tkorders o ON o.store_id = a.store_id AND o.order_id = a.order_id
                   WHERE a.locked = 0 AND {NOT_CANCELLED_SQL} GROUP BY a.isbn) u ON u.isbn = p.isbn
        WHERE p.active = 1""")
    return {r['isbn']: int(r['free']) for r in rows}


def normalize_isbn(value):
    return ''.join(ch for ch in str(value or '') if ch.isalnum()).upper()


def looks_like_isbn(isbn):
    """10 or 13 characters: digits, an ISBN-10 may end in X."""
    return bool(re.fullmatch(r'\d{13}|\d{9}[\dX]', isbn or ''))


def save_pool(items, mode, user, notes=None):
    """Add/update pool books. mode 'set' = total_qty becomes qty, 'add' = total_qty += qty.
    notes: optional {isbn: note} (only non-empty notes overwrite).
    Returns {'saved': n, 'missing': [isbns not in book_sku]}."""
    notes = notes or {}
    if mode not in ('set', 'add'):
        raise GiveawayError('mode must be set or add')
    if not items:
        return {'saved': 0, 'missing': []}
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        isbns = [i for i, _ in items]
        known = {r['isbn'] for r in _rows(cur, f'SELECT isbn FROM book_sku WHERE isbn IN ({_ph(isbns)})', isbns)}
        missing = [i for i in isbns if i not in known]
        rows = [(isbn, qty, (notes.get(isbn) or None), user) for isbn, qty in items if isbn in known]
        if rows:
            update = 'total_qty + VALUES(total_qty)' if mode == 'add' else 'VALUES(total_qty)'
            cur.executemany(f"""INSERT INTO giveaway_pool (isbn, total_qty, note, active, created_by)
                                VALUES (%s, %s, %s, 1, %s)
                                ON DUPLICATE KEY UPDATE total_qty = {update},
                                    note = COALESCE(VALUES(note), note)""", rows)
        conn.commit()
        return {'saved': len(rows), 'missing': missing}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def add_book(isbn, qty, note, user):
    """Add one book (quantity replaces an existing one). Raises if not in book_sku."""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute('SELECT isbn FROM book_sku WHERE isbn = %s', (isbn,))
        if not cur.fetchone():
            raise GiveawayError(f'{isbn} is not in book_sku. Add the book there first.')
        cur.execute('SELECT isbn FROM giveaway_pool WHERE isbn = %s', (isbn,))
        existed = cur.fetchone() is not None
        note = (str(note).strip()[:255] or None) if note is not None else None
        cur.execute("""INSERT INTO giveaway_pool (isbn, total_qty, note, active, created_by)
                       VALUES (%s, %s, %s, 1, %s)
                       ON DUPLICATE KEY UPDATE total_qty = VALUES(total_qty),
                           note = COALESCE(VALUES(note), note)""", (isbn, int(qty), note, user))
        conn.commit()
        return {'isbn': isbn, 'total_qty': int(qty), 'existed': existed}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def read_upload(filename, data):
    """Rows of an uploaded .xlsx / .csv -> ([(isbn, qty)], {isbn: note}, [errors]).
    Needs a header row with an isbn column and a quantity (or qty) column."""
    name = (filename or '').lower()
    if name.endswith('.xlsx'):
        import io as _io
        import warnings
        from openpyxl import load_workbook
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
    elif name.endswith('.csv'):
        import csv
        text = data.decode('utf-8-sig', errors='replace')
        rows = [r for r in csv.reader(text.splitlines())]
    else:
        raise GiveawayError('Upload an .xlsx or .csv file.')
    rows = [r for r in rows if any(str(c or '').strip() for c in r)]
    if not rows:
        raise GiveawayError('The file is empty.')
    header = [str(c or '').strip().lower() for c in rows[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    c_isbn, c_qty, c_note = col('isbn', 'sku', 'seller_sku'), col('quantity', 'qty', 'total_qty'), col('note')
    if c_isbn is None or c_qty is None:
        raise GiveawayError('The first row must name the columns: isbn, quantity (optional note).')
    items, notes, errors = {}, {}, []
    for n, r in enumerate(rows[1:], start=2):
        cell = lambda i: r[i] if i is not None and i < len(r) else None
        raw = cell(c_isbn)
        if isinstance(raw, float) and raw.is_integer():
            raw = int(raw)          # Excel number cell: 9781668024782.0
        isbn = normalize_isbn(raw)
        if not looks_like_isbn(isbn):
            errors.append(f'row {n}: bad ISBN "{raw}"')
            continue
        try:
            qty = int(float(cell(c_qty)))
        except (TypeError, ValueError):
            errors.append(f'row {n}: bad quantity "{cell(c_qty)}"')
            continue
        if qty < 0:
            errors.append(f'row {n}: quantity below 0')
            continue
        items[isbn] = qty
        note = str(cell(c_note) or '').strip()[:255]
        if note:
            notes[isbn] = note
    return list(items.items()), notes, errors


def update_pool_book(isbn, total_qty=None, active=None, note=None):
    sets, args = [], []
    if total_qty is not None:
        if int(total_qty) < 0:
            raise GiveawayError('quantity must be 0 or more')
        sets.append('total_qty = %s'); args.append(int(total_qty))
    if active is not None:
        sets.append('active = %s'); args.append(1 if active else 0)
    if note is not None:
        sets.append('note = %s'); args.append(str(note)[:255] or None)
    if not sets:
        return 0
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE giveaway_pool SET {', '.join(sets)} WHERE isbn = %s", args + [isbn])
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def delete_pool_book(isbn):
    """Remove a book from the pool. Its unlocked assignments are dropped (drawn again
    next time); locked ones stay as history."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute('DELETE FROM giveaway_assignments WHERE isbn = %s AND locked = 0', (isbn,))
        released = cur.rowcount
        cur.execute('DELETE FROM giveaway_pool WHERE isbn = %s', (isbn,))
        deleted = cur.rowcount
        conn.commit()
        return {'deleted': deleted, 'released': released}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Lottery lines and assignments
# ---------------------------------------------------------------------------

def lottery_lines(cur, where='1=1', args=()):
    """Lottery lines (not cancelled) with order / package context. `where` uses o / i."""
    return _rows(cur, f"""
        SELECT i.store_id, i.order_id, i.line_item_id, GREATEST(COALESCE(i.quantity, 1), 1) AS quantity,
               i.product_name, NULLIF(TRIM(i.tracking_number), '') AS tracking,
               o.buyer_user_id, o.status, o.create_time, o.warehouse_id
        FROM tkorders_items i
        JOIN tkorders o ON o.store_id = i.store_id AND o.order_id = i.order_id
        WHERE {LOTTERY_LINE_SQL} AND {NOT_CANCELLED_SQL} AND ({where})
        ORDER BY o.create_time, i.order_id, i.line_item_id""", list(args))


def assignments_for(cur, line_keys):
    """{(store_id, order_id, line_item_id): [assignment rows by unit_no]}"""
    keys = list(dict.fromkeys((k[0], str(k[1]), str(k[2])) for k in line_keys))
    out = defaultdict(list)
    for start in range(0, len(keys), 300):
        chunk = keys[start:start + 300]
        rows = _rows(cur, f"""
            SELECT a.store_id, a.order_id, a.line_item_id, a.unit_no, a.isbn, a.locked,
                   a.assigned_by, a.assigned_at, a.locked_at, b.book_title,
                   (p.isbn IS NOT NULL AND p.active = 1) AS in_pool
            FROM giveaway_assignments a
            LEFT JOIN book_sku b ON b.isbn = a.isbn
            LEFT JOIN giveaway_pool p ON p.isbn = a.isbn
            WHERE (a.store_id, a.order_id, a.line_item_id) IN ({','.join(['(%s,%s,%s)'] * len(chunk))})
            ORDER BY a.unit_no""", [v for k in chunk for v in k])
        for r in rows:
            out[(r['store_id'], r['order_id'], r['line_item_id'])].append(r)
    return out


def _package_isbns(cur, line):
    """ISBNs already in the same package: regular lines + other lottery assignments."""
    if line['tracking']:
        cond, args = 'i.store_id = %s AND TRIM(i.tracking_number) = %s', [line['store_id'], line['tracking']]
    else:
        cond, args = 'i.store_id = %s AND i.order_id = %s', [line['store_id'], line['order_id']]
    rows = _rows(cur, f"""
        SELECT i.seller_sku AS isbn FROM tkorders_items i WHERE {cond} AND COALESCE(i.seller_sku, '') <> ''
        UNION
        SELECT a.isbn FROM giveaway_assignments a
        JOIN tkorders_items i ON i.store_id = a.store_id AND i.order_id = a.order_id
             AND i.line_item_id = a.line_item_id
        WHERE {cond}""", args + args)
    return {normalize_isbn(r['isbn']) for r in rows}


def _buyer_isbns(cur, store_id, buyer):
    """ISBNs this buyer already received (giveaway) or bought, in this store."""
    if not buyer:
        return set()
    rows = _rows(cur, """
        SELECT i.seller_sku AS isbn FROM tkorders o
        JOIN tkorders_items i ON i.store_id = o.store_id AND i.order_id = o.order_id
        WHERE o.store_id = %s AND o.buyer_user_id = %s AND COALESCE(i.seller_sku, '') <> ''
        UNION
        SELECT a.isbn FROM tkorders o
        JOIN giveaway_assignments a ON a.store_id = o.store_id AND a.order_id = o.order_id
        WHERE o.store_id = %s AND o.buyer_user_id = %s""", (store_id, buyer, store_id, buyer))
    return {normalize_isbn(r['isbn']) for r in rows}


def _pick(remaining, exclude_hard, exclude_soft, oms_free=None):
    """First pool Left > 0, then OMS stock > 0 (when oms_free is given); weighted by Left.
    Package duplicates are never allowed; buyer repeats only when nothing else is left."""
    for excluded in (exclude_hard | exclude_soft, exclude_hard):
        pool = [(isbn, n) for isbn, n in remaining.items()
                if n > 0 and isbn not in excluded and (oms_free is None or oms_free.get(isbn, 0) > 0)]
        if pool:
            isbns, weights = zip(*pool)
            return random.choices(isbns, weights=weights, k=1)[0]
    return None


def _with_draw_lock(fn):
    """Run fn(conn, cur) under a MySQL named lock so two requests never overdraw the pool."""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute('SELECT GET_LOCK(%s, 20) AS ok', (DRAW_LOCK,))
        if (cur.fetchone() or {}).get('ok') != 1:
            raise GiveawayError('Another giveaway draw is running; try again in a moment.')
        try:
            result = fn(conn, cur)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.execute('SELECT RELEASE_LOCK(%s)', (DRAW_LOCK,))
            cur.fetchall()
    finally:
        cur.close()
        conn.close()


def _drop_invalid(cur, keys):
    """Unlocked assignments whose book is no longer an active pool book are drawn again."""
    if not keys:
        return 0
    removed = 0
    for start in range(0, len(keys), 300):
        chunk = keys[start:start + 300]
        cur.execute(f"""
            DELETE a FROM giveaway_assignments a
            LEFT JOIN giveaway_pool p ON p.isbn = a.isbn AND p.active = 1
            WHERE a.locked = 0 AND p.isbn IS NULL
              AND (a.store_id, a.order_id, a.line_item_id) IN ({','.join(['(%s,%s,%s)'] * len(chunk))})""",
            [v for k in chunk for v in k])
        removed += cur.rowcount
    return removed


def _draw_missing(conn, cur, lines, user, only_units=None):
    """only_units: optional set of (store_id, order_id, line_item_id, unit_no) to limit the draw."""
    keys = [(l['store_id'], l['order_id'], l['line_item_id']) for l in lines]
    stats = {'drawn': 0, 'redrawn': _drop_invalid(cur, keys), 'short': 0}
    existing = assignments_for(cur, keys)
    remaining = _remaining(cur)
    oms_free = _oms_free(cur)
    buyer_cache = {}
    for line in lines:
        key = (line['store_id'], line['order_id'], line['line_item_id'])
        have = {a['unit_no'] for a in existing.get(key, [])}
        missing = [u for u in range(1, int(line['quantity']) + 1) if u not in have
                   and (only_units is None or (*key, u) in only_units)]
        if not missing:
            continue
        package = _package_isbns(cur, line)
        bkey = (line['store_id'], line['buyer_user_id'])
        if bkey not in buyer_cache:
            buyer_cache[bkey] = _buyer_isbns(cur, *bkey)
        for unit in missing:
            isbn = _pick(remaining, package, buyer_cache[bkey], oms_free)
            if not isbn:
                stats['short'] += 1
                continue
            cur.execute("""INSERT INTO giveaway_assignments
                           (store_id, order_id, line_item_id, unit_no, isbn, assigned_by)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (line['store_id'], line['order_id'], line['line_item_id'], unit, isbn, user))
            remaining[isbn] -= 1
            oms_free[isbn] = oms_free.get(isbn, 0) - 1
            package.add(isbn)
            buyer_cache[bkey].add(isbn)
            stats['drawn'] += 1
    return stats


def ensure_for_orders(order_keys, user='auto'):
    """Give every lottery unit of these orders a book (OMS View, push, export).
    order_keys: [(store_id, order_id)]. Returns {'drawn', 'redrawn', 'short'}."""
    keys = list(dict.fromkeys((int(s), str(o)) for s, o in order_keys if o))
    if not keys:
        return {'drawn': 0, 'redrawn': 0, 'short': 0}

    def run(conn, cur):
        total = {'drawn': 0, 'redrawn': 0, 'short': 0}
        for start in range(0, len(keys), 300):
            chunk = keys[start:start + 300]
            lines = lottery_lines(cur, f"(i.store_id, i.order_id) IN ({','.join(['(%s,%s)'] * len(chunk))})",
                                  [v for k in chunk for v in k])
            for k, v in _draw_missing(conn, cur, lines, user).items():
                total[k] += v
        return total
    return _with_draw_lock(run)


def draw_units(units, user='auto'):
    """Draw books for the given units only: [(store_id, order_id, line_item_id, unit_no)]."""
    wanted = {(int(s), str(o), str(l), int(u)) for s, o, l, u in units}
    lines_keys = sorted({k[:3] for k in wanted})
    if not lines_keys:
        return {'drawn': 0, 'redrawn': 0, 'short': 0}

    def run(conn, cur):
        total = {'drawn': 0, 'redrawn': 0, 'short': 0}
        for start in range(0, len(lines_keys), 300):
            chunk = lines_keys[start:start + 300]
            lines = lottery_lines(cur, f"(i.store_id, i.order_id, i.line_item_id) IN ({','.join(['(%s,%s,%s)'] * len(chunk))})",
                                  [v for k in chunk for v in k])
            for k, v in _draw_missing(conn, cur, lines, user, wanted).items():
                total[k] += v
        return total
    return _with_draw_lock(run)


def draw_all(where='1=1', args=(), user='auto'):
    """Draw books for every unassigned lottery unit matching `where` (giveaway page)."""
    return _with_draw_lock(lambda conn, cur: _draw_missing(conn, cur, lottery_lines(cur, where, args), user))


def _unit(cur, store_id, order_id, line_item_id, unit_no):
    lines = lottery_lines(cur, 'i.store_id = %s AND i.order_id = %s AND i.line_item_id = %s',
                          (store_id, order_id, line_item_id))
    if not lines:
        raise GiveawayError('Not a lottery line (or the order is cancelled).')
    line = lines[0]
    if not 1 <= int(unit_no) <= int(line['quantity']):
        raise GiveawayError('Unit number out of range.')
    cur.execute("""SELECT isbn, locked FROM giveaway_assignments
                   WHERE store_id = %s AND order_id = %s AND line_item_id = %s AND unit_no = %s""",
                (store_id, order_id, line_item_id, unit_no))
    current = cur.fetchone()
    if current and current['locked']:
        raise GiveawayError('This book is locked (already pushed to OMS or exported).')
    return line, current


def reroll(store_id, order_id, line_item_id, unit_no, user):
    """Draw a different book for one unlocked unit."""
    def run(conn, cur):
        line, current = _unit(cur, store_id, order_id, line_item_id, unit_no)
        if current:
            cur.execute("""DELETE FROM giveaway_assignments WHERE store_id = %s AND order_id = %s
                           AND line_item_id = %s AND unit_no = %s""",
                        (store_id, order_id, line_item_id, unit_no))
        remaining = _remaining(cur)
        package = _package_isbns(cur, line)
        avoid = _buyer_isbns(cur, store_id, line['buyer_user_id'])
        if current:
            avoid.add(current['isbn'])      # prefer a different book than before
        isbn = _pick(remaining, package, avoid, _oms_free(cur))
        if not isbn:
            raise GiveawayError('No book left in the pool (or in OMS stock) for this order.')
        cur.execute("""INSERT INTO giveaway_assignments
                       (store_id, order_id, line_item_id, unit_no, isbn, assigned_by)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (store_id, order_id, line_item_id, unit_no, isbn, user))
        return isbn
    return _with_draw_lock(run)


def set_manual(store_id, order_id, line_item_id, unit_no, isbn, user, confirm_short=False):
    """Choose the book by hand. Must be an active pool book. Remaining 0 needs confirm_short.
    Returns {'isbn', 'warning'}; warning is set (and nothing saved) when confirmation is needed."""
    isbn = normalize_isbn(isbn)

    def run(conn, cur):
        _unit(cur, store_id, order_id, line_item_id, unit_no)
        cur.execute('SELECT active FROM giveaway_pool WHERE isbn = %s', (isbn,))
        row = cur.fetchone()
        if not row:
            raise GiveawayError(f'{isbn} is not in the giveaway pool.')
        if not row['active']:
            raise GiveawayError(f'{isbn} is inactive in the pool. Activate it first.')
        cur.execute("""SELECT isbn FROM giveaway_assignments WHERE store_id = %s AND order_id = %s
                       AND line_item_id = %s AND unit_no = %s""", (store_id, order_id, line_item_id, unit_no))
        same = (cur.fetchone() or {}).get('isbn') == isbn
        left = _remaining(cur).get(isbn, 0)
        free = _oms_free(cur).get(isbn, 0)
        if not same and not confirm_short:
            problems = []
            if left <= 0:
                problems.append(f'no copies left in the pool ({left})')
            if free <= 0:
                problems.append(f'no free copy in OMS stock ({free})')
            if problems:
                return {'isbn': isbn, 'warning': f"{isbn}: {' and '.join(problems)}. Save anyway?"}
        cur.execute("""INSERT INTO giveaway_assignments
                       (store_id, order_id, line_item_id, unit_no, isbn, assigned_by)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE isbn = VALUES(isbn), assigned_by = VALUES(assigned_by),
                                               assigned_at = CURRENT_TIMESTAMP""",
                    (store_id, order_id, line_item_id, unit_no, isbn, user))
        return {'isbn': isbn, 'warning': None}
    return _with_draw_lock(run)


def lock_orders(order_keys):
    """Lock every assignment of these orders (after a successful push / an export)."""
    keys = list(dict.fromkeys((int(s), str(o)) for s, o in order_keys if o))
    if not keys:
        return 0
    conn = get_db()
    cur = conn.cursor()
    try:
        n = 0
        for start in range(0, len(keys), 300):
            chunk = keys[start:start + 300]
            cur.execute(f"""UPDATE giveaway_assignments SET locked = 1, locked_at = UTC_TIMESTAMP()
                            WHERE locked = 0 AND (store_id, order_id) IN ({','.join(['(%s,%s)'] * len(chunk))})""",
                        [v for k in chunk for v in k])
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()
