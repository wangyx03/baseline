"""Read-only TikTok orders search Blueprint for the Baseline Flask application."""
from datetime import datetime, timedelta, timezone
import csv
import io
from decimal import Decimal
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

# ---------------------------------------------------------------------------
# CSV export field catalog (whitelist). Only keys listed here can reach SQL.
# (key, label, group, column, kind)  kind: text | time | bool | store | warehouse
#   group 'item' columns live on tkorders_items, everything else on tkorders.
# ---------------------------------------------------------------------------
EXPORT_GROUPS = [
    ('order', 'Order Info'),
    ('buyer', 'Buyer & Recipient'),
    ('amount', 'Order Amounts'),
    ('item', 'Line Items'),
]
_EXPORT_FIELD_DEFS = [
    # Order info
    ('store', 'Store', 'order', 'store_id', 'store'),
    ('order_id', 'Order ID', 'order', 'order_id', 'text'),
    ('status', 'Status', 'order', 'status', 'text'),
    ('order_type', 'Order Type', 'order', 'order_type', 'text'),
    ('create_time', 'Created Time', 'order', 'create_time', 'time'),
    ('paid_time', 'Paid Time', 'order', 'paid_time', 'time'),
    ('update_time', 'Updated Time', 'order', 'update_time', 'time'),
    ('rts_sla_time', 'RTS SLA Time', 'order', 'rts_sla_time', 'time'),
    ('shipping_due_time', 'Shipping Due Time', 'order', 'shipping_due_time', 'time'),
    ('delivery_sla_time', 'Delivery SLA Time', 'order', 'delivery_sla_time', 'time'),
    ('cancel_order_sla_time', 'Cancel SLA Time', 'order', 'cancel_order_sla_time', 'time'),
    ('warehouse', 'Warehouse', 'order', 'warehouse_id', 'warehouse'),
    ('warehouse_id', 'Warehouse ID', 'order', 'warehouse_id', 'text'),
    ('fulfillment_type', 'Fulfillment Type', 'order', 'fulfillment_type', 'text'),
    ('delivery_type', 'Delivery Type', 'order', 'delivery_type', 'text'),
    ('shipping_type', 'Shipping Type', 'order', 'shipping_type', 'text'),
    ('delivery_option_name', 'Delivery Option', 'order', 'delivery_option_name', 'text'),
    ('payment_method_name', 'Payment Method', 'order', 'payment_method_name', 'text'),
    ('currency', 'Currency', 'order', 'currency', 'text'),
    ('auto_combine_group_id', 'Combine Group ID', 'order', 'auto_combine_group_id', 'text'),
    ('is_cod', 'COD', 'order', 'is_cod', 'bool'),
    ('is_on_hold_order', 'On Hold', 'order', 'is_on_hold_order', 'bool'),
    ('is_exchange_order', 'Exchange Order', 'order', 'is_exchange_order', 'bool'),
    ('is_replacement_order', 'Replacement Order', 'order', 'is_replacement_order', 'bool'),
    ('is_sample_order', 'Sample Order', 'order', 'is_sample_order', 'bool'),
    ('is_subscription_order', 'Subscription Order', 'order', 'is_subscription_order', 'bool'),
    # Buyer & recipient
    ('buyer_user_id', 'Buyer User ID', 'buyer', 'buyer_user_id', 'text'),
    ('buyer_nickname', 'Buyer Nickname', 'buyer', 'buyer_nickname', 'text'),
    ('buyer_email', 'Buyer Email', 'buyer', 'buyer_email', 'text'),
    ('buyer_message', 'Buyer Message', 'buyer', 'buyer_message', 'text'),
    ('recipient_name', 'Recipient Name', 'buyer', 'recipient_name', 'text'),
    ('recipient_phone', 'Recipient Phone', 'buyer', 'recipient_phone', 'text'),
    ('recipient_country', 'Recipient Country', 'buyer', 'recipient_country', 'text'),
    ('recipient_state', 'Recipient State', 'buyer', 'recipient_state', 'text'),
    ('recipient_county', 'Recipient County', 'buyer', 'recipient_county', 'text'),
    ('recipient_city', 'Recipient City', 'buyer', 'recipient_city', 'text'),
    ('recipient_postal_code', 'Recipient Post Code', 'buyer', 'recipient_postal_code', 'text'),
    ('recipient_address_line1', 'Recipient Address Line 1', 'buyer', 'recipient_address_line1', 'text'),
    ('recipient_address_line2', 'Recipient Address Line 2', 'buyer', 'recipient_address_line2', 'text'),
    ('recipient_full_address', 'Recipient Full Address', 'buyer', 'recipient_full_address', 'text'),
    ('delivery_instruction', 'Delivery Instruction', 'buyer', 'delivery_instruction', 'text'),
    # Order amounts
    ('original_total_product_price', 'Original Product Total', 'amount', 'original_total_product_price', 'text'),
    ('original_shipping_fee', 'Original Shipping Fee', 'amount', 'original_shipping_fee', 'text'),
    ('order_platform_discount', 'Platform Discount', 'amount', 'platform_discount', 'text'),
    ('order_seller_discount', 'Seller Discount', 'amount', 'seller_discount', 'text'),
    ('shipping_fee', 'Shipping Fee', 'amount', 'shipping_fee', 'text'),
    ('shipping_fee_cofunded_discount', 'Shipping Co-funded Discount', 'amount', 'shipping_fee_cofunded_discount', 'text'),
    ('shipping_fee_platform_discount', 'Shipping Platform Discount', 'amount', 'shipping_fee_platform_discount', 'text'),
    ('shipping_fee_seller_discount', 'Shipping Seller Discount', 'amount', 'shipping_fee_seller_discount', 'text'),
    ('product_tax', 'Product Tax', 'amount', 'product_tax', 'text'),
    ('shipping_fee_tax', 'Shipping Fee Tax', 'amount', 'shipping_fee_tax', 'text'),
    ('tax', 'Tax', 'amount', 'tax', 'text'),
    ('sub_total', 'Subtotal', 'amount', 'sub_total', 'text'),
    ('total_amount', 'Total Amount', 'amount', 'total_amount', 'text'),
    # Line items
    ('line_item_id', 'Line Item ID', 'item', 'line_item_id', 'text'),
    ('product_id', 'Product ID', 'item', 'product_id', 'text'),
    ('seller_sku', 'Seller SKU', 'item', 'seller_sku', 'text'),
    ('product_name', 'Product Name', 'item', 'product_name', 'text'),
    ('quantity', 'Qty', 'item', 'quantity', 'text'),
    ('sku_id', 'SKU ID', 'item', 'sku_id', 'text'),
    ('sku_name', 'SKU Name', 'item', 'sku_name', 'text'),
    ('sku_type', 'SKU Type', 'item', 'sku_type', 'text'),
    ('item_original_price', 'Item Original Price', 'item', 'original_price', 'text'),
    ('item_sale_price', 'Item Sale Price', 'item', 'sale_price', 'text'),
    ('item_platform_discount', 'Item Platform Discount', 'item', 'platform_discount', 'text'),
    ('item_seller_discount', 'Item Seller Discount', 'item', 'seller_discount', 'text'),
    ('sales_tax_amount', 'Item Sales Tax', 'item', 'sales_tax_amount', 'text'),
    ('sales_tax_rate', 'Item Sales Tax Rate', 'item', 'sales_tax_rate', 'text'),
    ('display_status', 'Item Status', 'item', 'display_status', 'text'),
    ('is_gift', 'Gift', 'item', 'is_gift', 'bool'),
    ('is_dangerous_good', 'Dangerous Good', 'item', 'is_dangerous_good', 'bool'),
    ('is_pod_customized', 'POD Customized', 'item', 'is_pod_customized', 'bool'),
    ('room_id', 'Live Room ID', 'item', 'room_id', 'text'),
    ('tracking_number', 'Tracking', 'item', 'tracking_number', 'text'),
]
EXPORT_FIELDS = {f[0]: {'key': f[0], 'label': f[1], 'group': f[2], 'col': f[3], 'kind': f[4]}
                 for f in _EXPORT_FIELD_DEFS}
# Same columns/order as the previous fixed export.
DEFAULT_EXPORT_FIELDS = [
    'store', 'order_id', 'status', 'order_type', 'create_time',
    'product_id', 'seller_sku', 'product_name', 'quantity',
    'buyer_user_id', 'buyer_nickname', 'recipient_name', 'recipient_phone',
    'recipient_country', 'recipient_state', 'recipient_county', 'recipient_city',
    'recipient_postal_code', 'recipient_address_line1', 'recipient_address_line2',
    'delivery_instruction', 'warehouse', 'tracking_number',
]
LOCKED_EXPORT_FIELDS = ['order_id']
STORE_NAMES = {1: 'TruthUnfolds', 2: 'VesperBooks'}


def _export_selection(params):
    """Validated, de-duplicated, ordered field keys plus row mode ('item' | 'order')."""
    keys = [k for k in dict.fromkeys(params.getlist('fields')) if k in EXPORT_FIELDS]
    if not keys:
        keys = list(DEFAULT_EXPORT_FIELDS)
    for locked in reversed(LOCKED_EXPORT_FIELDS):
        if locked not in keys:
            keys.insert(0, locked)
    rows = 'order' if params.get('rows') == 'order' else 'item'
    return keys, rows


def _export_select_expr(field, rows):
    """SQL expression for one whitelisted export field."""
    col, kind = field['col'], field['kind']
    if field['group'] != 'item':
        if kind == 'warehouse':
            return 'COALESCE(w.warehouse, o.warehouse_id)'
        return 'o.' + col
    if rows == 'item':
        return 'i.' + col
    # One row per order: fold all line items of the order into one cell.
    if col == 'quantity':
        agg = 'SUM(COALESCE(x.quantity, 0))'
    else:
        inner = ("IF(x.{c} IS NULL, '', IF(x.{c}, 'Yes', 'No'))" if kind == 'bool'
                 else "COALESCE(x.{c}, '')").format(c=col)
        agg = f"GROUP_CONCAT({inner} ORDER BY x.id SEPARATOR ' | ')"
    return (f'(SELECT {agg} FROM tkorders_items x '
            'WHERE x.store_id=o.store_id AND x.order_id=o.order_id)')


def _effective_params(params):
    """Default to the latest seven calendar days, inclusive, in the selected timezone.

    Preserve explicitly supplied dates and all other filters.
    """
    effective = params.copy()
    if 'from' not in effective and 'to' not in effective:
        today = datetime.now(ZoneInfo(_selected_timezone(effective))).date()
        effective.setlist('from', [(today - timedelta(days=6)).isoformat()])
        effective.setlist('to', [today.isoformat()])
    return effective


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
    params = _effective_params(request.args)
    page_number = max(1, min(request.args.get('page', 1, type=int) or 1, 100000))
    per_page = 50
    sort_dir = "asc" if request.args.get("sort") == "asc" else "desc"
    tz_name = _selected_timezone(params)
    display_tz = ZoneInfo(tz_name)
    where, args = _filters(params)
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
        # Distinct non-empty tracking numbers across all matching orders (not just this page).
        tracking_where = where + (' AND ' if where else ' WHERE ') + "TRIM(i.tracking_number) <> ''"
        unique_tracking = _rows(cur, """
            SELECT COUNT(DISTINCT TRIM(i.tracking_number)) AS n
            FROM tkorders o
            JOIN tkorders_items i ON i.store_id = o.store_id AND i.order_id = o.order_id
        """ + tracking_where, args)[0]['n']
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
        # Store facet counts: honor all other active filters, but not store selections.
        store_where, store_args = _filters(params, {'store'})
        store_rows = _rows(cur, 'SELECT o.store_id AS value, COUNT(*) AS n FROM tkorders o' + store_where + ' GROUP BY o.store_id', store_args)
        store_counts = {r['value']: r['n'] for r in store_rows}
        status_where, status_args = _filters(params, {'status'})
        statuses = _rows(cur, 'SELECT o.status AS value, COUNT(*) AS n FROM tkorders o' + status_where + ' GROUP BY o.status ORDER BY n DESC', status_args)
        existing_statuses = {s['value'] for s in statuses}
        for selected in params.getlist('status'):
            if selected and selected not in ('all','__none__') and selected not in existing_statuses:
                statuses.append({'value': selected, 'n': 0})
        type_where, type_args = _filters(params, {'order_type'})
        order_types = _rows(cur, 'SELECT o.order_type AS value, COUNT(*) AS n FROM tkorders o' + type_where + ' GROUP BY o.order_type ORDER BY n DESC', type_args)
        unspecified_type_count = sum(r['n'] for r in order_types if not r['value'])
        known_types = {t['value'] for t in order_types if t['value']}
        for selected in params.getlist('order_type'):
            if selected not in ('', 'all', '__none__', '__null__') and selected not in known_types:
                order_types.append({'value': selected, 'n': 0})
        warehouse_where, warehouse_args = _filters(params, {'warehouse'})
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
    return render_template('orders.html', orders=orders, total=total, total_products=total_products, unique_tracking=unique_tracking, page=page_number,
                           pages=max(1, (total + per_page - 1) // per_page),
                           statuses=statuses, order_types=order_types, warehouses=all_warehouses,
                           store_counts=store_counts, unspecified_type_count=unspecified_type_count,
                           selected_stores=params.getlist('store'),
                           selected_statuses=params.getlist('status'),
                           selected_order_types=params.getlist('order_type'),
                           selected_warehouses=params.getlist('warehouse'),
                           params=params, sort_dir=sort_dir, selected_tz=tz_name, tz_label=TIME_ZONES[tz_name], time_zones=TIME_ZONES,
                           export_groups=EXPORT_GROUPS,
                           export_fields=[{k: f[k] for k in ('key', 'label', 'group')} for f in EXPORT_FIELDS.values()],
                           default_export_fields=DEFAULT_EXPORT_FIELDS,
                           locked_export_fields=LOCKED_EXPORT_FIELDS)


@orders_bp.route('/export.csv', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def export_csv():
    """Export all matching orders with user-selected fields.

    ?fields=<key>&fields=<key>...  ordered column list (whitelisted via EXPORT_FIELDS)
    ?rows=item   one CSV row per line item (default)
    ?rows=order  one CSV row per order; line-item fields are joined with " | ", Qty is summed
    """
    params = _effective_params(request.args)
    tz_name = _selected_timezone(params)
    display_tz = ZoneInfo(tz_name)
    where, args = _filters(params)
    sort_dir = "ASC" if request.args.get('sort') == 'asc' else "DESC"
    keys, rows = _export_selection(request.args)
    fields = [EXPORT_FIELDS[k] for k in keys]
    headers = [f['label'] for f in fields]
    select_list = ',\n               '.join(
        f"{_export_select_expr(f, rows)} AS `f_{f['key']}`" for f in fields)
    if rows == 'item':
        from_sql = """
        FROM tkorders o
        LEFT JOIN tkorders_items i
          ON i.store_id=o.store_id AND i.order_id=o.order_id
        LEFT JOIN warehouses w
          ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
        """
        order_sql = f" ORDER BY o.create_time {sort_dir}, o.order_id {sort_dir}, i.id ASC"
    else:
        from_sql = """
        FROM tkorders o
        LEFT JOIN warehouses w
          ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
        """
        order_sql = f" ORDER BY o.create_time {sort_dir}, o.order_id {sort_dir}"
    sql = 'SELECT ' + select_list + from_sql + where + order_sql

    def safe_cell(value):
        if value is None:
            return ''
        # Numbers (incl. negative discounts) are never formulas; keep them numeric.
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return str(value)
        value = str(value)
        # Prevent spreadsheet formula execution for untrusted user/shop text.
        if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
            return "'" + value
        return value

    def format_value(field, value):
        kind = field['kind']
        if value is None:
            return None
        if kind == 'time' and isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc).astimezone(display_tz).strftime('%Y-%m-%d %H:%M:%S')
        if kind == 'store':
            return STORE_NAMES.get(value, value)
        if kind == 'bool' and not isinstance(value, str):
            return 'Yes' if value else 'No'
        return value

    def generate():
        conn = get_db()
        cur = None
        output = io.StringIO()
        writer = csv.writer(output)
        try:
            cur = conn.cursor(dictionary=True)
            if rows == 'order':
                # Long multi-item orders must not be truncated at the 1024-char default.
                cur.execute('SET SESSION group_concat_max_len = 1000000')
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
                    writer.writerow([safe_cell(format_value(f, r['f_' + f['key']])) for f in fields])
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
