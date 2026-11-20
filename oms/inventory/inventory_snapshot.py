"""
oms_inventory_to_mysql.py — Query real-time inventory (+ true inbound in-transit)
from the OMS and write it directly into a MySQL table.

This is a trimmed-down version of oms_inventory.py: same OMS querying logic
(product catalog + on-hand stock + in-transit from open inbound orders), but
writes ONLY to MySQL — no Feishu involved.

=============================================================================
1. One-time table setup (run once in your DB, e.g. `olselling`)
=============================================================================

CREATE TABLE IF NOT EXISTS inventory_snapshot (
    sku                 VARCHAR(64)  NOT NULL,
    product_name        VARCHAR(255),
    warehouse           VARCHAR(32)  NOT NULL,
    stock_type          VARCHAR(16)  NOT NULL,
    total_stock         INT          DEFAULT 0,
    available_stock     INT          DEFAULT 0,
    locked_stock        INT          DEFAULT 0,
    inbound_in_transit  INT          DEFAULT 0,
    updated_at          DATETIME     NOT NULL,  -- stored as UTC, see write_full() below
    PRIMARY KEY (sku, warehouse, stock_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

=============================================================================
2. Install dependencies on the VPS
=============================================================================
    pip install requests mysql-connector-python python-dotenv --break-system-packages

    DB access goes through the shared baseline/db.py (get_db(), mysql.connector)
    instead of a local pymysql connection.

=============================================================================
3. .env file (baseline/.env, shared with db.py)
=============================================================================
    OMS_APP_KEY=your AppKey
    OMS_APP_SECRET=your AppSecret

    DB_HOST=127.0.0.1
    DB_PORT=3306
    DB_USER=your_user
    DB_PASSWORD=your_password
    DB_NAME=olselling
    DB_TABLE=inventory_snapshot

=============================================================================
4. Usage
=============================================================================
    # Run once
    python oms/inventory/inventory_snapshot.py

    # Refresh every hour, forever (run this under systemd, see below)
    python oms/inventory/inventory_snapshot.py --watch 3600

    # Filter to specific SKUs / warehouses
    python oms/inventory/inventory_snapshot.py --sku ABC123,DEF456 --warehouse M60003

    # Skip the (slower) in-transit computation for a quick stock-only run
    python oms/inventory/inventory_snapshot.py --skip-transit

=============================================================================
5. systemd service (for --watch mode, same pattern as your other daemons)
=============================================================================
    [Unit]
    Description=OMS inventory -> MySQL sync
    After=network.target mysql.service

    [Service]
    WorkingDirectory=/opt/baseline
    ExecStart=/opt/baseline/venv/bin/python oms/inventory/inventory_snapshot.py --watch 3600
    Restart=always
    Environment=PYTHONUNBUFFERED=1

    [Install]
    WantedBy=multi-user.target
"""
import os
import sys
import time
import hmac
import json
import hashlib
import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")   # baseline/.env (this file is in oms/inventory/)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from db import get_db  # noqa: E402  (shared connection helper, baseline/db.py)

import requests


def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


from api.omsapi.client import OmsClient, AuthError, OMS_API_BASE


def flatten_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    prod = rec.get("productStockDtl") or {}
    stock_type_raw = rec.get("stockType")
    stock_type_label = {"0": "Good", "1": "Defective"}.get(str(stock_type_raw), str(stock_type_raw))
    return {
        "SKU": rec.get("sku", ""),
        "Product Name": rec.get("productName", ""),
        "Warehouse": rec.get("whCode", ""),
        "Stock Type": stock_type_label,
        "Total Stock (Dropship)": rec.get("productTotalAmount", 0),
        "Available Stock": prod.get("availableAmount", 0),
        "Locked Stock": prod.get("lockAmount", 0),
        "Inbound In-Transit": 0,
    }


ROW_COLUMNS = [
    "SKU", "Product Name", "Warehouse", "Stock Type",
    "Total Stock (Dropship)", "Available Stock", "Locked Stock", "Inbound In-Transit",
]


def build_combined_rows(products, stock_records, in_transit_map) -> List[Dict[str, Any]]:
    product_names = {p.get("sku", ""): p.get("productName", "") for p in products if p.get("sku")}
    all_master_skus = set(product_names.keys())
    combined: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for rec in stock_records:
        flat = flatten_record(rec)
        if flat["SKU"] in product_names and product_names[flat["SKU"]]:
            flat["Product Name"] = product_names[flat["SKU"]]
        key = (flat["SKU"], flat["Warehouse"], flat["Stock Type"])
        combined[key] = flat

    for (sku, warehouse), qty in in_transit_map.items():
        key = (sku, warehouse, "Good")
        if key in combined:
            combined[key]["Inbound In-Transit"] = combined[key].get("Inbound In-Transit", 0) + qty
        else:
            combined[key] = {
                "SKU": sku, "Product Name": product_names.get(sku, ""), "Warehouse": warehouse,
                "Stock Type": "Good", "Total Stock (Dropship)": 0, "Available Stock": 0,
                "Locked Stock": 0, "Inbound In-Transit": qty,
            }

    skus_with_data = {k[0] for k in combined.keys()}
    for sku in sorted(all_master_skus - skus_with_data):
        key = (sku, "-", "-")
        combined[key] = {
            "SKU": sku, "Product Name": product_names.get(sku, ""), "Warehouse": "-",
            "Stock Type": "-", "Total Stock (Dropship)": 0, "Available Stock": 0,
            "Locked Stock": 0, "Inbound In-Transit": 0,
        }

    rows = list(combined.values())
    rows.sort(key=lambda r: (r["SKU"], r["Warehouse"], r["Stock Type"]))
    return [{col: r.get(col, 0 if col not in ("SKU", "Product Name", "Warehouse", "Stock Type") else "")
             for col in ROW_COLUMNS} for r in rows]


# ----------------------------------------------------------------------
# MySQL writer — swap-table full refresh (never leaves the live table
# empty or half-written, even if this run crashes partway through)
# ----------------------------------------------------------------------
class DbWriter:
    def __init__(self, database: str, table: str = "inventory_snapshot"):
        # database name is only needed here for the log line below — the
        # actual connection (host/port/user/password/time_zone) comes from
        # the shared get_db() in baseline/db.py.
        if not database:
            raise RuntimeError("Missing DB_NAME")
        self.database, self.table = database, table

    def write_full(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            log("No data, skipping write to MySQL.")
            return
        table = self.table
        staging_table = f"{table}_staging"
        old_table = f"{table}_old"

        db = get_db()
        cursor = db.cursor(dictionary=True)
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{staging_table}`")
            cursor.execute(f"CREATE TABLE `{staging_table}` LIKE `{table}`")

            # UTC, not local time — DATETIME columns don't carry timezone
            # info, so by convention every DATETIME here is UTC (matches the
            # time_zone='+00:00' session setting in db.py's get_db()).
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            insert_sql = (
                f"INSERT INTO `{staging_table}` "
                "(sku, product_name, warehouse, stock_type, total_stock, "
                "available_stock, locked_stock, inbound_in_transit, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            values = [
                (r["SKU"], r["Product Name"], r["Warehouse"], r["Stock Type"],
                 r["Total Stock (Dropship)"], r["Available Stock"],
                 r["Locked Stock"], r["Inbound In-Transit"], now)
                for r in rows
            ]
            cursor.executemany(insert_sql, values)

            cursor.execute(f"DROP TABLE IF EXISTS `{old_table}`")
            cursor.execute(f"RENAME TABLE `{table}` TO `{old_table}`, `{staging_table}` TO `{table}`")
            cursor.execute(f"DROP TABLE IF EXISTS `{old_table}`")

            db.commit()
            log(f"Wrote {len(rows)} row(s) to MySQL table `{self.database}`.`{table}`")
        except Exception:
            db.rollback()
            raise
        finally:
            cursor.close()
            db.close()


def run_once(client: OmsClient, args) -> List[Dict[str, Any]]:
    sku_list = args.sku.split(",") if args.sku else None
    wh_list = args.warehouse.split(",") if args.warehouse else None

    log("Fetching OMS product catalog...")
    products = client.query_product_catalog(sku_list=sku_list)
    log(f"  -> {len(products)} product(s)")

    log("Fetching on-hand stock...")
    stock_records = client.query_inventory(
        sku_list=sku_list, wh_code_list=wh_list, stock_type=args.stock_type,
        page_size=100, lookback_days=args.stock_lookback_days,
    )
    log(f"  -> {len(stock_records)} stock record(s)")

    in_transit_map: Dict[Tuple[str, str], int] = {}
    if not args.skip_transit:
        log("Computing inbound in-transit from open inbound orders...")
        in_transit_map = client.compute_in_transit_by_sku(
            wh_code_filter=wh_list, sku_filter=sku_list, lookback_days=args.transit_lookback_days
        )
        log(f"  -> in-transit computed for {len(in_transit_map)} (SKU, warehouse) pair(s)")
    else:
        log("Skipping in-transit computation (--skip-transit given).")

    return build_combined_rows(products, stock_records, in_transit_map)


def main():
    parser = argparse.ArgumentParser(description="Query OMS inventory and write it directly into MySQL")
    parser.add_argument("--sku")
    parser.add_argument("--warehouse")
    parser.add_argument("--stock-type", type=int, choices=[0, 1], default=None)
    parser.add_argument("--stock-lookback-days", type=int, default=3650)
    parser.add_argument("--skip-transit", action="store_true")
    parser.add_argument("--transit-lookback-days", type=int, default=60)
    parser.add_argument("--watch", type=int, default=int(os.environ.get("WATCH_INTERVAL_SECONDS", "0") or "0"),
                         metavar="SECONDS")
    args = parser.parse_args()

    try:
        client = OmsClient(os.environ.get("OMS_APP_KEY", ""), os.environ.get("OMS_APP_SECRET", ""))
        db_writer = DbWriter(
            database=os.environ.get("DB_NAME", ""),
            table=os.environ.get("DB_TABLE", "inventory_snapshot"),
        )
    except (AuthError, RuntimeError) as e:
        log(f"Error: {e}", err=True)
        sys.exit(1)

    def sync(rows):
        try:
            db_writer.write_full(rows)
        except Exception as e:
            log(f"Error writing to MySQL: {e}", err=True)

    if args.watch > 0:
        log(f"Entering watch mode, refreshing every {args.watch} seconds. Press Ctrl+C to stop.\n")
        try:
            while True:
                rows = run_once(client, args)
                log(f"\n=== Refreshed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
                sync(rows)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log("\nWatch mode stopped.")
    else:
        rows = run_once(client, args)
        sync(rows)


if __name__ == "__main__":
    main()