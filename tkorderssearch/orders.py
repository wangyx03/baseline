"""Read-only TikTok orders search Blueprint for the Baseline Flask application."""
from datetime import datetime, timedelta, timezone
import csv
import io
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, Response, stream_with_context
from flask_login import login_required
from permissions.permissions import module_required, MODULE_TIKTOK_ORDERS
from db import get_db

orders_bp = Blueprint('orders', __name__, template_folder='templates', url_prefix='/orders')
ET = ZoneInfo('America/New_York')
TIME_ZONES = {
    'America/New_York': 'ET',
    'America/Chicago': 'CT',
    'America/Denver': 'MT',
    'America/Los_Angeles': 'PT',
    'UTC': 'UTC',
}

def _selected_timezone(params):
    name = params.get('tz', 'America/New_York')
    return name if name in TIME_ZONES else 'America/New_York'

SEARCH_FIELDS = {'order_id': 'o.order_id', 'product_id': 'i.product_id',
                 'product_name': 'i.product_name', 'seller_sku': 'i.seller_sku'}


def _day_bound(value, tz_name, end=False):
    if not value:
        return None
    try:
        local = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=ZoneInfo(tz_name))
        if end:
            local += timedelta(days=1)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _filters(params, omit=None):
    omit = omit or set()
    parts, args = [], []
    stores = [int(v) for v in params.getlist('store') if v in ('1', '2')]
    statuses = [v for v in params.getlist('status') if v and v != 'all' and len(v) <= 64]
    order_types = list(dict.fromkeys(v for v in params.getlist('order_type') if v and v != 'all' and len(v) <= 64))
    warehouses = [v for v in params.getlist('warehouse') if v and v != 'all' and len(v) <= 90 and ':' in v]
    if 'store' not in omit and stores:
        parts.append('o.store_id IN (' + ','.join(['%s'] * len(stores)) + ')')
        args.extend(stores)
    if 'status' not in omit and statuses:
        parts.append('o.status IN (' + ','.join(['%s'] * len(statuses)) + ')')
        args.extend(statuses)
    if 'order_type' not in omit and order_types:
        named_types = [v for v in order_types if v != '__null__']
        conditions = []
        if named_types:
            conditions.append('o.order_type IN (' + ','.join(['%s'] * len(named_types)) + ')')
            args.extend(named_types)
        if '__null__' in order_types:
            conditions.append("(o.order_type IS NULL OR o.order_type = '')")
        parts.append('(' + ' OR '.join(conditions) + ')')
    if 'warehouse' not in omit and warehouses:
        pairs = []
        for value in warehouses:
            store_id, warehouse_id = value.split(':', 1)
            if store_id in ('1', '2') and warehouse_id:
                pairs.append((int(store_id), warehouse_id))
        if pairs:
            parts.append('(' + ' OR '.join('(o.store_id=%s AND o.warehouse_id=%s)' for _ in pairs) + ')')
            for pair in pairs:
                args.extend(pair)
    sku_rule = params.get('sku_rule', 'all')
    sku_match = ("EXISTS (SELECT 1 FROM tkorders_items sku_filter "
                 "WHERE sku_filter.store_id=o.store_id "
                 "AND sku_filter.order_id=o.order_id "
                 "AND sku_filter.seller_sku REGEXP '^9[0-9]{12}$')")
    if 'sku_rule' not in omit:
        if sku_rule == 'match':
            parts.append(sku_match)
        elif sku_rule == 'nonmatch':
            parts.append('NOT ' + sku_match)
    tz_name = _selected_timezone(params)
    start, end = _day_bound(params.get('from'), tz_name), _day_bound(params.get('to'), tz_name, True)
    if start:
        parts.append('o.create_time >= %s')
        args.append(start)
    if end:
        parts.append('o.create_time < %s')
        args.append(end)
    keyword = (params.get('q') or '').strip()
    if keyword and 'q' not in omit:
        like = '%' + keyword + '%'
        parts.append('(' + ' OR '.join([
            'o.order_id LIKE %s',
            'EXISTS (SELECT 1 FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id AND (i.product_id LIKE %s OR i.product_name LIKE %s OR i.seller_sku LIKE %s OR i.tracking_number LIKE %s))'
        ]) + ')')
        args.extend([like] * 5)
    return (' WHERE ' + ' AND '.join(parts) if parts else ''), args


def _rows(cursor, sql, args=()):
    cursor.execute(sql, args)
    return cursor.fetchall()


@orders_bp.route('/', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def page():
    page_number = max(1, min(request.args.get('page', 1, type=int) or 1, 100000))
    per_page = 50
    sort_dir = "asc" if request.args.get("sort") == "asc" else "desc"
    tz_name = _selected_timezone(request.args)
    display_tz = ZoneInfo(tz_name)
    where, args = _filters(request.args)
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        total = _rows(cur, 'SELECT COUNT(*) AS n FROM tkorders o' + where, args)[0]['n']
        # Sum all line-item quantities for every matching order, not just the current page.
        # EXISTS / correlated aggregate prevents duplicating orders with multiple items.
        total_products = _rows(cur, """
            SELECT COALESCE(SUM(
                (SELECT COALESCE(SUM(COALESCE(i.quantity, 0)), 0)
                 FROM tkorders_items i
                 WHERE i.store_id = o.store_id AND i.order_id = o.order_id)
            ), 0) AS n
            FROM tkorders o
        """ + where, args)[0]['n']
        orders = _rows(cur, '''
            SELECT o.store_id, o.order_id, o.status, o.order_type, o.create_time,
                   o.update_time, o.warehouse_id,
                   w.warehouse AS warehouse_name,
                   (SELECT GROUP_CONCAT(DISTINCT i.product_name ORDER BY i.product_name SEPARATOR ' | ')
                      FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id)
                      AS products,
                   (SELECT GROUP_CONCAT(DISTINCT i.seller_sku ORDER BY i.seller_sku SEPARATOR ', ')
                      FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id)
                      AS skus,
                   (SELECT GROUP_CONCAT(DISTINCT NULLIF(TRIM(i.tracking_number), '') ORDER BY i.tracking_number SEPARATOR ' | ')
                      FROM tkorders_items i WHERE i.store_id=o.store_id AND i.order_id=o.order_id)
                      AS tracking
            FROM tkorders o
            LEFT JOIN warehouses w ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
        ''' + where + f' ORDER BY o.create_time {sort_dir.upper()}, o.order_id {sort_dir.upper()} LIMIT %s OFFSET %s',
            args + [per_page, (page_number - 1) * per_page])
        # Fetch line items for the current page in one query (no N+1 queries).
        items_by_order = {}
        if orders:
            keys = [(o['store_id'], o['order_id']) for o in orders]
            placeholders = ','.join(['(%s,%s)'] * len(keys))
            item_args = [v for pair in keys for v in pair]
            item_rows = _rows(cur, f"""
                SELECT store_id, order_id, line_item_id, product_name, product_id,
                       seller_sku, quantity
                FROM tkorders_items
                WHERE (store_id, order_id) IN ({placeholders})
                ORDER BY store_id, order_id, id
            """, item_args)
            for item in item_rows:
                items_by_order.setdefault((item['store_id'], item['order_id']), []).append(item)
        for order in orders:
            line_items = items_by_order.get((order['store_id'], order['order_id']), [])
            order['line_items'] = line_items
            order['item_count'] = len(line_items)
            order['total_qty'] = sum(int(item['quantity'] or 0) for item in line_items)
            order['first_item'] = line_items[0] if line_items else None
            if order['create_time']:
                order['create_time_local'] = order['create_time'].replace(tzinfo=timezone.utc).astimezone(display_tz).strftime('%Y-%m-%d %I:%M %p')
        # Facets use all active filters except their own dimension.
        status_where, status_args = _filters(request.args, {'status'})
        statuses = _rows(cur, 'SELECT o.status AS value, COUNT(*) AS n FROM tkorders o' + status_where + ' GROUP BY o.status ORDER BY n DESC', status_args)
        existing_statuses = {s['value'] for s in statuses}
        for selected in request.args.getlist('status'):
            if selected and selected not in ('all','__none__') and selected not in existing_statuses:
                statuses.append({'value': selected, 'n': 0})
        type_where, type_args = _filters(request.args, {'order_type'})
        order_types = _rows(cur, 'SELECT o.order_type AS value, COUNT(*) AS n FROM tkorders o' + type_where + ' GROUP BY o.order_type ORDER BY n DESC', type_args)
        known_types = {t['value'] for t in order_types if t['value']}
        for selected in request.args.getlist('order_type'):
            if selected not in ('', 'all', '__none__', '__null__') and selected not in known_types:
                order_types.append({'value': selected, 'n': 0})
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
    return render_template('orders.html', orders=orders, total=total, total_products=total_products, page=page_number,
                           pages=max(1, (total + per_page - 1) // per_page),
                           statuses=statuses, order_types=order_types, warehouses=all_warehouses,
                           selected_stores=request.args.getlist('store'),
                           selected_statuses=request.args.getlist('status'),
                           selected_order_types=request.args.getlist('order_type'),
                           selected_warehouses=request.args.getlist('warehouse'),
                           params=request.args, sort_dir=sort_dir, selected_tz=tz_name, tz_label=TIME_ZONES[tz_name], time_zones=TIME_ZONES)


@orders_bp.route('/export.csv', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def export_csv():
    """Export all matching orders, one CSV row per line item (not one per order)."""
    tz_name = _selected_timezone(request.args)
    display_tz = ZoneInfo(tz_name)
    where, args = _filters(request.args)
    sort_dir = "ASC" if request.args.get('sort') == 'asc' else "DESC"
    headers = [
        'Store', 'Order ID', 'Status', 'Tag', 'Created Time',
        'Product ID', 'Seller SKU', 'Product Name', 'Qty',
        'Buyer User ID', 'Buyer Nickname', 'Recipient Name', 'Recipient Phone',
        'Recipient Country', 'Recipient State', 'Recipient County',
        'Recipient City', 'Recipient Post Code', 'Recipient Address Line 1',
        'Recipient Address Line 2', 'Delivery Instruction', 'Warehouse', 'Tracking',
    ]
    sql = """
        SELECT o.store_id, o.order_id, o.status, o.order_type, o.create_time,
               i.product_id, i.seller_sku, i.product_name, i.quantity,
               o.buyer_user_id, o.buyer_nickname, o.recipient_name, o.recipient_phone,
               o.recipient_country, o.recipient_state, o.recipient_county,
               o.recipient_city, o.recipient_postal_code,
               o.recipient_address_line1, o.recipient_address_line2,
               o.delivery_instruction, COALESCE(w.warehouse, o.warehouse_id) AS warehouse,
               i.tracking_number
        FROM tkorders o
        LEFT JOIN tkorders_items i
          ON i.store_id=o.store_id AND i.order_id=o.order_id
        LEFT JOIN warehouses w
          ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
    """ + where + f" ORDER BY o.create_time {sort_dir}, o.order_id {sort_dir}, i.id ASC"

    def safe_cell(value):
        if value is None:
            return ''
        value = str(value)
        # Prevent spreadsheet formula execution for untrusted user/shop text.
        if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
            return "'" + value
        return value

    def generate():
        conn = get_db()
        cur = None
        output = io.StringIO()
        writer = csv.writer(output)
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(sql, args)
            yield '\ufeff'
            writer.writerow(headers)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                for r in batch:
                    created = r['create_time']
                    local_time = (created.replace(tzinfo=timezone.utc)
                                  .astimezone(display_tz).strftime('%Y-%m-%d %H:%M:%S')) if created else ''
                    values = [
                        'TU' if r['store_id'] == 1 else 'VB' if r['store_id'] == 2 else r['store_id'],
                        r['order_id'], r['status'], r['order_type'], local_time,
                        r['product_id'], r['seller_sku'], r['product_name'],
                        r['quantity'] if r['quantity'] is not None else '',
                        r['buyer_user_id'], r['buyer_nickname'], r['recipient_name'],
                        r['recipient_phone'], r['recipient_country'], r['recipient_state'],
                        r['recipient_county'], r['recipient_city'], r['recipient_postal_code'],
                        r['recipient_address_line1'], r['recipient_address_line2'],
                        r['delivery_instruction'], r['warehouse'], r['tracking_number'],
                    ]
                    writer.writerow([safe_cell(v) for v in values])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
        finally:
            if cur is not None:
                cur.close()
            conn.close()

    filename = f"tiktok_orders_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv"
    response = Response(stream_with_context(generate()), content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
