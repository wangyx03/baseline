"""
inventory_query_to_feishu.py — Run the TU/Vb locked-inventory query and
write the result into a Feishu (Lark) spreadsheet, overwriting a fixed range.

Logic now lives in two reusable modules:
    inventory_query.py — the MySQL query + row shaping (db.py owns the connection)
    feishu_sheet.py     — the Feishu auth + write-grid client (reusable elsewhere)
This file just wires them together and handles the CLI / watch loop.

=============================================================================
.env keys used
=============================================================================
    DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME   (read by db.py)
    FEISHU_APP_ID / FEISHU_APP_SECRET
    FEISHU_SHEET_URL=https://xcn3xthf3pue.feishu.cn/sheets/UnSRsCAfGhDWkitWOwvcb8o6nRc
    FEISHU_SHEET_ID=Alyix3
    WATCH_INTERVAL_SECONDS=3600   (optional; 0/absent = run once)

=============================================================================
Usage — run from baseline/ (uses relative imports, so this needs -m)
=============================================================================
    python -m inventory.inventory_query_to_feishu            # run once
    python -m inventory.inventory_query_to_feishu --watch 3600
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# db.py / utils.py live one directory up (baseline/), this file lives in
# baseline/inventory/. Make sure the parent dir is importable regardless of
# how this script gets invoked (direct run, systemd, -m, etc).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .feishu_sheet import FeishuClient, extract_spreadsheet_token
from .inventory_query import fetch_locked_inventory, to_grid
from utils import format_et

# Cell to the right of the 8-column data grid (A-H), so it never overlaps
# with data rows regardless of how many rows the query returns.
TIMESTAMP_CELL = "J1"


def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


def sync_once(feishu: FeishuClient, spreadsheet_token: str, sheet_id: str) -> None:
    log("Running MySQL query...")
    records = fetch_locked_inventory()
    log(f"  -> {len(records)} row(s)")
    grid = to_grid(records)
    feishu.write_grid(spreadsheet_token, sheet_id, grid)

    updated_at = format_et(datetime.utcnow())
    feishu.write_cell(spreadsheet_token, sheet_id, TIMESTAMP_CELL, f"Renew at: {updated_at}")

    log(f"Wrote {len(grid) - 1} data row(s) + header to Feishu sheet {sheet_id}.")
    log(f"Updated timestamp cell {TIMESTAMP_CELL}: {updated_at}")


def main():
    parser = argparse.ArgumentParser(description="Sync inventory query results from MySQL into a Feishu sheet")
    parser.add_argument("--watch", type=int, default=int(os.environ.get("WATCH_INTERVAL_SECONDS", "0") or "0"),
                         metavar="SECONDS")
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

    if args.watch > 0:
        log(f"Entering watch mode, refreshing every {args.watch} seconds. Press Ctrl+C to stop.\n")
        try:
            while True:
                sync_once(feishu, spreadsheet_token, sheet_id)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log("\nWatch mode stopped.")
    else:
        sync_once(feishu, spreadsheet_token, sheet_id)


if __name__ == "__main__":
    main()