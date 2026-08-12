"""
inventory_query_to_feishu.py — Run the TU/Vb locked-inventory query against
MySQL (inventory_snapshot + book_sku + weekly_inventory) and write the
result straight into a Feishu (Lark) spreadsheet, overwriting a fixed range.

Companion to oms_inventory_to_mysql.py: that script fills inventory_snapshot,
this one reads it back out (joined with book metadata + weekly allocation
plans) and pushes the combined view to Feishu so it's viewable without
touching MySQL directly.

=============================================================================
1. Install dependencies on the VPS
=============================================================================
    pip install pymysql requests python-dotenv --break-system-packages

=============================================================================
2. .env additions (same file/keys as your other Feishu daemons already use)
=============================================================================
    FEISHU_APP_ID=cli_xxxxxxxx
    FEISHU_APP_SECRET=xxxxxxxx

    # Target Feishu spreadsheet
    FEISHU_SHEET_URL=https://xcn3xthf3pue.feishu.cn/sheets/UnSRsCAfGhDWkitWOwvcb8o6nRc
    FEISHU_SHEET_ID=Alyix3

    # Refresh interval in seconds, 3600 = every hour
    WATCH_INTERVAL_SECONDS=3600

=============================================================================
3. Usage
=============================================================================
    # Run once
    python inventory_query_to_feishu.py

    # Refresh every hour, forever (systemd, same pattern as your other daemons)
    python inventory_query_to_feishu.py --watch 3600

=============================================================================
4. systemd service
=============================================================================
    [Unit]
    Description=Inventory query -> Feishu sync
    After=network.target mysql.service

    [Service]
    WorkingDirectory=/path/to/script
    ExecStart=/usr/bin/python3 inventory_query_to_feishu.py --watch 3600
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

import requests
import pymysql
from pymysql.cursors import DictCursor


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
def run_query(host: str, port: int, user: str, password: str, database: str) -> List[Dict[str, Any]]:
    conn = pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4", cursorclass=DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            return cur.fetchall()
    finally:
        conn.close()


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


def sync_once(db_cfg: Dict[str, Any], feishu: FeishuClient, spreadsheet_token: str, sheet_id: str) -> None:
    log("Running MySQL query...")
    records = run_query(**db_cfg)
    log(f"  -> {len(records)} row(s)")
    grid = rows_to_grid(records)
    feishu.write_values(spreadsheet_token, sheet_id, grid)


def main():
    parser = argparse.ArgumentParser(description="Sync inventory query results from MySQL into a Feishu sheet")
    parser.add_argument("--watch", type=int, default=int(os.environ.get("WATCH_INTERVAL_SECONDS", "0") or "0"),
                         metavar="SECONDS")
    args = parser.parse_args()

    db_cfg = dict(
        host=os.environ.get("DB_HOST", ""),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", ""),
    )
    if not all([db_cfg["host"], db_cfg["user"], db_cfg["password"], db_cfg["database"]]):
        log("Error: missing DB_HOST / DB_USER / DB_PASSWORD / DB_NAME in .env", err=True)
        sys.exit(1)

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

    if args.watch > 0:
        log(f"Entering watch mode, refreshing every {args.watch} seconds. Press Ctrl+C to stop.\n")
        try:
            while True:
                sync_once(db_cfg, feishu, spreadsheet_token, sheet_id)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log("\nWatch mode stopped.")
    else:
        sync_once(db_cfg, feishu, spreadsheet_token, sheet_id)


if __name__ == "__main__":
    main()