"""Read-only TikTok orders search Blueprint for the Baseline Flask application."""
from datetime import datetime, timedelta, timezone
import csv
import re
import io
import warnings
import importlib.util
import json
from pathlib import Path
from tempfile import SpooledTemporaryFile
from urllib.parse import parse_qsl, urlencode
from openpyxl import Workbook, load_workbook
from decimal import Decimal
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, Response, stream_with_context, jsonify, url_for
from flask_login import login_required
from werkzeug.datastructures import MultiDict
import mysql.connector
import requests
from permissions.permissions import module_required, MODULE_TIKTOK_ORDERS
from db import get_db
from tk.tkorders import oms_packages

orders_bp = Blueprint('orders', __name__, template_folder='templates', url_prefix='/orders')
ET = ZoneInfo('America/New_York')
TIME_ZONES = {
    'America/New_York': 'ET',
    'America/Chicago': 'CT',
    'America/Denver': 'MT',
    'America/Los_Angeles': 'PT',
    'UTC': 'UTC',
}

LIVE_ID_RE = re.compile(r'^[0-9A-Za-z_-]{1,64}$')


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
    ('bundle_name', 'Bundle', 'item', 'bundle_name', 'text'),
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
    locked_fields = LOCKED_EXPORT_FIELDS
    for locked in reversed(locked_fields):
        if locked not in keys:
            keys.insert(0, locked)
    rows = params.get('rows') if params.get('rows') in ('order', 'item') else 'item'
    return keys, rows


# Virtual bundles in exports: a bundle line item is joined to tkproducts_bundle_items
# and becomes one row per component book. SKU / product columns show the component,
# Qty = line quantity x books per bundle, and the line's money columns stay on the
# first component row only so totals are not counted twice. "Bundle" shows the
# bundle's own product name on those rows.
BUNDLE_JOIN = ('LEFT JOIN tkproducts_bundle_items {b} '
               'ON {b}.store_id={i}.store_id AND {b}.bundle_sku_id={i}.sku_id')
_BUNDLE_COMPONENT_COLS = {'seller_sku': 'component_seller_sku', 'product_name': 'component_title',
                          'product_id': 'component_product_id', 'sku_id': 'component_sku_id'}
_BUNDLE_MONEY_COLS = {'original_price', 'sale_price', 'platform_discount', 'seller_discount',
                      'sales_tax_amount'}


def _item_col_expr(col, i, b):
    """Line-item column expression aware of bundle components (aliases i / b)."""
    if col in _BUNDLE_COMPONENT_COLS:
        return f'COALESCE({b}.{_BUNDLE_COMPONENT_COLS[col]}, {i}.{col})'
    if col == 'quantity':
        return f'COALESCE({i}.quantity, 0) * COALESCE({b}.sku_count, 1)'
    if col == 'bundle_name':
        return f'IF({b}.bundle_sku_id IS NULL, NULL, {i}.product_name)'
    if col in _BUNDLE_MONEY_COLS:
        first = (f'({b}.component_sku_id IS NULL OR {b}.component_sku_id = '
                 f'(SELECT MIN(b2.component_sku_id) FROM tkproducts_bundle_items b2 '
                 f'WHERE b2.store_id={b}.store_id AND b2.bundle_sku_id={b}.bundle_sku_id))')
        return f'IF({first}, {i}.{col}, NULL)'
    return f'{i}.{col}'


def _export_select_expr(field, rows):
    """SQL expression for one whitelisted export field."""
    col, kind = field['col'], field['kind']
    if field['group'] != 'item':
        if kind == 'warehouse':
            return 'COALESCE(w.warehouse, o.warehouse_id)'
        return 'o.' + col
    if rows == 'item':
        return _item_col_expr(col, 'i', 'b')
    # One row per order: fold all line items (bundles as their books) into one cell.
    expr = _item_col_expr(col, 'x', 'xb')
    if col == 'quantity':
        agg = f'SUM({expr})'
    else:
        inner = (f"IF({expr} IS NULL, '', IF({expr}, 'Yes', 'No'))" if kind == 'bool'
                 else f"COALESCE({expr}, '')")
        agg = f"GROUP_CONCAT({inner} ORDER BY x.id, xb.component_title SEPARATOR ' | ')"
    return (f'(SELECT {agg} FROM tkorders_items x ' + BUNDLE_JOIN.format(i='x', b='xb') +
            ' WHERE x.store_id=o.store_id AND x.order_id=o.order_id)')


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


# OMS condition of one line item's package, matching the OMS View badges:
# no_tracking (no tracking number yet), not_in_oms, in_oms, exception, cancelled.
# Uses the latest OMS outbound order for the tracking number (same as _oms_status).
OMS_STATES = (('no_tracking', 'No tracking yet'), ('not_in_oms', 'Not in OMS'),
              ('in_oms', 'In OMS'), ('exception', 'OMS exception'), ('cancelled', 'OMS cancelled'))
OMS_STATE_SQL = """CASE WHEN TRIM(COALESCE(oi.tracking_number, '')) = '' THEN 'no_tracking'
  ELSE COALESCE((SELECT CASE WHEN m.is_exception = 1 THEN 'exception'
                             WHEN m.status = '4' THEN 'cancelled' ELSE 'in_oms' END
                 FROM oms_outbound_orders m
                 WHERE m.tracking_number = UPPER(TRIM(oi.tracking_number))
                 ORDER BY COALESCE(m.oms_update_time, m.oms_create_time, m.synced_at) DESC, m.id DESC
                 LIMIT 1), 'not_in_oms') END"""


# Distinct SKUs in one order. A virtual bundle counts as its component books, so an
# order with one bundle of two books is a multi-SKU order.
ORDER_SKU_COUNT_SQL = """(SELECT COUNT(DISTINCT COALESCE(sb.component_sku_id, si.sku_id, si.seller_sku))
  FROM tkorders_items si
  LEFT JOIN tkproducts_bundle_items sb ON sb.store_id=si.store_id AND sb.bundle_sku_id=si.sku_id
  WHERE si.store_id=o.store_id AND si.order_id=o.order_id)"""
SKU_COUNT_OPTIONS = (('single', 'Single SKU'), ('multi', 'Multiple SKUs'))


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
    lives = list(dict.fromkeys(v for v in params.getlist('live')
                               if v and v != 'all' and (v == '__null__' or LIVE_ID_RE.match(v))))
    if 'live' not in omit and lives:
        named_lives = [v for v in lives if v != '__null__']
        conditions = []
        if named_lives:
            conditions.append('EXISTS (SELECT 1 FROM tkorders_items live_filter '
                              'WHERE live_filter.store_id=o.store_id AND live_filter.order_id=o.order_id '
                              'AND live_filter.room_id IN (' + ','.join(['%s'] * len(named_lives)) + '))')
            args.extend(named_lives)
        if '__null__' in lives:
            conditions.append("EXISTS (SELECT 1 FROM tkorders_items live_filter "
                              "WHERE live_filter.store_id=o.store_id AND live_filter.order_id=o.order_id "
                              "AND (live_filter.room_id IS NULL OR live_filter.room_id = ''))")
        parts.append('(' + ' OR '.join(conditions) + ')')
    sku_count = params.get('sku_count', '')
    if 'sku_count' not in omit and sku_count in dict(SKU_COUNT_OPTIONS):
        parts.append(ORDER_SKU_COUNT_SQL + (' > 1' if sku_count == 'multi' else ' <= 1'))
    oms_states = [v for v in dict.fromkeys(params.getlist('oms')) if v in dict(OMS_STATES)]
    if 'oms' not in omit and oms_states:
        # An order matches when any of its packages is in one of the selected OMS states.
        parts.append('EXISTS (SELECT 1 FROM tkorders_items oi '
                     'WHERE oi.store_id=o.store_id AND oi.order_id=o.order_id '
                     'AND (' + OMS_STATE_SQL + ') IN (' + ','.join(['%s'] * len(oms_states)) + '))')
        args.extend(oms_states)
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


def _live_titles(cursor, live_ids):
    """{live_id: {live_title, creator_username}} from tkliveinfo; empty if the table is missing."""
    ids = [v for v in dict.fromkeys(live_ids) if v]
    if not ids:
        return {}
    try:
        rows = _rows(cursor, 'SELECT live_id, live_title, creator_username FROM tkliveinfo '
                             'WHERE live_id IN (' + ','.join(['%s'] * len(ids)) + ')', ids)
    except mysql.connector.errors.ProgrammingError:
        return {}  # tkliveinfo not created yet (sync has never run)
    return {str(r['live_id']): r for r in rows}


def _tracking_source(where):
    # Blank tracking is kept separately for each order/line item, never collapsed.
    # A tracking number is scoped by store, not globally unique.
    return (" FROM tkorders o JOIN tkorders_items i "
            "ON i.store_id=o.store_id AND i.order_id=o.order_id "
            "LEFT JOIN warehouses w ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id "
            + where)


def _tracking_key():
    return ("o.store_id, CASE WHEN NULLIF(TRIM(i.tracking_number),'') IS NOT NULL "
            "THEN CONCAT('T:',TRIM(i.tracking_number)) "
            "ELSE CONCAT('U:',o.order_id,':',i.line_item_id) END")


@orders_bp.route('/', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def page():
    params = _effective_params(request.args)
    page_number = max(1, min(request.args.get('page', 1, type=int) or 1, 100000))
    per_page = 50
    view = 'oms' if request.args.get('view') == 'oms' else 'order'
    sort_dir = "asc" if request.args.get("sort") == "asc" else "desc"
    tz_name = _selected_timezone(params)
    display_tz = ZoneInfo(tz_name)
    where, args = _filters(params)
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        total = _rows(cur, 'SELECT COUNT(*) AS n FROM tkorders o' + where, args)[0]['n']
        # Total physical items for every matching order, not just the current page.
        # A virtual bundle line counts quantity x (books in the bundle).
        # EXISTS / correlated aggregate prevents duplicating orders with multiple items.
        total_items = _rows(cur, """
            SELECT COALESCE(SUM(
                (SELECT COALESCE(SUM(COALESCE(i.quantity, 0) * COALESCE(
                            (SELECT SUM(b.sku_count) FROM tkproducts_bundle_items b
                             WHERE b.store_id = i.store_id AND b.bundle_sku_id = i.sku_id), 1)), 0)
                 FROM tkorders_items i
                 WHERE i.store_id = o.store_id AND i.order_id = o.order_id)
            ), 0) AS n
            FROM tkorders o
        """ + where, args)[0]['n']
        # Distinct non-empty tracking numbers across all matching orders (not just this page).
        tracking_where = where + (' AND ' if where else ' WHERE ') + "TRIM(i.tracking_number) <> ''"
        unique_tracking = _rows(cur, """
            SELECT COUNT(DISTINCT CONCAT(o.store_id, ':', TRIM(i.tracking_number))) AS n
            FROM tkorders o
            JOIN tkorders_items i ON i.store_id = o.store_id AND i.order_id = o.order_id
        """ + tracking_where, args)[0]['n']
        # Count every distinct order linked to the selected assigned packages,
        # including orders created outside the active date range. The selected
        # package set is still determined by all current filters.
        package_order_count = _rows(cur, """
            SELECT COUNT(DISTINCT all_i.store_id, all_i.order_id) AS n
            FROM (
                SELECT DISTINCT o.store_id, TRIM(i.tracking_number) AS tracking
                FROM tkorders o
                JOIN tkorders_items i
                  ON i.store_id=o.store_id AND i.order_id=o.order_id
            """ + tracking_where + """
            ) selected_packages
            JOIN tkorders_items all_i
              ON all_i.store_id=selected_packages.store_id
             AND all_i.tracking_number=selected_packages.tracking
        """, args)[0]['n']
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
        if view == 'tracking':
            group_key = _tracking_key()
            source = _tracking_source(where)
            total = _rows(cur, 'SELECT COUNT(*) AS n FROM (SELECT 1' + source +
                          ' GROUP BY ' + group_key + ') grouped', args)[0]['n']
            cur.execute('SET SESSION group_concat_max_len = 1000000')
            orders = _rows(cur, """
                SELECT o.store_id, MAX(NULLIF(TRIM(i.tracking_number),'')) AS tracking,
                       GROUP_CONCAT(DISTINCT o.order_id ORDER BY o.order_id SEPARATOR ' | ') AS order_ids,
                       GROUP_CONCAT(DISTINCT o.status ORDER BY o.status SEPARATOR ' | ') AS statuses,
                       GROUP_CONCAT(DISTINCT o.order_type ORDER BY o.order_type SEPARATOR ' | ') AS order_types,
                       GROUP_CONCAT(DISTINCT COALESCE(w.warehouse,o.warehouse_id) SEPARATOR ' | ') AS warehouses,
                       GROUP_CONCAT(DISTINCT i.seller_sku ORDER BY i.seller_sku SEPARATOR ' | ') AS skus,
                       GROUP_CONCAT(DISTINCT i.product_name ORDER BY i.product_name SEPARATOR ' | ') AS products,
                       COUNT(DISTINCT o.order_id) AS order_count,
                       COUNT(*) AS item_count,
                       SUM(COALESCE(i.quantity,0)) AS total_qty,
                       MAX(o.create_time) AS create_time,
                       MIN(o.order_id) AS fallback_order_id
            """ + source + ' GROUP BY ' + group_key +
                f' ORDER BY create_time {sort_dir.upper()}, fallback_order_id {sort_dir.upper()} LIMIT %s OFFSET %s',
                args + [per_page, (page_number - 1) * per_page])
            # Show full order membership for assigned packages on this page,
            # rather than only the orders that passed the date filter.
            assigned = [(g['store_id'], g['tracking']) for g in orders if g['tracking']]
            if assigned:
                pair_sql = ' OR '.join('(i.store_id=%s AND i.tracking_number=%s)' for _ in assigned)
                pair_args = [v for pair in assigned for v in pair]
                membership = _rows(cur, 'SELECT i.store_id, i.tracking_number, '
                    'GROUP_CONCAT(DISTINCT i.order_id ORDER BY i.order_id SEPARATOR ' | ') AS order_ids, '
                    'COUNT(DISTINCT i.order_id) AS order_count '
                    'FROM tkorders_items i WHERE ' + pair_sql +
                    ' GROUP BY i.store_id, i.tracking_number', pair_args)
                by_package = {(r['store_id'], r['tracking_number']): r for r in membership}
                for group in orders:
                    full = by_package.get((group['store_id'], group['tracking']))
                    if full:
                        group['order_ids'] = full['order_ids']
                        group['order_count'] = full['order_count']
            for group in orders:
                group['create_time_local'] = (group['create_time'].replace(tzinfo=timezone.utc)
                    .astimezone(display_tz).strftime('%Y-%m-%d %I:%M %p')) if group['create_time'] else None
        page_total = total
        pkg_ready, pkg_pushable, pkg_error, oms_last_sync = True, 0, None, None
        if view == 'oms':
            # One row per package (store + tracking number). A package is selected when
            # any of its line items matches the filters; its contents are the full package.
            try:
                page_total, pkg_pushable, orders = _package_page(
                    cur, tracking_where, args, sort_dir, per_page, page_number, display_tz)
                last = _rows(cur, "SELECT MAX(synced_at) AS t FROM oms_outbound_orders WHERE source='sync'")[0]['t']
                oms_last_sync = (last.replace(tzinfo=timezone.utc).astimezone(display_tz)
                                 .strftime('%m/%d %I:%M %p')) if last else None
            except mysql.connector.errors.Error as exc:
                # 1146 = table missing (migration not run); anything else is shown as-is.
                pkg_ready, page_total, orders = False, 0, []
                pkg_error = None if exc.errno == 1146 else f'{exc.errno}: {exc.msg}'
                conn.rollback()
        # Fetch line items for the current page in one query (no N+1 queries).

        items_by_order = {}
        if orders and view == 'order':
            keys = [(o['store_id'], o['order_id']) for o in orders]
            placeholders = ','.join(['(%s,%s)'] * len(keys))
            item_args = [v for pair in keys for v in pair]
            item_rows = _rows(cur, f"""
                SELECT store_id, order_id, line_item_id, product_name, product_id,
                       sku_id, seller_sku, quantity
                FROM tkorders_items
                WHERE (store_id, order_id) IN ({placeholders})
                ORDER BY store_id, order_id, id
            """, item_args)
            components = _bundle_components(cur, item_rows)
            for item in item_rows:
                item['components'] = components.get((item['store_id'], item['sku_id']), [])
                items_by_order.setdefault((item['store_id'], item['order_id']), []).append(item)
        for order in (orders if view == 'order' else []):
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
        # Live Room ID facet: orders per live room under all other active filters.
        # An order counts once per live it has items from; '' = items not tied to a live.
        live_where, live_args = _filters(params, {'live'})
        live_rows = _rows(cur, '''
            SELECT COALESCE(i.room_id, '') AS value,
                   MIN(o.store_id) AS store_id,
                   MIN(o.create_time) AS first_time,
                   COUNT(DISTINCT o.store_id, o.order_id) AS n
            FROM tkorders o
            JOIN tkorders_items i ON i.store_id=o.store_id AND i.order_id=o.order_id
            ''' + live_where + '''
            GROUP BY COALESCE(i.room_id, '')
            ORDER BY first_time DESC''', live_args)
        lives, no_live_count = [], 0
        for r in live_rows:
            if not r['value']:
                no_live_count = r['n']
                continue
            r['first_local'] = (r['first_time'].replace(tzinfo=timezone.utc).astimezone(display_tz)
                                .strftime('%m/%d %I:%M %p')) if r['first_time'] else ''
            lives.append(r)
        # LIVE titles from tkliveinfo (filled by tk/tklive/tkliveinfo.py); room_id == live_id.
        live_info = _live_titles(cur, [r['value'] for r in lives])
        for r in lives:
            info = live_info.get(r['value']) or {}
            r['title'] = info.get('live_title') or ''
            r['creator'] = info.get('creator_username') or ''
        known_lives = {r['value'] for r in lives}
        for selected in params.getlist('live'):
            if selected not in ('', 'all', '__null__') and selected not in known_lives and LIVE_ID_RE.match(selected):
                lives.append({'value': selected, 'store_id': None, 'first_local': '', 'n': 0,
                              'title': '', 'creator': ''})
        # SKUs-per-order facet under all other active filters.
        sc_where, sc_args = _filters(params, {'sku_count'})
        sc = _rows(cur, f'''SELECT COALESCE(SUM(c <= 1), 0) AS single, COALESCE(SUM(c > 1), 0) AS multi
                            FROM (SELECT {ORDER_SKU_COUNT_SQL} AS c FROM tkorders o{sc_where}) x''', sc_args)[0]
        sku_count_options = [{'value': v, 'label': label, 'n': int(sc[v] or 0)} for v, label in SKU_COUNT_OPTIONS]
        # OMS condition facet: orders per package state under all other active filters.
        oms_where, oms_args = _filters(params, {'oms'})
        try:
            oms_rows = _rows(cur, f'''
                SELECT state, COUNT(DISTINCT store_id, order_id) AS n FROM (
                    SELECT o.store_id, o.order_id, {OMS_STATE_SQL} AS state
                    FROM tkorders o
                    JOIN tkorders_items oi ON oi.store_id=o.store_id AND oi.order_id=o.order_id
                    {oms_where}) x
                GROUP BY state''', oms_args)
        except mysql.connector.errors.Error as exc:
            if exc.errno != 1146:       # oms_outbound_orders missing: no counts
                raise
            oms_rows = []
        oms_counts = {r['state']: r['n'] for r in oms_rows}
        oms_options = [{'value': v, 'label': label, 'n': oms_counts.get(v, 0)} for v, label in OMS_STATES]
    finally:
        cur.close()
        conn.close()
    return render_template('orders.html', orders=orders, total=total, total_items=total_items, unique_tracking=unique_tracking, package_order_count=package_order_count, page=page_number,
                           pages=max(1, (page_total + per_page - 1) // per_page),
                           pkg_ready=pkg_ready, pkg_pushable=pkg_pushable, pkg_error=pkg_error, oms_last_sync=oms_last_sync,
                           view_links=_view_links(),
                           statuses=statuses, order_types=order_types, warehouses=all_warehouses,
                           store_counts=store_counts, unspecified_type_count=unspecified_type_count,
                           selected_stores=params.getlist('store'),
                           selected_statuses=params.getlist('status'),
                           selected_order_types=params.getlist('order_type'),
                           selected_warehouses=params.getlist('warehouse'),
                           lives=lives, no_live_count=no_live_count,
                           selected_lives=params.getlist('live'),
                           oms_options=oms_options, selected_oms=params.getlist('oms'),
                           push_options=oms_packages.form_options() if view == 'oms' else None,
                           push_defaults=_push_defaults() if view == 'oms' else None,
                           sku_count_options=sku_count_options,
                           params=params, sort_dir=sort_dir, selected_tz=tz_name, tz_label=TIME_ZONES[tz_name], time_zones=TIME_ZONES,
                           export_groups=EXPORT_GROUPS,
                           export_fields=[{k: f[k] for k in ('key', 'label', 'group')} for f in EXPORT_FIELDS.values()],
                           default_export_fields=DEFAULT_EXPORT_FIELDS,
                           locked_export_fields=LOCKED_EXPORT_FIELDS, view=view)


def _view_links():
    """Switching views replaces ?view= instead of appending a second one."""
    links = {}
    for name in ('order', 'oms'):
        args = MultiDict(request.args)
        args.setlist('view', [name])
        args.setlist('page', ['1'])
        links[name] = url_for('orders.page') + '?' + urlencode(list(args.items(multi=True)))
    return links


# ---------------------------------------------------------------------------
# OMS View: packages (store + tracking number) and the "Create package" push.
# ---------------------------------------------------------------------------
PKG_MAX = 500           # packages per Create package request
PKG_LIST_MAX = 5000     # packages per "all ticked" push (sent in chunks)


def _selected_packages_sql(tracking_where):
    """DISTINCT (store_id, tracking) of packages with at least one matching line item."""
    return ("SELECT DISTINCT o.store_id, TRIM(i.tracking_number) AS tracking "
            "FROM tkorders o JOIN tkorders_items i ON i.store_id=o.store_id AND i.order_id=o.order_id"
            + tracking_where)


# oms_outbound_orders holds OMS outbound orders (synced from OMS + written on push),
# matched to packages by tracking number.
# A package can be pushed when OMS has no outbound order for its tracking number, or when
# the latest one (same ordering as _oms_status) is cancelled (status 4). Earlier
# cancellations do not matter: only the most recent OMS order counts.
PUSHABLE_SQL = """COALESCE((SELECT COALESCE(m.status, '') = '4' FROM oms_outbound_orders m
    WHERE m.tracking_number = UPPER(s.tracking)
    ORDER BY COALESCE(m.oms_update_time, m.oms_create_time, m.synced_at) DESC, m.id DESC
    LIMIT 1), 1) = 1"""


def _oms_status(cur, trackings):
    """{UPPER(tracking): latest OMS outbound order row} for the given tracking numbers."""
    keys = list(dict.fromkeys(t.upper() for t in trackings if t))
    if not keys:
        return {}
    rows = _rows(cur, f"""
        SELECT tracking_number, oms_order_no, status, status_name, source,
               is_exception, exception_desc
        FROM oms_outbound_orders WHERE tracking_number IN ({','.join(['%s'] * len(keys))})
        ORDER BY COALESCE(oms_update_time, oms_create_time, synced_at) ASC, id ASC""", keys)
    latest = {}
    for r in rows:
        r['count'] = latest.get(r['tracking_number'], {}).get('count', 0) + 1
        latest[r['tracking_number']] = r
    return latest


def _package_page(cur, tracking_where, args, sort_dir, per_page, page_number, display_tz):
    selected = _selected_packages_sql(tracking_where)
    counts = _rows(cur, f"""
        SELECT COUNT(*) AS n, COALESCE(SUM({PUSHABLE_SQL}), 0) AS pushable
        FROM ({selected}) s""", args)[0]
    rows = _rows(cur, f"""
        SELECT s.store_id, s.tracking, MAX(o.create_time) AS create_time,
               {PUSHABLE_SQL} AS pushable
        FROM ({selected}) s
        JOIN tkorders_items a ON a.store_id=s.store_id AND a.tracking_number=s.tracking
        JOIN tkorders o ON o.store_id=a.store_id AND o.order_id=a.order_id
        GROUP BY s.store_id, s.tracking
        ORDER BY create_time {sort_dir.upper()}, s.tracking {sort_dir.upper()}
        LIMIT %s OFFSET %s
    """, args + [per_page, (page_number - 1) * per_page])
    contents = _package_contents(cur, [(r['store_id'], r['tracking']) for r in rows])
    oms = _oms_status(cur, [r['tracking'] for r in rows])
    for r in rows:
        lines = contents.get((r['store_id'], r['tracking']), [])
        skus, order_ids = {}, []
        for li in lines:
            sku = li['seller_sku'] or '—'
            skus[sku] = skus.get(sku, 0) + int(li['quantity'] or 0)
            if li['order_id'] not in order_ids:
                order_ids.append(li['order_id'])
        first = lines[0] if lines else {}
        r['lines'] = [(li['order_id'], li['seller_sku'] or '—', int(li['quantity'] or 0)) for li in lines]
        r.update(skus=list(skus.items()), order_ids=order_ids,
                 total_qty=sum(skus.values()), oms=oms.get(r['tracking'].upper()),
                 ship_to=' '.join(x for x in (first.get('recipient_state'),
                                              first.get('recipient_postal_code')) if x))
        r['create_time_local'] = (r['create_time'].replace(tzinfo=timezone.utc).astimezone(display_tz)
                                  .strftime('%Y-%m-%d %I:%M %p')) if r['create_time'] else None
    return counts['n'], int(counts['pushable']), rows


def _package_contents(cur, keys):
    """{(store_id, tracking): [line items + order recipient fields]} for whole packages.

    Virtual bundle lines are replaced by their component SKUs (quantity x sku_count),
    so OMS View, Create package and Download OMS Excel all ship the real books.
    A bundle whose contents are not synced yet keeps its empty Seller SKU and is
    therefore skipped by the push / export rules.
    """
    if not keys:
        return {}
    placeholders = ','.join(['(%s,%s)'] * len(keys))
    lines = _rows(cur, f"""
        SELECT i.store_id, i.tracking_number, i.order_id, i.sku_id, i.seller_sku, i.quantity,
               o.warehouse_id, o.buyer_email, o.recipient_name, o.recipient_phone, o.recipient_state,
               o.recipient_city, o.recipient_postal_code, o.recipient_address_line1,
               o.recipient_address_line2, o.delivery_instruction
        FROM tkorders_items i
        JOIN tkorders o ON o.store_id=i.store_id AND o.order_id=i.order_id
        WHERE (i.store_id, i.tracking_number) IN ({placeholders})
        ORDER BY o.create_time ASC, o.order_id ASC, i.id ASC
    """, [v for key in keys for v in key])
    components = _bundle_components(cur, lines)
    out = {}
    for li in lines:
        parts = components.get((li['store_id'], li['sku_id'])) or []
        if parts:
            qty = int(li['quantity'] or 0)
            expanded = [dict(li, seller_sku=c['component_seller_sku'],
                             quantity=qty * int(c['sku_count'] or 1), bundle_sku_id=li['sku_id'])
                        for c in parts]
        else:
            expanded = [li]
        out.setdefault((li['store_id'], li['tracking_number']), []).extend(expanded)
    return out


def _push_defaults():
    """Dialog defaults only (never the OMS credentials in settings())."""
    cfg = oms_packages.settings()
    return {'channel': cfg['channel'], 'sales_platform': cfg['sales_platform']}


def _picked_packages(value):
    """None = no selection sent (push everything matching); else [(store_id, tracking)]."""
    if value is None:
        return None
    out = []
    for item in value if isinstance(value, list) else []:
        store, _, tracking = str(item).partition(':')
        tracking = tracking.strip()
        if store in ('1', '2') and tracking and len(tracking) <= 128:
            out.append((int(store), tracking))
    return list(dict.fromkeys(out))


def _oms_order_counts(cur, trackings):
    """{UPPER(tracking): number of OMS outbound orders already recorded}."""
    keys = list(dict.fromkeys(t.upper() for t in trackings if t))
    if not keys:
        return {}
    rows = _rows(cur, f"""SELECT tracking_number, COUNT(*) AS n FROM oms_outbound_orders
        WHERE tracking_number IN ({','.join(['%s'] * len(keys))}) GROUP BY tracking_number""", keys)
    return {r['tracking_number']: int(r['n']) for r in rows}


@orders_bp.route('/oms/create-packages', methods=['POST'])
@module_required(MODULE_TIKTOK_ORDERS)
def create_packages():
    """Push the ticked packages (body.packages = ["store_id:tracking", ...]) or, when
    nothing is ticked, every package matching the submitted filters, that is pushable.

    Pushable = no row in oms_outbound_orders with this tracking number, or the latest
    one is cancelled (re-push uses thirdOrderNo <tracking>-R<n>).
    A successful push is written there immediately, so it is not pushed again
    before the next sync. OMS also rejects a duplicate thirdOrderNo (= tracking).
    """
    body = request.get_json(silent=True) or {}
    cfg = oms_packages.settings()
    if not oms_packages.is_configured(cfg):
        return jsonify(error='OMS_APP_KEY / OMS_APP_SECRET are not set.'), 400
    params = _effective_params(MultiDict(parse_qsl(str(body.get('query') or ''), keep_blank_values=True)))
    where, args = _filters(params)
    tracking_where = where + (' AND ' if where else ' WHERE ') + "TRIM(i.tracking_number) <> ''"
    results = []
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        if body.get('dry_run'):
            # "All ticked": the page first fetches every pushable package of the search,
            # then sends them in chunks (each chunk stays well inside the request timeout).
            keys = _rows(cur, f"""
                SELECT s.store_id, s.tracking FROM ({_selected_packages_sql(tracking_where)}) s
                WHERE {PUSHABLE_SQL}
                ORDER BY s.store_id, s.tracking LIMIT %s""", args + [PKG_LIST_MAX + 1])
            if len(keys) > PKG_LIST_MAX:
                return jsonify(error=f'More than {PKG_LIST_MAX} packages. Narrow the filters first.'), 400
            return jsonify(packages=[f"{k['store_id']}:{k['tracking']}" for k in keys])
        picked = _picked_packages(body.get('packages'))
        if picked is not None and not picked:
            return jsonify(error='No valid packages selected.'), 400
        if picked and len(picked) > PKG_MAX:
            return jsonify(error=f'More than {PKG_MAX} packages selected.'), 400
        pick_sql, pick_args = '', []
        if picked:
            # Ticked rows only, and still only those matching the filters and pushable.
            pick_sql = (' AND (s.store_id, s.tracking) IN ('
                        + ','.join(['(%s,%s)'] * len(picked)) + ')')
            pick_args = [v for pair in picked for v in pair]
        packages = _rows(cur, f"""
            SELECT s.store_id, s.tracking FROM ({_selected_packages_sql(tracking_where)}) s
            WHERE {PUSHABLE_SQL}{pick_sql}
            ORDER BY s.store_id, s.tracking LIMIT %s""", args + pick_args + [PKG_MAX + 1])
        if len(packages) > PKG_MAX:
            return jsonify(error=f'More than {PKG_MAX} packages. Narrow the filters first.'), 400
        contents = _package_contents(cur, [(p['store_id'], p['tracking']) for p in packages])
        previous = _oms_order_counts(cur, [p['tracking'] for p in packages])
        overrides = oms_packages.clean_overrides(body.get('options'))
        if not overrides.get('wh_code') or not overrides.get('channel'):
            return jsonify(error='Choose a Warehouse and a Shipping service.'), 400
        ready = []
        for p in packages:
            lines = contents.get((p['store_id'], p['tracking'])) or []
            if not lines:
                continue
            payload, order_ids = oms_packages.build_payload(p['tracking'], lines, cfg, overrides)
            earlier = previous.get(p['tracking'].upper(), 0)
            if earlier:
                # Re-push after a cancellation: thirdOrderNo must be new in OMS, so add -R<n>.
                payload['thirdOrderNo'] = f"{p['tracking']}-R{earlier + 1}"
            ref = {'store': 'TU' if p['store_id'] == 1 else 'VB', 'tracking': p['tracking'], 'orders': order_ids}
            if any(not (li.get('seller_sku') or '').strip() for li in lines):
                # Never send a partial package: a line without SKU would be left out.
                results.append(dict(ref, state='skipped', message='a line item has no Seller SKU'))
                continue
            ready.append((payload, ref))
        for start in range(0, len(ready), oms_packages.API_MAX_BATCH):
            results.extend(_push_batch(conn, cur, ready[start:start + oms_packages.API_MAX_BATCH], cfg))
    finally:
        cur.close()
        conn.close()
    summary = {}
    for r in results:
        summary[r['state']] = summary.get(r['state'], 0) + 1
    return jsonify(results=results, summary=summary)


def _bundle_components(cur, items):
    """{(store_id, sku_id): [component rows]} for line items that are virtual bundles.

    Bundle contents come from tkproducts_bundle_items (filled by the product sync);
    a bundle line item itself has no seller SKU.
    """
    keys = list({(i['store_id'], i['sku_id']) for i in items if i.get('sku_id')})
    if not keys:
        return {}
    try:
        rows = _rows(cur, f"""
            SELECT store_id, bundle_sku_id, component_seller_sku, component_sku_id,
                   component_product_id, component_title, sku_count
            FROM tkproducts_bundle_items
            WHERE (store_id, bundle_sku_id) IN ({','.join(['(%s,%s)'] * len(keys))})
            ORDER BY component_title
        """, [v for k in keys for v in k])
    except mysql.connector.errors.Error as exc:
        if exc.errno == 1146:       # table not created yet: show orders without bundle detail
            return {}
        raise
    out = {}
    for r in rows:
        out.setdefault((r['store_id'], r['bundle_sku_id']), []).append(r)
    return out


OMS_SYNC_SCRIPT = Path(__file__).resolve().parents[2] / 'oms' / 'oms packages' / 'oms_outbound_sync.py'
OMS_SYNC_MAX_DAYS = 60
_oms_sync_module = None


def _oms_sync():
    """Load oms/oms packages/oms_outbound_sync.py (folder name has a space, so not importable)."""
    global _oms_sync_module
    if _oms_sync_module is None:
        spec = importlib.util.spec_from_file_location('oms_outbound_sync', OMS_SYNC_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _oms_sync_module = module
    return _oms_sync_module


@orders_bp.route('/oms/sync', methods=['POST'])
@module_required(MODULE_TIKTOK_ORDERS)
def sync_from_oms():
    """Refresh oms_outbound_orders from OMS.

    quick=true (automatic sync when OMS View opens): OMS orders created in the last 2 days.
    Button: from one day before the filter's start date (at least 3 days back), because
    OMS orders are created after the TikTok order and OMS times are Beijing time.
    """
    body = request.get_json(silent=True) or {}
    params = _effective_params(MultiDict(parse_qsl(str(body.get('query') or ''), keep_blank_values=True)))
    now = datetime.now()
    if body.get('quick'):
        start = now - timedelta(days=2)
    else:
        try:
            start = datetime.strptime(params.get('from') or '', '%Y-%m-%d') - timedelta(days=1)
        except ValueError:
            start = now - timedelta(days=3)
        start = min(start, now - timedelta(days=3))
    end = now + timedelta(days=1)
    if (end - start).days > OMS_SYNC_MAX_DAYS:
        return jsonify(error=f'Date range is longer than {OMS_SYNC_MAX_DAYS} days. Narrow it, '
                             'or run oms_outbound_sync.py --from ... on the server.'), 400
    try:
        stats = _oms_sync().sync(start, end)
    except Exception as exc:  # API / DB errors are shown to the user
        return jsonify(error=f'OMS sync failed: {exc}'), 502
    return jsonify(dict(stats, start=start.strftime('%Y-%m-%d'), end=now.strftime('%Y-%m-%d')))


def _record_pushed(conn, cur, payload, oms_no):
    """Write a created parcel into oms_outbound_orders right away (sync refreshes it later)."""
    cur.execute("""INSERT INTO oms_outbound_orders
        (oms_order_no, tracking_number, third_order_no, platform_order_no, wh_code,
         logistics_channel, status_name, source, raw_json)
        VALUES (%s, %s, %s, %s, %s, %s, 'pushed', 'push', %s)
        ON DUPLICATE KEY UPDATE tracking_number = VALUES(tracking_number)""",
        (oms_no or 'PUSH:' + payload['thirdOrderNo'], payload['logisticsTrackNo'].upper(),
         payload['thirdOrderNo'], payload['platformOrderNo'], payload['whCode'],
         payload['logisticsChannel'], json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def _push_batch(conn, cur, batch, cfg):
    """One API call (up to 100 parcels). Results are returned; only successes are stored."""
    out = []
    try:
        body = oms_packages.call([payload for payload, _ in batch], cfg)
    except (requests.Timeout, requests.ConnectionError) as exc:
        msg = (f'No response from OMS ({type(exc).__name__}). It may still have been created: '
               'run the OMS sync before pushing again.')
        return [dict(ref, state='unknown', message=msg) for _, ref in batch]
    except oms_packages.OmsPushError as exc:
        state = 'unknown' if str(exc).startswith('HTTP 5') else 'failed'  # 5xx may still have created it
        return [dict(ref, state=state, message=str(exc)) for _, ref in batch]

    per_parcel = oms_packages.per_parcel_results(body)
    overall_ok = oms_packages.response_ok(body)
    overall_msg = str(body.get('msg') or body.get('message') or '')
    for payload, ref in batch:
        r = per_parcel.get(payload['thirdOrderNo'])
        if (r and r['ok']) or (not r and overall_ok):
            oms_no = r['oms_no'] if r else None
            _record_pushed(conn, cur, payload, oms_no)
            out.append(dict(ref, state='created', message=oms_no or ''))
        else:
            out.append(dict(ref, state='failed', message=(r and r['msg']) or overall_msg or 'rejected by OMS'))
    return out


def _export_response(fmt):
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
        """ + BUNDLE_JOIN.format(i='i', b='b') + """
        LEFT JOIN warehouses w
          ON w.store_id=o.store_id AND w.warehouse_id=o.warehouse_id
        """
        order_sql = f" ORDER BY o.create_time {sort_dir}, o.order_id {sort_dir}, i.id ASC, b.component_title ASC"
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

    if fmt == 'xlsx':
        return _xlsx_export(sql, args, fields, headers, rows, display_tz, format_value)
    filename = f"tiktok_orders_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv"
    response = Response(stream_with_context(generate()), content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


TEXT_EXPORT_KEYS = {'order_id', 'line_item_id', 'product_id', 'sku_id', 'seller_sku',
                    'tracking_number', 'room_id', 'buyer_user_id', 'recipient_phone',
                    'recipient_postal_code', 'warehouse_id', 'auto_combine_group_id'}


def _xlsx_cell(field, value):
    if value is None:
        return None
    if field['key'] in TEXT_EXPORT_KEYS or field['kind'] in ('text', 'warehouse', 'store'):
        # Numeric amounts are converted back to numbers only for known amount fields.
        if field['group'] == 'amount' and isinstance(value, (int, float, Decimal)):
            return float(value)
        if field['key'] in ('quantity', 'item_original_price', 'item_sale_price',
                            'item_platform_discount', 'item_seller_discount',
                            'sales_tax_amount', 'sales_tax_rate') and isinstance(value, (int, float, Decimal)):
            return float(value)
        value = str(value)
        # Avoid spreadsheet formula injection. XLSX string cells are not formulas.
        return value
    return value


def _xlsx_rows(row_iter, fields, headers, filename):
    wb = Workbook(write_only=True)
    ws = wb.create_sheet('Orders')
    ws.append(headers)
    for row in row_iter:
        ws.append([_xlsx_cell(f, v) for f, v in zip(fields, row)])
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        wb.save(output)
        output.seek(0)
        response = Response(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    finally:
        output.close()
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _xlsx_export(sql, args, fields, headers, rows, display_tz, format_value):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        if rows == 'order':
            cur.execute('SET SESSION group_concat_max_len = 1000000')
        cur.execute(sql, args)
        def values():
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                for record in batch:
                    yield [format_value(f, record['f_' + f['key']]) for f in fields]
        return _xlsx_rows(values(), fields, headers,
                          f'tiktok_orders_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx')
    finally:
        cur.close()
        conn.close()


def _export_tracking(fmt, params, where, args, sort_dir, keys, rows, display_tz):
    # All matching line items are queried. Grouping is by (store, tracking), with
    # each unassigned line item isolated to avoid inventing a shared package.
    fields = [EXPORT_FIELDS[k] for k in keys]
    headers = [f['label'] for f in fields]
    select_list = ', '.join(f"{_export_select_expr(f, 'item')} AS `f_{f['key']}`" for f in fields)
    sql = ('SELECT o.store_id AS _store, o.order_id AS _order, '
           "i.line_item_id AS _line, NULLIF(TRIM(i.tracking_number), '') AS _tracking, "
           'o.create_time AS _created, ' + select_list + _tracking_source(where) +
           f' ORDER BY o.create_time {sort_dir}, o.order_id {sort_dir}, i.id ASC')
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, args)
        grouped = {}
        for record in iter(lambda: cur.fetchmany(1000), []):
            for r in record:
                key = (r['_store'], 'T', r['_tracking']) if r['_tracking'] else (r['_store'], 'U', r['_order'], r['_line'])
                grouped.setdefault(key, []).append(r)
        def fmt_value(f, v):
            if v is None:
                return None
            if f['kind'] == 'time' and isinstance(v, datetime):
                return v.replace(tzinfo=timezone.utc).astimezone(display_tz).strftime('%Y-%m-%d %H:%M:%S')
            if f['kind'] == 'store':
                return STORE_NAMES.get(v, v)
            if f['kind'] == 'bool' and not isinstance(v, str):
                return 'Yes' if v else 'No'
            return v
        def values():
            for group in grouped.values():
                if rows == 'tracking_item':
                    for r in group:
                        yield [fmt_value(f, r['f_' + f['key']]) for f in fields]
                else:
                    result = []
                    for f in fields:
                        k = 'f_' + f['key']
                        vals = [fmt_value(f, r[k]) for r in group if r[k] is not None]
                        if f['key'] == 'quantity':
                            result.append(sum(int(v) for v in vals))
                        elif f['key'] in ('order_id', 'tracking_number'):
                            result.append(' | '.join(dict.fromkeys(str(v) for v in vals)))
                        elif f['group'] == 'item':
                            result.append(' | '.join(dict.fromkeys(str(v) for v in vals)))
                        else:
                            result.append(' | '.join(dict.fromkeys(str(v) for v in vals)))
                    yield result
        if fmt == 'xlsx':
            return _xlsx_rows(values(), fields, headers,
                              f'tiktok_tracking_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx')
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for vals in values():
            writer.writerow([_csv_safe(v) for v in vals])
        response = Response('﻿' + output.getvalue(), content_type='text/csv; charset=utf-8')
        response.headers['Content-Disposition'] = f'attachment; filename="tiktok_tracking_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv"'
        response.headers['Cache-Control'] = 'no-store, private'
        return response
    finally:
        cur.close()
        conn.close()


def _csv_safe(value):
    if value is None:
        return ''
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    value = str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


@orders_bp.route('/export.csv', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def export_csv():
    return _export_response('csv')


@orders_bp.route('/export.xlsx', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def export_xlsx():
    return _export_response('xlsx')


# ---------------------------------------------------------------------------
# Download OMS Excel: Lingxing OMS 小包出库单 import file for the filtered packages.
# Same packages as OMS View / Create package (full contents of every package with a
# matching line item). One OMS order per tracking number, one row per SKU; when a
# tracking number repeats a SKU, the first row's quantity is increased and the
# duplicate row is dropped. Packages already in OMS and packages with a line
# without Seller SKU are left out (same rules as Create package).
# ---------------------------------------------------------------------------
OMS_IMPORT_TEMPLATE = Path(__file__).with_name('oms_import_template.xlsx')
OMS_IMPORT_SHEET = '小包出库单'


def _oms_import_rows(packages, contents, cfg):
    """Template rows A..Z for every package; returns (rows, skipped_trackings)."""
    out, skipped = [], []
    for p in packages:
        lines = contents.get((p['store_id'], p['tracking'])) or []
        if not lines or any(not (li.get('seller_sku') or '').strip() for li in lines):
            skipped.append(p['tracking'])
            continue
        tracking = p['tracking']
        by_sku = {}
        for li in lines:   # ordered by order create time
            sku = li['seller_sku'].strip()
            qty = int(li['quantity'] or 0)
            if sku in by_sku:
                by_sku[sku][25] += qty      # same tracking + same SKU: add to the first row
                continue
            text = lambda v: '' if v is None else str(v)
            row = [
                tracking,                                                   # A Identification No.
                oms_packages.wh_code_for(li.get('warehouse_id'), cfg),      # B Warehouse Code
                '', '', '',                                                 # C Sales Platform, D Store, E Reference No.
                text(li['order_id']),                                       # F Platform Number
                cfg['channel'],                                             # G Shipping Service
                oms_packages.carrier_for(tracking, 'usps'),                 # H Carrier
                tracking,                                                   # I Tracking No.
                '', '',                                                     # J Signature, K Insurance
                text(li.get('recipient_name')),                             # L Recipient
                text(li.get('recipient_phone')),                            # M Phone
                '', '', '',                                                 # N Email, O Tax ID, P Company
                'US',                                                       # Q Country
                text(li.get('recipient_state')),                            # R State
                text(li.get('recipient_city')),                             # S City
                text(li.get('recipient_postal_code')),                      # T Zip
                '',                                                         # U House Number
                text(li.get('recipient_address_line1')),                    # V Address 1
                text(li.get('recipient_address_line2')),                    # W Address 2
                text(li.get('delivery_instruction')),                       # X Remark
                sku,                                                        # Y SKU
                qty,                                                        # Z Quantity (int while merging)
            ]
            by_sku[sku] = row
            out.append(row)
    return out, skipped


@orders_bp.route('/oms/export.xlsx', methods=['GET'])
@module_required(MODULE_TIKTOK_ORDERS)
def export_oms_xlsx():
    params = _effective_params(request.args)
    where, args = _filters(params)
    tracking_where = where + (' AND ' if where else ' WHERE ') + "TRIM(i.tracking_number) <> ''"
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        packages = _rows(cur, f"""
            SELECT s.store_id, s.tracking, MAX(o.create_time) AS created
            FROM ({_selected_packages_sql(tracking_where)}) s
            JOIN tkorders_items a ON a.store_id=s.store_id AND a.tracking_number=s.tracking
            JOIN tkorders o ON o.store_id=a.store_id AND o.order_id=a.order_id
            WHERE {PUSHABLE_SQL}
            GROUP BY s.store_id, s.tracking
            ORDER BY created ASC, s.tracking ASC""", args)
        contents = _package_contents(cur, [(p['store_id'], p['tracking']) for p in packages])
    finally:
        cur.close()
        conn.close()

    rows, skipped = _oms_import_rows(packages, contents, oms_packages.settings())
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')     # the template's data-validation extension
        wb = load_workbook(OMS_IMPORT_TEMPLATE)
    ws = wb[OMS_IMPORT_SHEET]
    for r_idx, row in enumerate(rows, start=2):
        for c_idx, value in enumerate(row, start=1):
            if value == '':
                continue
            cell = ws.cell(row=r_idx, column=c_idx, value=str(value))
            cell.number_format = '@'        # the template stores every value as text
    output = io.BytesIO()
    wb.save(output)
    response = Response(output.getvalue(),
                        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response.headers['Content-Disposition'] = (
        f'attachment; filename="OMS_{datetime.now(ET):%Y%m%d_%H%M}_{len(packages) - len(skipped)}pkg.xlsx"')
    response.headers['X-Packages-Skipped'] = str(len(skipped))
    response.headers['Cache-Control'] = 'no-store, private'
    return response
