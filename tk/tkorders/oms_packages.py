"""Push TikTok packages (one tracking number = one parcel) to Lingxing OMS.

Used by the OMS View "Create package" button in orders_search.py.
Signing/transport reuse api/omsapi/client.py (OmsClient).

.env
    OMS_APP_KEY / OMS_APP_SECRET            same credentials as inventory_snapshot.py
    OMS_PKG_WH_CODE=M60003                  default OMS warehouse code
    OMS_PKG_WH_MAP=<tiktok warehouse_id>:M60001,...   optional per-warehouse override
    OMS_PKG_CHANNEL=Upload_Shipping_Label_SD
    OMS_PKG_CARRIER=usps                    used when the tracking prefix is not recognised
    OMS_PKG_IS_SUBMIT=0                     0 = save as draft (待提交)
    OMS_PKG_SALES_PLATFORM=TikTok           销售平台, must match an OMS platform name
"""
import json
import os
import re
import time

from api.omsapi.client import OmsClient

CREATE_PATH = '/v1/outboundOrder/create'   # OmsClient base already ends in /openapi
API_MAX_BATCH = 100

STORE_NAMES = {1: 'TruthUnfolds', 2: 'VesperBooks'}

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
    'puerto rico': 'PR', 'guam': 'GU', 'virgin islands': 'VI',
}


class OmsPushError(Exception):
    pass


def settings():
    wh_map = {}
    for part in os.environ.get('OMS_PKG_WH_MAP', '').split(','):
        tk_wh, _, oms_wh = part.strip().partition(':')
        if tk_wh and oms_wh:
            wh_map[tk_wh.strip()] = oms_wh.strip()
    return {
        'app_key': os.environ.get('OMS_APP_KEY', ''),
        'app_secret': os.environ.get('OMS_APP_SECRET', ''),
        'wh_code': os.environ.get('OMS_PKG_WH_CODE', 'M60003'),
        'wh_map': wh_map,
        'channel': os.environ.get('OMS_PKG_CHANNEL', 'Upload_Shipping_Label_SD'),
        'carrier': os.environ.get('OMS_PKG_CARRIER', 'usps'),
        'is_submit': 1 if os.environ.get('OMS_PKG_IS_SUBMIT', '0') == '1' else 0,
        'sales_platform': os.environ.get('OMS_PKG_SALES_PLATFORM', 'TikTok').strip(),
    }


def is_configured(cfg=None):
    cfg = cfg or settings()
    return bool(cfg['app_key'] and cfg['app_secret'])


def carrier_for(tracking, default):
    t = tracking.upper()
    if t.startswith('1Z'):
        return 'ups'
    if re.match(r'^9[2-5]\d{18,24}$', t):
        return 'usps'
    return default


def wh_code_for(warehouse_id, cfg):
    return cfg['wh_map'].get(str(warehouse_id or ''), cfg['wh_code'])


def build_payload(tracking, lines, cfg):
    """One OMS parcel for one tracking number.

    lines: every line item with this tracking (ordered by order create time),
    each carrying the order's recipient fields. Same SKU -> quantities added.
    Recipient comes from the first order (orders in one package share it).
    """
    first = lines[0]
    products, order_ids, remarks = {}, [], []  # field names per OMS doc 创建小包出库单
    for li in lines:
        sku = (li.get('seller_sku') or '').strip()
        if sku:
            products[sku] = products.get(sku, 0) + int(li.get('quantity') or 0)
        if li['order_id'] not in order_ids:
            order_ids.append(li['order_id'])
        note = (li.get('delivery_instruction') or '').strip()
        if note and note not in remarks:
            remarks.append(note)
    state = (first.get('recipient_state') or '').strip()
    payload = {
        'whCode': wh_code_for(first.get('warehouse_id'), cfg),
        'thirdOrderNo': tracking,                         # required, globally unique -> tracking
        # OMS accepts one platform order number only: the first (earliest) order in the package.
        'platformOrderNo': order_ids[0] if order_ids else '',
        'store': STORE_NAMES.get(first.get('store_id'), ''),
        'salesPlatform': cfg['sales_platform'],
        'subOrderType': 1,                                # 1 = product outbound (小包)
        'isSubmit': cfg['is_submit'],                     # 0 = draft
        'logisticsChannel': cfg['channel'],
        'logisticsTrackNo': tracking,
        'logisticsCarrier': carrier_for(tracking, cfg['carrier']),
        'receiver': first.get('recipient_name') or '',
        'telephone': first.get('recipient_phone') or '',
        'countryRegionCode': 'US',
        'provinceCode': US_STATES.get(state.lower(), ''),
        'provinceName': state,
        'cityName': first.get('recipient_city') or '',
        'postCode': first.get('recipient_postal_code') or '',
        'addressOne': first.get('recipient_address_line1') or '',
        'addressTwo': first.get('recipient_address_line2') or '',
        'remark': ' | '.join(remarks)[:255],
        'productList': [{'sku': sku, 'quantity': qty} for sku, qty in products.items()],
    }
    return payload, order_ids


def call(data, cfg, timeout=60):
    """POST /v1/outboundOrder/create signed by OmsClient; returns the full JSON.

    Unlike OmsClient._post this keeps the whole response (per-parcel results)
    and does not raise on a non-200 business code.
    """
    if not is_configured(cfg):
        raise OmsPushError('OMS_APP_KEY / OMS_APP_SECRET are not set')
    client = OmsClient(cfg['app_key'], cfg['app_secret'])
    req_time = str(int(time.time()))
    resp = client.session.post(client.base_url + CREATE_PATH,
                               params={'authcode': client._authcode(data, req_time)},
                               json={'appKey': client.app_key, 'reqTime': req_time, 'data': data},
                               timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        raise OmsPushError(f'HTTP {resp.status_code}: non-JSON response {resp.text[:300]!r}')
    if resp.status_code >= 400:
        raise OmsPushError(f'HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:300]}')
    return body


def response_ok(body):
    return body.get('code') in (0, 200, '0', '200') or body.get('success') is True


def per_parcel_results(body):
    """Best-effort {thirdOrderNo: {'ok', 'oms_no', 'msg'}} from a create response."""
    results = {}

    def visit(node, ok_hint=None):
        if isinstance(node, list):
            for x in node:
                visit(x, ok_hint)
        elif isinstance(node, dict):
            ref = node.get('thirdOrderNo') or node.get('referOrderNo')
            if ref:
                oms_no = node.get('outboundOrderNo') or node.get('orderNo') or node.get('outboundNo')
                ok = node.get('success')
                if ok is None:
                    ok = ok_hint if ok_hint is not None else bool(oms_no)
                msg = node.get('msg') or node.get('message') or node.get('errorMsg') or ''
                results[str(ref)] = {'ok': bool(ok), 'oms_no': oms_no, 'msg': str(msg)[:500]}
                return
            for key, value in node.items():
                low = key.lower()
                hint = False if ('fail' in low or 'error' in low) else True if 'success' in low else ok_hint
                visit(value, hint)

    visit(body.get('data'))
    return results
