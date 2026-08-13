"""
inventory_query_to_feishu.py — Run the TU/Vb locked-inventory query against
MySQL (inventory_snapshot + book_sku + weekly_inventory) and write the
result straight into a Feishu (Lark) spreadsheet, overwriting a fixed range.

Companion to oms_inventory_to_mysql.py: that script fills inventory_snapshot,
this one reads it back out (joined with book metadata + weekly allocation
plans) and pushes the combined view to Feishu so it's viewable without
touching MySQL directly.

Instead of running on its own fixed timer, this script polls
inventory_snapshot's MAX(updated_at) every --poll-interval seconds and only
runs the Feishu sync when that value has changed — i.e. right after
oms_inventory_to_mysql.py finishes its own hourly write. The two scripts
don't need to be chained or invoked in order; each just runs on its own
schedule (see oms_inventory_to_mysql.py's docstring), and this one reacts
whenever fresh data actually shows up.

=============================================================================
1. Install dependencies on the VPS
=============================================================================
    pip install mysql-connector-python requests python-dotenv --break-system-packages

    DB access goes through the shared baseline/db.py (get_db(), mysql.connector)
    instead of a local pymysql connection — no separate DB_HOST/PORT/USER/etc.
    handling needed here.

=============================================================================
2. .env additions (same file/keys as your other Feishu daemons already use)
=============================================================================
    FEISHU_APP_ID=cli_xxxxxxxx
    FEISHU_APP_SECRET=xxxxxxxx

    # Target Feishu spreadsheet
    FEISHU_SHEET_URL=https://xcn3xthf3pue.feishu.cn/sheets/UnSRsCAfGhDWkitWOwvcb8o6nRc
    FEISHU_SHEET_ID=Alyix3

    # How often to check inventory_snapshot.updated_at for a change, in seconds
    POLL_INTERVAL_SECONDS=60

=============================================================================
3. Usage
=============================================================================
    # Sync immediately once and exit (ignores change detection)
    python inventory_query_to_feishu.py --once

    # Poll forever, syncing to Feishu only when updated_at changes (default)
    python inventory_query_to_feishu.py

    # Poll every 30s instead of the default 60s
    python inventory_query_to_feishu.py --poll-interval 30

=============================================================================
4. systemd service
=============================================================================
    [Unit]
    Description=Inventory query -> Feishu sync (polls for changes)
    After=network.target mysql.service

    [Service]
    WorkingDirectory=/path/to/script
    ExecStart=/usr/bin/python3 inventory_query_to_feishu.py
    Restart=always
    Environment=PYTHONUNBUFFERED=1

    [Install]
    WantedBy=multi-user.target
"""

import os
import re
import sys
import time
import argparse
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_db  # noqa: E402  (shared connection helper, baseline/db.py)
from utils import format_et  # noqa: E402  (UTC -> Eastern display formatting, baseline/utils.py)

import requests


def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


# ----------------------------------------------------------------------
# The query, exactly as specified
# ----------------------------------------------------------------------
QUERY = """
SELECT
    bs.isbn AS 'sku',
    bs.book_title AS 'product name',
    inv.warehouse,
    inv.stock_type,
    weekly_tu.planned_qty AS 'TU_locked',
    weekly_vb.planned_qty AS 'Vb_locked',
    inv.available_stock AS 'warehouse_stock',
    inv.available_stock
        - COALESCE(weekly_tu.planned_qty, 0)
        - COALESCE(weekly_vb.planned_qty, 0)
        AS 'remaining_stock'

FROM inventory_snapshot inv

LEFT JOIN book_sku bs
    ON inv.sku = bs.isbn

LEFT JOIN weekly_inventory weekly_tu
    ON bs.isbn = weekly_tu.sku
    AND weekly_tu.store_id = 1

LEFT JOIN weekly_inventory weekly_vb
    ON bs.isbn = weekly_vb.sku
    AND weekly_vb.store_id = 2

WHERE inv.stock_type = 'Good'
"""

HEADER = ["sku", "product name", "warehouse", "stock_type", "TU_locked", "Vb_locked",
          "warehouse_stock", "remaining_stock"]


# ----------------------------------------------------------------------
# MySQL
# ----------------------------------------------------------------------
def run_query() -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(QUERY)
        return cursor.fetchall()
    finally:
        cursor.close()
        db.close()


def get_latest_snapshot_time(table: str):
    """Read MAX(updated_at) off inventory_snapshot — used to detect that
    inventory_snapshot.py has just finished a fresh write, without needing
    this script to be invoked right after that one."""
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT MAX(updated_at) AS latest FROM `{table}`")
        row = cursor.fetchone()
        return row["latest"] if row else None
    finally:
        cursor.close()
        db.close()


# ----------------------------------------------------------------------
# Feishu (Lark) client — tenant_access_token + sheets v2 values API
# ----------------------------------------------------------------------
class FeishuClient:
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        if not app_id or not app_secret:
            raise RuntimeError("Missing FEISHU_APP_ID / FEISHU_APP_SECRET")
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._token_expiry: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = requests.post(
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu auth failed: {result}")
        self._token = result["tenant_access_token"]
        self._token_expiry = time.time() + result.get("expire", 7200)
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json; charset=utf-8"}

    def write_values(self, spreadsheet_token: str, sheet_id: str, rows: List[List[Any]],
                      clear_rows: int = 2000, clear_cols: int = 10) -> None:
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=0)

        # 1) Blank a generous fixed-size range first, so old rows beyond the
        #    new result set's length don't linger.
        blank_range = f"{sheet_id}!A1:{_col_letter(clear_cols)}{clear_rows}"
        blank_values = [[""] * clear_cols for _ in range(min(clear_rows, max(n_rows, 1) + 50))]
        self._put_values(spreadsheet_token, f"{sheet_id}!A1:{_col_letter(clear_cols)}{len(blank_values)}", blank_values)

        # 2) Write the real data on top.
        if n_rows == 0:
            log("Query returned no rows; sheet cleared, nothing else to write.")
            return
        data_range = f"{sheet_id}!A1:{_col_letter(n_cols)}{n_rows}"
        self._put_values(spreadsheet_token, data_range, rows)
        log(f"Wrote {n_rows - 1} data row(s) + header to Feishu sheet {sheet_id}.")

    def write_timestamp(self, spreadsheet_token: str, sheet_id: str, text: str, cell: str = "I1") -> None:
        """Write a "last updated" label into row 1, right after the data
        columns (HEADER is 8 columns, A–H, so I1 sits right beside it).

        Feishu's values API rejects a single-cell range like "I1" (code
        90202, "wrong range") — it needs a two-endpoint range, so a single
        cell is addressed as "I1:I1"."""
        a1_range = f"{sheet_id}!{cell}:{cell}"
        self._put_values(spreadsheet_token, a1_range, [[text]])
        log(f"Wrote update timestamp to {a1_range}: {text}")

    def _put_values(self, spreadsheet_token: str, a1_range: str, values: List[List[Any]]) -> None:
        url = f"{self.BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values"
        resp = requests.put(
            url, headers=self._headers(),
            json={"valueRange": {"range": a1_range, "values": values}},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu write failed for range {a1_range}: {result}")


def extract_spreadsheet_token(sheet_url: str) -> str:
    """Pull the spreadsheet token out of a Feishu sheet URL, e.g.
    https://xcn3xthf3pue.feishu.cn/sheets/UnSRsCAfGhDWkitWOwvcb8o6nRc(?sheet=...)
    -> UnSRsCAfGhDWkitWOwvcb8o6nRc"""
    match = re.search(r"/sheets/([A-Za-z0-9]+)", sheet_url)
    if not match:
        raise RuntimeError(f"Could not parse spreadsheet token out of FEISHU_SHEET_URL: {sheet_url!r}")
    return match.group(1)


def _col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letters (1 -> A, 27 -> AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def rows_to_grid(records: List[Dict[str, Any]]) -> List[List[Any]]:
    grid = [HEADER]
    for r in records:
        grid.append([
            r.get("sku", ""),
            r.get("product name", ""),
            r.get("warehouse", ""),
            r.get("stock_type", ""),
            r.get("TU_locked") if r.get("TU_locked") is not None else "",
            r.get("Vb_locked") if r.get("Vb_locked") is not None else "",
            r.get("warehouse_stock", ""),
            r.get("remaining_stock", ""),
        ])
    return grid


def sync_once(feishu: FeishuClient, spreadsheet_token: str, sheet_id: str, snapshot_time=None) -> None:
    log("Running MySQL query...")
    records = run_query()
    log(f"  -> {len(records)} row(s)")
    grid = rows_to_grid(records)
    feishu.write_values(spreadsheet_token, sheet_id, grid)

    # snapshot_time is inventory_snapshot's own updated_at (naive UTC, as
    # stored in MySQL) — this is when the DATA was captured, not when this
    # script happens to run. format_et() converts it to Eastern for display.
    if snapshot_time is not None:
        feishu.write_timestamp(spreadsheet_token, sheet_id, f"Renew at: {format_et(snapshot_time)}")
    else:
        log("No snapshot_time available; skipping timestamp write.")


def main():
    parser = argparse.ArgumentParser(description="Sync inventory query results from MySQL into a Feishu sheet")
    parser.add_argument("--poll-interval", type=int,
                         default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60") or "60"),
                         metavar="SECONDS", help="how often to check inventory_snapshot.updated_at for changes")
    parser.add_argument("--once", action="store_true",
                         help="sync immediately and exit, skipping change detection")
    args = parser.parse_args()

    sheet_url = os.environ.get("FEISHU_SHEET_URL", "")
    sheet_id = os.environ.get("FEISHU_SHEET_ID", "")
    if not sheet_url or not sheet_id:
        log("Error: missing FEISHU_SHEET_URL / FEISHU_SHEET_ID in .env", err=True)
        sys.exit(1)
    try:
        spreadsheet_token = extract_spreadsheet_token(sheet_url)
    except RuntimeError as e:
        log(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        feishu = FeishuClient(os.environ.get("FEISHU_APP_ID", ""), os.environ.get("FEISHU_APP_SECRET", ""))
    except RuntimeError as e:
        log(f"Error: {e}", err=True)
        sys.exit(1)

    # Same table name inventory_snapshot.py writes to (DB_TABLE in .env,
    # defaults to "inventory_snapshot" on both sides).
    table = os.environ.get("DB_TABLE", "inventory_snapshot")

    if args.once:
        snapshot_time = get_latest_snapshot_time(table)
        sync_once(feishu, spreadsheet_token, sheet_id, snapshot_time)
        return

    log(f"Polling `{table}`.updated_at every {args.poll_interval}s; "
        f"syncing to Feishu only when it changes. Press Ctrl+C to stop.\n")
    last_seen = None
    try:
        while True:
            try:
                latest = get_latest_snapshot_time(table)
            except Exception as e:
                log(f"Error checking updated_at: {e}", err=True)
                time.sleep(args.poll_interval)
                continue

            if latest is not None and latest != last_seen:
                log(f"Detected new snapshot (updated_at={latest}) — syncing to Feishu...")
                try:
                    sync_once(feishu, spreadsheet_token, sheet_id, latest)
                    last_seen = latest
                except Exception as e:
                    log(f"Error syncing to Feishu: {e}", err=True)
                    # last_seen deliberately not updated, so we retry on the next poll
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log("\nStopped.")


if __name__ == "__main__":
    main()