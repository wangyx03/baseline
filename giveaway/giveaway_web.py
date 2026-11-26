"""Flask blueprint: Giveaway Pool (books for TikTok ZERO_LOTTERY orders).

- GET  /giveaway/?tab=pool           pool management (default)
- GET  /giveaway/?tab=draws          lottery books (filters: store, dates ET, state)
- POST /giveaway/pool/add            add one book (or replace its quantity)
- POST /giveaway/pool/upload         .xlsx / .csv with isbn, quantity[, note] (set or add)
- GET  /giveaway/pool/template.xlsx  empty upload file
- POST /giveaway/inventory/refresh   rebuild inventory_snapshot from OMS now (background)
- GET  /giveaway/inventory/status    is that refresh still running
- POST /giveaway/pool/update         change qty / active / note of one book
- POST /giveaway/pool/delete         remove one book (its unlocked assignments are released)
- POST /giveaway/draw                draw books for every unassigned lottery unit in the filter
- POST /giveaway/assign/reroll       draw another book for one unit
- POST /giveaway/assign/set          choose the book by hand (active pool books only)
- GET  /giveaway/export.xlsx         pick list of the filtered lottery books (?lock=1 locks them)

Drawing also happens automatically in Orders > OMS View (see giveaway.service), and books
are locked when their package is pushed to OMS or exported to the OMS Excel.

Access: users with the Giveaway module (MODULE_GIVEAWAY = 11).
"""
import io
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, jsonify, render_template, request, url_for
from flask_login import current_user

from db import get_db
from giveaway import inventory as inv
from giveaway import service as gs
from permissions.permissions import MODULE_GIVEAWAY, MODULE_TIKTOK_ORDERS, has_module, module_required

bp = Blueprint('giveaway', __name__, url_prefix='/giveaway', template_folder='templates')

ET = ZoneInfo('America/New_York')
STORES = {'TU': 1, 'VB': 2}
STORE_NAMES = {v: k for k, v in STORES.items()}
STATES = ('unassigned', 'unlocked', 'locked')
MAX_ROWS = 2000


@bp.before_request
@module_required(MODULE_GIVEAWAY)
def require_giveaway_access():
    pass


def _user():
    return (getattr(current_user, 'username', None) or str(current_user.get_id()))[:64]


def _fmt(dt):
    if not dt:
        return ''
    return dt.replace(tzinfo=timezone.utc).astimezone(ET).strftime('%Y-%m-%d %I:%M %p')


def _day(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _filters(args, omit=()):
    """WHERE (aliases o / i) for the lottery list + normalized filter values.
    omit={'status'} leaves the shipping-status filter out (for its own counts)."""
    today = datetime.now(ET).date()
    d_from = _day(args.get('from'), today - timedelta(days=6))
    d_to = _day(args.get('to'), today)
    start = datetime.combine(d_from, time.min, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)
    where, params = ['o.create_time >= %s', 'o.create_time < %s'], [start, end]
    store = args.get('store', '')
    if store in STORES:
        where.append('o.store_id = %s')
        params.append(STORES[store])
    q = (args.get('q') or '').strip()
    if q:
        where.append('(o.order_id = %s OR TRIM(i.tracking_number) = %s)')
        params += [q, q]
    statuses = list(dict.fromkeys(v for v in args.getlist('status') if v and v != 'all' and len(v) <= 64))
    if statuses and 'status' not in omit:
        where.append('o.status IN (' + ','.join(['%s'] * len(statuses)) + ')')
        params += statuses
    state = args.get('state', '')
    values = {'from': d_from.isoformat(), 'to': d_to.isoformat(), 'store': store, 'q': q,
              'state': state if state in STATES else '', 'statuses': statuses}
    return ' AND '.join(where), params, values


def _units(cur, where, params, state):
    """One row per lottery book (line x unit) with its assignment."""
    lines = gs.lottery_lines(cur, where, params)
    assigned = gs.assignments_for(cur, [(l['store_id'], l['order_id'], l['line_item_id']) for l in lines])
    rows = []
    for l in lines:
        by_unit = {a['unit_no']: a for a in assigned.get((l['store_id'], l['order_id'], l['line_item_id']), [])}
        for unit in range(1, int(l['quantity']) + 1):
            a = by_unit.get(unit)
            st = 'unassigned' if not a else ('locked' if a['locked'] else 'unlocked')
            if state and st != state:
                continue
            rows.append({
                'store': STORE_NAMES.get(l['store_id'], l['store_id']), 'store_id': l['store_id'],
                'order_id': l['order_id'], 'line_item_id': l['line_item_id'], 'unit_no': unit,
                'units': int(l['quantity']), 'tracking': l['tracking'] or '', 'status': l['status'],
                'created': _fmt(l['create_time']), 'product_name': l['product_name'] or '',
                'isbn': a['isbn'] if a else '', 'title': (a['book_title'] or '') if a else '',
                'in_pool': bool(a and a['in_pool']), 'state': st,
                'assigned_by': (a['assigned_by'] or '') if a else '',
                'assigned_at': _fmt(a['assigned_at']) if a else '',
            })
    return rows


@bp.get('/')
def page():
    tab = 'draws' if request.args.get('tab') == 'draws' else 'pool'
    where, params, values = _filters(request.args)
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        pool = gs.pool_rows(cur)
        rows = _units(cur, where, params, values['state']) if tab == 'draws' else []
        status_counts = {}
        if tab == 'draws':
            # Shipping-status choices with counts under every other filter (books, like the list).
            s_where, s_params, _ = _filters(request.args, omit={'status'})
            for r in _units(cur, s_where, s_params, values['state']):
                status_counts[r['status'] or '—'] = status_counts.get(r['status'] or '—', 0) + 1
            for st in values['statuses']:
                status_counts.setdefault(st, 0)
        oms = inv.available_by_isbn(cur, [p['isbn'] for p in pool]) if tab == 'pool' else {}
        snap = _fmt(inv.snapshot_time(cur)) if tab == 'pool' else ''
    finally:
        cur.close()
        conn.close()
    counts = {s: 0 for s in STATES}
    for r in rows:
        counts[r['state']] += 1
    active_left = sum(max(int(p['remaining']), 0) for p in pool if p['active'])
    for p in pool:
        p['oms_available'] = oms.get(p['isbn'])          # None = SKU not in the snapshot
    status_options = sorted(status_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return render_template('giveaway.html', tab=tab, pool=pool, snapshot_at=snap,
                           status_options=status_options,
                           inventory_running=inv.is_running() if tab == 'pool' else False,
                           extra_nav_links=([{'label': 'Orders', 'url': url_for('orders.page')}]
                                            if has_module(MODULE_TIKTOK_ORDERS) else []), rows=rows[:MAX_ROWS], total_rows=len(rows),
                           counts=counts, values=values, active_left=active_left,
                           query=request.query_string.decode('utf-8'))


def _json():
    return request.get_json(silent=True) or {}


def _error(exc, status=400):
    return jsonify(error=str(exc)), status


@bp.post('/pool/add')
def pool_add():
    body = _json()
    isbn = gs.normalize_isbn(body.get('isbn'))
    if not gs.looks_like_isbn(isbn):
        return _error('Enter a 10- or 13-digit ISBN.')
    try:
        qty = int(body.get('total_qty'))
    except (TypeError, ValueError):
        return _error('Quantity must be a whole number.')
    if qty < 0:
        return _error('Quantity must be 0 or more.')
    try:
        return jsonify(gs.add_book(isbn, qty, body.get('note'), _user()))
    except gs.GiveawayError as exc:
        return _error(exc)


@bp.post('/pool/upload')
def pool_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return _error('Choose a file.')
    try:
        items, notes, errors = gs.read_upload(f.filename, f.read())
        result = gs.save_pool(items, request.form.get('mode') or 'set', _user(), notes=notes)
    except gs.GiveawayError as exc:
        return _error(exc)
    return jsonify(dict(result, errors=errors[:20]))


@bp.get('/pool/template.xlsx')
def upload_template():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pool'
    ws.append(['isbn', 'quantity', 'note'])
    ws.append(['9781668024782', 20, 'example row, delete me'])
    ws.column_dimensions['A'].width = 18
    for row in ws.iter_rows(min_row=2, max_col=1):
        for cell in row:
            cell.number_format = '@'
    out = io.BytesIO()
    wb.save(out)
    resp = Response(out.getvalue(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp.headers['Content-Disposition'] = 'attachment; filename="giveaway_pool_template.xlsx"'
    return resp


@bp.post('/inventory/refresh')
def inventory_refresh():
    if not inv.start_refresh():
        return jsonify(status='busy', message='An OMS stock refresh is already running.'), 409
    return jsonify(status='started'), 202


@bp.get('/inventory/status')
def inventory_status():
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        updated = _fmt(inv.snapshot_time(cur))
    finally:
        cur.close()
        conn.close()
    return jsonify(running=inv.is_running(), updated=updated, error=inv.last_error())


@bp.post('/pool/update')
def pool_update():
    body = _json()
    isbn = gs.normalize_isbn(body.get('isbn'))
    try:
        qty = body.get('total_qty')
        n = gs.update_pool_book(isbn,
                                total_qty=None if qty in (None, '') else int(qty),
                                active=body.get('active'),
                                note=body.get('note'))
    except (ValueError, gs.GiveawayError) as exc:
        return _error(exc)
    return jsonify(updated=n)


@bp.post('/pool/delete')
def pool_delete():
    return jsonify(gs.delete_pool_book(gs.normalize_isbn(_json().get('isbn'))))


@bp.post('/draw')
def draw():
    from urllib.parse import parse_qsl
    from werkzeug.datastructures import MultiDict
    body = _json()
    picked = _picked_units(body.get('units'))
    try:
        if picked is not None:          # some rows ticked: only those
            return jsonify(gs.draw_units(picked, _user()))
        args = MultiDict(parse_qsl(str(body.get('query') or ''), keep_blank_values=True))
        where, params, _ = _filters(args)
        return jsonify(gs.draw_all(where, params, _user()))
    except gs.GiveawayError as exc:
        return _error(exc, 409)


def _picked_units(raw):
    """None = nothing sent (whole filter); else [(store_id, order_id, line_item_id, unit_no)]
    from "store:order:line:unit" strings."""
    if raw is None:
        return None
    out = []
    for item in raw if isinstance(raw, list) else []:
        parts = str(item).split(':')
        if len(parts) == 4 and parts[0].isdigit() and parts[3].isdigit():
            out.append((int(parts[0]), parts[1], parts[2], int(parts[3])))
    return out[:5000]


def _unit_args(body):
    return (int(body.get('store_id')), str(body.get('order_id')), str(body.get('line_item_id')),
            int(body.get('unit_no') or 1))


@bp.post('/assign/reroll')
def assign_reroll():
    try:
        return jsonify(isbn=gs.reroll(*_unit_args(_json()), _user()))
    except (ValueError, TypeError, gs.GiveawayError) as exc:
        return _error(exc)


@bp.post('/assign/set')
def assign_set():
    body = _json()
    try:
        return jsonify(gs.set_manual(*_unit_args(body), body.get('isbn'), _user(),
                                     confirm_short=bool(body.get('confirm'))))
    except (ValueError, TypeError, gs.GiveawayError) as exc:
        return _error(exc)


@bp.route('/export.xlsx', methods=['GET', 'POST'])
def export_xlsx():
    """Pick list of the filtered lottery books. lock=1 locks the exported books.
    POST with units (JSON list of "store:order:line:unit") = only the ticked rows."""
    import json
    from openpyxl import Workbook
    where, params, values = _filters(request.args)
    picked = None
    if request.method == 'POST':
        try:
            picked = set(_picked_units(json.loads(request.form.get('units') or '[]')) or [])
        except ValueError:
            picked = set()
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        rows = _units(cur, where, params, values['state'])
    finally:
        cur.close()
        conn.close()
    rows = [r for r in rows if r['isbn'] and (picked is None or
            (r['store_id'], str(r['order_id']), str(r['line_item_id']), r['unit_no']) in picked)]
    if (request.form.get('lock') if request.method == 'POST' else request.args.get('lock')) == '1':
        gs.lock_orders([(r['store_id'], r['order_id']) for r in rows])
    wb = Workbook(write_only=True)
    ws = wb.create_sheet('Pick list')
    ws.append(['Store', 'Order ID', 'Tracking', 'Created (ET)', 'Book #', 'ISBN', 'Title', 'Order status'])
    for r in rows:   # IDs stay text so Excel does not turn them into 5.77E+17
        ws.append([r['store'], str(r['order_id']), str(r['tracking']), r['created'],
                   f"{r['unit_no']}/{r['units']}", str(r['isbn']), r['title'], r['status']])
    out = io.BytesIO()
    wb.save(out)
    resp = Response(out.getvalue(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="giveaway_picklist_{datetime.now(ET):%Y%m%d_%H%M}.xlsx"')
    resp.headers['Cache-Control'] = 'no-store, private'
    return resp
