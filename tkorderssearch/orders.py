"""Read-only TikTok orders search Blueprint for the Baseline Flask application."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request
from flask_login import login_required
from db import get_db

orders_bp = Blueprint('orders', __name__, template_folder='templates', url_prefix='/orders')
ET = ZoneInfo('America/New_York')
SEARCH_FIELDS = {'order_id': 'o.order_id', 'product_id': 'i.product_id',
                 'product_name': 'i.product_name', 'seller_sku': 'i.seller_sku'}


def _day_bound(value, end=False):
    if not value:
        return None
    try:
        local = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=ET)
        if end:
            local += timedelta(days=1)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _filters(params, omit=None):
    omit = omit or set()
    parts, args = [], []
    stores = [int(v) for v in params.getlist('store') if v in ('1', '2')]
    statuses = [v for v in params.getlist('status') if v and len(v) <= 64]
    warehouses = [v for v in params.getlist('warehouse') if v and len(v) <= 64]
    tag = params.get('giveaway', 'all')
    if 'store' not in omit and stores:
        parts.append('o.store_id IN (' + ','.join(['%s'] * len(stores)) + ')')
        args.extend(stores)
    if 'status' not in omit and statuses:
        parts.append('o.status IN (' + ','.join(['%s'] * len(statuses)) + ')')
        args.extend(statuses)
    if 'warehouse' not in omit and warehouses:
        parts.append('o.warehouse_id IN (' + ','.join(['%s'] * len(warehouses)) + ')')
        args.extend(warehouses)
    if 'giveaway' not in omit:
        if tag == 'include':
            parts.append("o.order_type = 'ZERO_LOTTERY'")
        elif tag == 'exclude':
            parts.append("(o.order_type <> 'ZERO_LOTTERY' OR o.order_type IS NULL)")
    start, end = _day_bound(params.get('from')), _day_bound(params.get('to'), True)
    if start:
        parts.append('o.create_time >= %s')
        args.append(start)
    if end:
        parts.append('o.create_time < %s')
        args.append(end)
    for field in SEARCH_FIELDS:
        if field in omit:
            continue
        term = (params.get(field) or '').strip()
        if not term:
            continue
        if field == 'order_id':
            parts.append('o.order_id LIKE %s')
            args.append('%' + term + '%')
        else:
            # Each product criterion can match independently within the same order.
            # This avoids duplicating order totals for multi-item orders.
            parts.append('EXISTS (SELECT 1 FROM tkorders_items i WHERE i.store_id=o.store_id '
                         'AND i.order_id=o.order_id AND ' + SEARCH_FIELDS[field] + ' LIKE %s)')
            args.append('%' + term + '%')
    return (' WHERE ' + ' AND '.join(parts) if parts else ''), args


def _rows(cursor, sql, args=()):
    cursor.execute(sql, args)
    return cursor.fetchall()


@orders_bp.route('/', methods=['GET'])
@login_required
def page():
    page_number = max(1, min(request.args.get('page', 1, type=int) or 1, 100000))
    per_page = 50
    where, args = _filters(request.args)
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        total = _rows(cur, 'SELECT COUNT(*) AS n FROM tkorders o' + where, args)[0]['n']
        orders = _rows(cur, '''
            SELECT o.store_id, o.order_id, o.status, o.order_type, o.create_time,
                   o.update_time, o.warehouse_id, o.total_amount, o.currency,
                   w.warehouse AS warehouse_name,
                   (SELECT GROUP_CONCAT(DISTINCT i.product_name ORDER BY i.product_name SEPARATOR ' | ')
                      FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id)
                      AS products,
                   (SELECT GROUP_CONCAT(DISTINCT i.seller_sku ORDER BY i.seller_sku SEPARATOR ', ')
                      FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id)
                      AS skus
            FROM tkorders o
            LEFT JOIN warehouses w ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
        ''' + where + ' ORDER BY o.create_time DESC, o.order_id DESC LIMIT %s OFFSET %s',
            args + [per_page, (page_number - 1) * per_page])
        for order in orders:
            if order['create_time']:
                order['create_time_et'] = order['create_time'].replace(tzinfo=timezone.utc).astimezone(ET).strftime('%Y-%m-%d %I:%M %p')
        # Facets use all active filters except their own dimension.
        status_where, status_args = _filters(request.args, {'status'})
        statuses = _rows(cur, 'SELECT o.status AS value, COUNT(*) AS n FROM tkorders o' + status_where + ' GROUP BY o.status ORDER BY n DESC', status_args)
        warehouse_where, warehouse_args = _filters(request.args, {'warehouse'})
        warehouses = _rows(cur, '''SELECT w.store_id, w.warehouse_id, w.warehouse,
            COUNT(o.order_id) AS n FROM warehouses w
            LEFT JOIN tkorders o ON o.store_id=w.store_id AND o.warehouse_id=w.warehouse_id
            ''' + (warehouse_where + ' AND ' if warehouse_where else ' WHERE ') +
            'o.order_id IS NOT NULL GROUP BY w.store_id,w.warehouse_id,w.warehouse ORDER BY w.store_id,w.warehouse', warehouse_args)
        # Include warehouses with zero matching orders, so choices remain discoverable.
        all_warehouses = _rows(cur, 'SELECT store_id,warehouse_id,warehouse FROM warehouses ORDER BY store_id,warehouse')
        counts = {(x['store_id'], x['warehouse_id']): x['n'] for x in warehouses}
        for w in all_warehouses:
            w['n'] = counts.get((w['store_id'], w['warehouse_id']), 0)
    finally:
        cur.close()
        conn.close()
    return render_template('orders.html', orders=orders, total=total, page=page_number,
                           pages=max(1, (total + per_page - 1) // per_page),
                           statuses=statuses, warehouses=all_warehouses,
                           selected_stores=request.args.getlist('store'),
                           selected_statuses=request.args.getlist('status'),
                           selected_warehouses=request.args.getlist('warehouse'),
                           giveaway=request.args.get('giveaway', 'all'),
                           params=request.args)
