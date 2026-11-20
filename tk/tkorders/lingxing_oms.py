"""Lingxing (xlwms) OMS parcel outbound drafts from TikTok orders.

Signing/transport reuse api/omsapi/client.py (OmsClient). Only used by the
"Lingxing Drafts" view in orders_search.py.

.env
    OMS_APP_KEY=... / OMS_APP_SECRET=...            same credentials as inventory_snapshot.py
    LX_OMS_CHANNELS=CODE:Label,CODE2:Label 2        channels shown in the dropdown
    LX_OMS_CHANNEL=...                              optional default logisticsChannel code
    LX_OMS_IS_SUBMIT=0                              0 = save as draft (待提交)
    LX_OMS_SALES_PLATFORM=                          optional, e.g. TIKTOK (left out if blank)
    LX_OMS_BATCH_SIZE=100                           API max is 100 parcels per call
    LX_OMS_DRAFT_STATUSES=AWAITING_SHIPMENT         TikTok statuses allowed to create drafts
"""
import json
import os
import re
import time

from api.omsapi.client import OmsClient

CREATE_PATH = '/v1/outboundOrder/create'  # OMS_API_BASE already ends in /openapi
API_MAX_BATCH = 100

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'puerto rico': 'PR', 'guam': 'GU', 'virgin islands': 'VI', 'american samoa': 'AS',
    'northern mariana islands': 'MP', 'armed forces americas': 'AA', 'armed forces europe': 'AE',
    'armed forces pacific': 'AP',
}
COUNTRIES = {'united states': 'US', 'usa': 'US', 'us': 'US'}
ISBN_SKU_RE = re.compile(r'^9[0-9]{12}$')
STORE_NAMES = {1: 'TruthUnfolds', 2: 'VesperBooks'}


class LingxingError(Exception):
    pass


def config():
    statuses = os.environ.get('LX_OMS_DRAFT_STATUSES', 'AWAITING_SHIPMENT')
    return {
        'app_key': os.environ.get('OMS_APP_KEY', ''),
        'app_secret': os.environ.get('OMS_APP_SECRET', ''),
        'channel': os.environ.get('LX_OMS_CHANNEL', ''),
        'is_submit': 1 if os.environ.get('LX_OMS_IS_SUBMIT', '0') == '1' else 0,
        'sales_platform': os.environ.get('LX_OMS_SALES_PLATFORM', '').strip(),
        'batch_size': max(1, min(API_MAX_BATCH, int(os.environ.get('LX_OMS_BATCH_SIZE', '100') or 100))),
        'statuses': {s.strip().upper() for s in statuses.split(',') if s.strip()},
    }


def is_configured(cfg=None):
    cfg = cfg or config()
    return bool(cfg['app_key'] and cfg['app_secret'])


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def call(path, data, cfg=None, timeout=60):
    """POST one OMS v1 request signed by OmsClient; returns the full JSON body.

    Unlike OmsClient._post this keeps the whole response (create returns per-order
    results) and does not raise on a non-200 business code.
    """
    cfg = cfg or config()
    if not is_configured(cfg):
        raise LingxingError('OMS_APP_KEY / OMS_APP_SECRET are not set')
    client = OmsClient(cfg['app_key'], cfg['app_secret'])
    req_time = str(int(time.time()))
    body = {'appKey': client.app_key, 'reqTime': req_time, 'data': data}
    resp = client.session.post(client.base_url + path,
                               params={'authcode': client._authcode(data, req_time)},
                               json=body, timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        raise LingxingError(f'HTTP {resp.status_code}: non-JSON response {resp.text[:300]!r}')
    if resp.status_code >= 400:
        raise LingxingError(f'HTTP {resp.status_code}: {json.dumps(payload, ensure_ascii=False)[:300]}')
    return payload


def response_ok(payload):
    code = payload.get('code')
    return code in (0, 200, '0', '200') or payload.get('success') is True


def per_order_results(payload):
    """Best-effort {thirdOrderNo: {'ok': bool, 'oms_no': str|None, 'msg': str}} from a create response.

    The response layout is not fully documented, so look for any list of dicts that
    carries an order reference. Orders missing from the result are left to the caller.
    """
    results = {}

    def visit(node, ok_hint=None):
        if isinstance(node, list):
            for x in node:
                visit(x, ok_hint)
        elif isinstance(node, dict):
            ref = node.get('thirdOrderNo') or node.get('referOrderNo') or node.get('platformOrderNo')
            if ref:
                oms_no = node.get('outboundOrderNo') or node.get('orderNo') or node.get('outboundNo')
                ok = node.get('success')
                if ok is None:
                    ok = ok_hint if ok_hint is not None else bool(oms_no)
                msg = node.get('msg') or node.get('message') or node.get('errorMsg') or ''
                results[str(ref)] = {'ok': bool(ok), 'oms_no': oms_no, 'msg': str(msg)[:500]}
                return
            for key, value in node.items():
                hint = ok_hint
                low = key.lower()
                if 'fail' in low or 'error' in low:
                    hint = False
                elif 'success' in low:
                    hint = True
                visit(value, hint)

    visit(payload.get('data'))
    return results


# ---------------------------------------------------------------------------
# TikTok order -> parcel outbound order
# ---------------------------------------------------------------------------
def build_parcel(order, items, wh_code, channel, cfg):
    """Return (payload | None, blockers[], warnings[]) for one TikTok order.

    One TikTok order = one OMS parcel. Quantities of repeated SKUs are merged.
    """
    blockers, warnings = [], []
    status = (order.get('status') or '').upper()
    if status not in cfg['statuses']:
        blockers.append(f'status {status or "—"}')
    if order.get('is_on_hold_order'):
        blockers.append('on hold')
    if not wh_code:
        blockers.append('warehouse not mapped to OMS')
    if not channel:
        blockers.append('no logistics channel')
    if not items:
        blockers.append('no line items')

    products = {}
    for it in items:
        sku = (it.get('seller_sku') or '').strip()
        if not sku:
            blockers.append(f'line {it.get("line_item_id")} has no Seller SKU')
            continue
        if (it.get('display_status') or '').upper() in ('CANCELLED', 'CANCELED'):
            continue
        if not ISBN_SKU_RE.match(sku):
            warnings.append(f'SKU {sku} is not a 13-digit 9… ISBN')
        products[sku] = products.get(sku, 0) + int(it.get('quantity') or 0)
    if items and not products and not blockers:
        blockers.append('all line items cancelled')

    country = COUNTRIES.get((order.get('recipient_country') or '').strip().lower(), '')
    state_name = (order.get('recipient_state') or '').strip()
    state_code = US_STATES.get(state_name.lower(), '') if country == 'US' else ''
    if not country:
        blockers.append(f'unknown country {order.get("recipient_country") or "—"}')
    if not order.get('recipient_postal_code'):
        blockers.append('no postal code')
    if country == 'US' and not state_code:
        warnings.append(f'state "{state_name}" not mapped to a code')
    # TikTok-shipping orders arrive with masked recipient data ("20** ******"); that is
    # expected for platform labels and is sent as-is.

    if blockers:
        return None, blockers, warnings

    parcel = {
        'whCode': wh_code,
        'thirdOrderNo': str(order['order_id']),
        'logisticsChannel': channel,
        'isSubmit': cfg['is_submit'],
        'storeName': STORE_NAMES.get(order['store_id'], str(order['store_id'])),
        'receiver': order.get('recipient_name') or '',
        'telephone': order.get('recipient_phone') or '',
        'email': order.get('buyer_email') or '',
        'countryRegionCode': country,
        'provinceCode': state_code,
        'provinceName': state_name,
        'cityName': order.get('recipient_city') or '',
        'postCode': order.get('recipient_postal_code') or '',
        'addressOne': order.get('recipient_address_line1') or '',
        'addressTwo': order.get('recipient_address_line2') or '',
        'productList': [{'sku': sku, 'quantity': qty} for sku, qty in products.items()],
    }
    if cfg['sales_platform']:
        parcel['salesPlatform'] = cfg['sales_platform']
    return parcel, blockers, warnings
