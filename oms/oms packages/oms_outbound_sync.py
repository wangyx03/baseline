#!/usr/bin/env python3
"""Sync Lingxing OMS parcel outbound orders (小包出库单) into oms_outbound_orders.

OMS View in tkorders compares TikTok tracking numbers with this table to show
whether a package already exists in OMS (however it was created: Excel import,
OMS back office, or the "Create package" button).

Run from the baseline root:
    python "oms/oms packages/oms_outbound_sync.py"                 # last 3 days (default)
    python "oms/oms packages/oms_outbound_sync.py" --days 90       # first run: backfill
    python "oms/oms packages/oms_outbound_sync.py" --from 2026-08-01 --to 2026-11-16
    python "oms/oms packages/oms_outbound_sync.py" --dump 3        # print 3 raw records, write nothing

Uses OMS_APP_KEY / OMS_APP_SECRET (same as inventory_snapshot.py).
Check the field mapping once with --dump: every record is also stored in raw_json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]   # baseline/
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:  # load baseline/.env when run by hand (systemd may already set the variables)
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
except ImportError:
    pass

from api.omsapi.client import OmsClient   # noqa: E402
from db import get_db                     # noqa: E402

PAGE_LIST_PATH = '/v1/outboundOrder/pageList'
DETAIL_PATH = '/v1/outboundOrder/detail'
DETAIL_BATCH = 50       # outbound orders per detail call (outboundOrderNoList)
OPEN_CHECK_MAX = 1000    # open orders re-read through the detail API per sync
PAGE_SIZE = 50          # OMS rejects larger pages (code 10002 查询超过最大条数限制)

# Candidate response field names, first non-empty wins. Adjust after --dump if needed.
FIELDS = {
    'oms_order_no': ('outboundOrderNo', 'orderNo', 'outboundNo'),
    'tracking_number': ('logisticsTrackNo', 'trackingNo', 'trackNo', 'logisticsTrackingNo', 'expressNo'),
    'third_order_no': ('thirdOrderNo',),
    'platform_order_no': ('platformOrderNo',),
    'wh_code': ('whCode',),
    'logistics_channel': ('logisticsChannel', 'logisticsChannelCode', 'channelCode'),
    'status': ('status', 'orderStatus'),
    'status_name': ('statusName', 'statusDesc', 'orderStatusName'),
    'oms_create_time': ('orderCreateTime', 'createTime', 'createdTime'),
    'oms_update_time': ('updateTime', 'updatedTime', 'gmtModified'),
    'exception_time': ('exceptionTime',),
}
TRACKING_KEYS = FIELDS['tracking_number']

# OMS parcel outbound status codes confirmed against the OMS back office.
# Add codes here as they are confirmed; unknown codes fall back to event times below.
STATUS_NAMES = {
    '0': '草稿',
    '3': '已出库',
    '4': '已取消',
    '5': '仓库处理中',
}


def pick(record, keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, ''):
            return value
    return None


def find_tracking(record):
    """Tracking may sit on the order or inside a nested package/label list."""
    value = pick(record, TRACKING_KEYS)
    if value:
        return value
    many = record.get('logisticsTrackNos')
    if isinstance(many, list) and many and isinstance(many[0], (str, int)) and str(many[0]).strip():
        return many[0]
    for value in record.values():
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for item in items:
            if isinstance(item, dict):
                found = pick(item, TRACKING_KEYS)
                if found:
                    return found
    return None


def normalize_tracking(value):
    return str(value).strip().upper() if value not in (None, '') else None


def parse_time(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


def to_row(record):
    row = {name: pick(record, keys) for name, keys in FIELDS.items()}
    row['tracking_number'] = normalize_tracking(find_tracking(record))
    if not row['status_name']:
        # pageList returns only a numeric status. Known codes win; otherwise guess from
        # event times (exceptionTime is not used: it stays set after an exception is fixed).
        row['status_name'] = STATUS_NAMES.get(str(row['status']))
        if not row['status_name']:
            if record.get('canceledTime'):
                row['status_name'] = '已取消'
            elif record.get('outboundTime'):
                row['status_name'] = '已出库'
    row['oms_create_time'] = parse_time(row['oms_create_time'])
    row['oms_update_time'] = parse_time(row['oms_update_time'])
    row['exception_time'] = parse_time(row['exception_time'])
    for name, limit in (('third_order_no', 100), ('platform_order_no', 255), ('wh_code', 30),
                        ('logistics_channel', 150), ('status', 32), ('status_name', 64)):
        if row[name] is not None:
            row[name] = str(row[name])[:limit]
    row['raw_json'] = json.dumps(record, ensure_ascii=False, default=str)
    return row


MAX_PAGES = 2000         # hard stop, so a misbehaving API can never loop forever


def fetch(client, start_dt, end_dt, time_type=None, page_size=PAGE_SIZE, progress=False):
    """Yield raw records created on/after start_dt.

    pageList ignores startTime/endTime (it returns its default ~90 days every time),
    but it returns orders newest-first by orderCreateTime. So page from the newest and
    stop at the first page whose orders are all older than start_dt.
    OMS times are Beijing time (ahead of ET), so this errs on the side of including more.
    """
    page = 1
    while page <= MAX_PAGES:
        data = {'page': page, 'pageSize': page_size,
                'startTime': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'endTime': end_dt.strftime('%Y-%m-%d %H:%M:%S')}
        if time_type:
            data['timeType'] = time_type
        result = client._post(PAGE_LIST_PATH, data)
        records = result.get('records', []) or []
        total = result.get('total', 0) or 0
        created = [parse_time(pick(r, FIELDS['oms_create_time'])) for r in records]
        oldest = min((c for c in created if c), default=None)
        if progress:
            print(f'page {page}/{max(1, -(-total // page_size))}  '
                  f'oldest on page: {oldest:%Y-%m-%d %H:%M}' if oldest else f'page {page}', flush=True)
        kept = [r for r, c in zip(records, created) if c is None or c >= start_dt]
        yield from kept
        if not records or page * page_size >= total:
            return
        if records and not kept:   # whole page older than start_dt -> done
            return
        page += 1
    raise RuntimeError(f'Stopped after {MAX_PAGES} pages; check the OMS pageList response')


UPSERT_SQL = """
INSERT INTO oms_outbound_orders
    (oms_order_no, tracking_number, third_order_no, platform_order_no, wh_code,
     logistics_channel, status, status_name, oms_create_time, oms_update_time, exception_time,
     source, raw_json)
VALUES
    (%(oms_order_no)s, %(tracking_number)s, %(third_order_no)s, %(platform_order_no)s, %(wh_code)s,
     %(logistics_channel)s, %(status)s, %(status_name)s, %(oms_create_time)s, %(oms_update_time)s,
     %(exception_time)s, 'sync', %(raw_json)s)
ON DUPLICATE KEY UPDATE
    tracking_number = COALESCE(VALUES(tracking_number), tracking_number),
    third_order_no = VALUES(third_order_no),
    platform_order_no = VALUES(platform_order_no),
    wh_code = VALUES(wh_code),
    logistics_channel = VALUES(logistics_channel),
    status = VALUES(status),
    status_name = VALUES(status_name),
    oms_create_time = VALUES(oms_create_time),
    oms_update_time = VALUES(oms_update_time),
    exception_time = VALUES(exception_time),
    -- shipped / cancelled orders are no longer in exception
    is_exception = IF(VALUES(status) IN ('3', '4'), 0, is_exception),
    source = 'sync',
    raw_json = VALUES(raw_json)
"""

# Rows written by "Create package" when OMS did not return an order number use a
# placeholder key; drop them once the real OMS order for that tracking is synced.
CLEANUP_SQL = """
DELETE p FROM oms_outbound_orders p
JOIN oms_outbound_orders r ON r.tracking_number = p.tracking_number AND r.source = 'sync'
WHERE p.oms_order_no LIKE 'PUSH:%%'
"""


def refresh_open_orders(client, conn, progress=False, trackings=None):
    """Re-read every open OMS order (draft 0 / 仓库处理中 5) through the detail API.

    pageList cannot tell whether an order is in exception right now (exceptionTime stays set
    after a fix), and the regular sync only re-reads recently created orders, while an order
    can turn 异常 / 已出库 / 已取消 days after it was created. The detail API answers all of
    that in batches of DETAIL_BATCH orders, so this costs a few calls per sync.

    trackings=None: every open order (daily timer / CLI). A list: only open orders with
    those tracking numbers (OMS View sync = the packages of the current search), so the
    web request stays short however many orders are open in OMS.
    """
    cur = conn.cursor()
    try:
        if trackings is None:
            cur.execute("""SELECT oms_order_no FROM oms_outbound_orders
                           WHERE status IN ('0', '5') AND oms_order_no NOT LIKE 'PUSH:%%'
                           ORDER BY oms_create_time DESC LIMIT %s""", (OPEN_CHECK_MAX,))
            order_nos = [r[0] for r in cur.fetchall()]
        else:
            keys = list(dict.fromkeys(str(t).strip().upper() for t in trackings if t))
            order_nos = []
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                cur.execute(f"""SELECT oms_order_no FROM oms_outbound_orders
                               WHERE status IN ('0', '5') AND oms_order_no NOT LIKE 'PUSH:%%'
                                 AND tracking_number IN ({','.join(['%s'] * len(chunk))})""", chunk)
                order_nos.extend(r[0] for r in cur.fetchall())
            order_nos = order_nos[:OPEN_CHECK_MAX]
        checked = live = changed = 0
        for i in range(0, len(order_nos), DETAIL_BATCH):
            batch = order_nos[i:i + DETAIL_BATCH]
            try:
                details = client._post(DETAIL_PATH, {'outboundOrderNoList': batch})
            except Exception as exc:   # keep the sync result even if a detail call fails
                print(f'Open-order check failed for {len(batch)} orders: {exc}', flush=True)
                continue
            details = details if isinstance(details, list) else [details] if details else []
            rows = []
            for d in details:
                if not isinstance(d, dict) or not d.get('outboundOrderNo'):
                    continue
                status = None if d.get('status') is None else str(d['status'])
                if status not in ('0', '5'):
                    changed += 1
                is_exc = 1 if (d.get('statusName') or '').strip() == '异常' else 0
                live += is_exc
                desc = (d.get('exceptionDesc') or '').strip()[:500] or None
                name = STATUS_NAMES.get(status)
                if not name and d.get('canceledTime'):
                    name = '已取消'
                elif not name and d.get('outboundTime'):
                    name = '已出库'
                rows.append((status, name, parse_time(d.get('exceptionTime')), is_exc,
                             desc if is_exc else None, d['outboundOrderNo']))
            if rows:
                cur.executemany("""UPDATE oms_outbound_orders
                                   SET status = COALESCE(%s, status),
                                       status_name = COALESCE(%s, status_name),
                                       exception_time = %s, is_exception = %s, exception_desc = %s,
                                       exception_checked_at = UTC_TIMESTAMP()
                                   WHERE oms_order_no = %s""", rows)
                conn.commit()
            checked += len(rows)
            if progress:
                print(f'open-order check {min(i + DETAIL_BATCH, len(order_nos))}/{len(order_nos)}', flush=True)
        return {'open_checked': checked, 'exception_live': live, 'open_finished': changed}
    finally:
        cur.close()


def sync(start_dt, end_dt, time_type=None, page_size=PAGE_SIZE, progress=False, open_trackings=None):
    """Fetch OMS outbound orders created/updated in [start_dt, end_dt) and upsert them.

    Returns counts. Used by the CLI below and by the OMS View "Sync from OMS" button.
    open_trackings: see refresh_open_orders (None = re-check every open order).
    """
    client = OmsClient(os.environ.get('OMS_APP_KEY', ''), os.environ.get('OMS_APP_SECRET', ''))
    conn = get_db()
    cur = conn.cursor()
    stats = {'seen': 0, 'saved': 0, 'no_tracking': 0, 'skipped': 0}
    try:
        batch = []
        for record in fetch(client, start_dt, end_dt, time_type, page_size, progress):
            stats['seen'] += 1
            row = to_row(record)
            if not row['oms_order_no']:
                stats['skipped'] += 1
                continue
            if not row['tracking_number']:
                stats['no_tracking'] += 1
            batch.append(row)
            if len(batch) >= 500:
                cur.executemany(UPSERT_SQL, batch)
                stats['saved'] += len(batch)
                batch = []
                conn.commit()
        if batch:
            cur.executemany(UPSERT_SQL, batch)
            stats['saved'] += len(batch)
        cur.execute(CLEANUP_SQL)
        conn.commit()
        stats.update(refresh_open_orders(client, conn, progress, open_trackings))
    finally:
        cur.close()
        conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description='Sync Lingxing OMS parcel outbound orders')
    parser.add_argument('--days', type=int, default=3, help='look back N days (default 3)')
    parser.add_argument('--from', dest='start', help='YYYY-MM-DD (overrides --days)')
    parser.add_argument('--to', dest='end', help='YYYY-MM-DD, inclusive (default today)')
    parser.add_argument('--time-type', help='optional OMS timeType value, if the API needs one')
    parser.add_argument('--page-size', type=int, default=PAGE_SIZE, help=f'records per API page (default {PAGE_SIZE})')
    parser.add_argument('--dump', type=int, default=0, help='print N raw records and exit (no DB write)')
    opts = parser.parse_args()

    end_dt = (datetime.strptime(opts.end, '%Y-%m-%d') + timedelta(days=1)) if opts.end \
        else datetime.now() + timedelta(days=1)
    start_dt = datetime.strptime(opts.start, '%Y-%m-%d') if opts.start \
        else datetime.now() - timedelta(days=opts.days)

    if opts.dump:
        client = OmsClient(os.environ.get('OMS_APP_KEY', ''), os.environ.get('OMS_APP_SECRET', ''))
        for i, record in enumerate(fetch(client, start_dt, end_dt, opts.time_type, opts.page_size)):
            if i >= opts.dump:
                break
            print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
            print('mapped ->', {k: v for k, v in to_row(record).items() if k != 'raw_json'})
        return

    stats = sync(start_dt, end_dt, opts.time_type, opts.page_size, progress=True)
    print(f'{start_dt:%Y-%m-%d} → {end_dt - timedelta(days=1):%Y-%m-%d}: '
          f'{stats["seen"]} OMS orders, {stats["saved"]} saved, {stats["no_tracking"]} without tracking, '
          f'{stats["skipped"]} skipped (no order number); '
          f'{stats.get("open_checked", 0)} open orders re-checked: '
          f'{stats.get("exception_live", 0)} in exception, {stats.get("open_finished", 0)} now shipped/cancelled', flush=True)


if __name__ == '__main__':
    main()
