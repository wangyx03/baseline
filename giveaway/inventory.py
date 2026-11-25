"""OMS stock for the giveaway pool + a manual refresh of inventory_snapshot.

inventory_snapshot is filled by oms/inventory/inventory_snapshot.py (baseline-inventory
service, hourly). It is rebuilt as a whole each run (staging table + swap), so the
manual refresh always runs the full snapshot, exactly like the service does.
"""
import argparse
import importlib.util
import os
import threading
from pathlib import Path

from db import get_db

SNAPSHOT_SCRIPT = Path(__file__).resolve().parents[1] / 'oms' / 'inventory' / 'inventory_snapshot.py'
REFRESH_LOCK = 'oms_inventory_refresh'
_module = None
_last_error = {'message': None}


def available_by_isbn(cur, isbns):
    """{isbn: sellable OMS stock} = available_stock of 'Good' stock summed over warehouses."""
    keys = list(dict.fromkeys(i for i in isbns if i))
    out = {}
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        cur.execute(f"""SELECT sku, SUM(available_stock) AS available
                        FROM inventory_snapshot
                        WHERE stock_type = 'Good' AND sku IN ({','.join(['%s'] * len(chunk))})
                        GROUP BY sku""", chunk)
        for r in cur.fetchall():
            out[r['sku']] = int(r['available'] or 0)
    return out


def snapshot_time(cur):
    cur.execute('SELECT MAX(updated_at) AS t FROM inventory_snapshot')
    row = cur.fetchone()
    return row['t'] if row else None


def _snapshot_module():
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location('inventory_snapshot', SNAPSHOT_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _module = module
    return _module


def is_running():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute('SELECT IS_USED_LOCK(%s)', (REFRESH_LOCK,))
        return bool(cur.fetchone()[0])
    finally:
        cur.close()
        conn.close()


def last_error():
    return _last_error['message']


def _run():
    """Full OMS -> inventory_snapshot run under a named lock (same defaults as the service)."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute('SELECT GET_LOCK(%s, 0)', (REFRESH_LOCK,))
        if cur.fetchone()[0] != 1:
            return
        _last_error['message'] = None
        try:
            m = _snapshot_module()
            client = m.OmsClient(os.environ.get('OMS_APP_KEY', ''), os.environ.get('OMS_APP_SECRET', ''))
            args = argparse.Namespace(sku=None, warehouse=None, stock_type=None, stock_lookback_days=3650,
                                      skip_transit=False, transit_lookback_days=60)
            rows = m.run_once(client, args)
            m.DbWriter(database=os.environ.get('DB_NAME', ''),
                       table=os.environ.get('DB_TABLE', 'inventory_snapshot')).write_full(rows)
        except Exception as exc:   # shown on the page; OMS errors never contain secrets
            _last_error['message'] = f'{type(exc).__name__}: {exc}'[:300]
        finally:
            cur.execute('SELECT RELEASE_LOCK(%s)', (REFRESH_LOCK,))
            cur.fetchall()
    finally:
        cur.close()
        conn.close()


def start_refresh():
    """Start a background refresh. Returns False when one is already running."""
    if is_running():
        return False
    threading.Thread(target=_run, daemon=True, name='oms-inventory-refresh').start()
    return True
