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
    updated_at          DATETIME     NOT NULL,
    PRIMARY KEY (sku, warehouse, stock_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

=============================================================================
2. Install dependencies on the VPS
=============================================================================
    pip install requests pymysql python-dotenv --break-system-packages

=============================================================================
3. .env file (same directory as this script)
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
    python oms_inventory_to_mysql.py

    # Refresh every hour, forever (run this under systemd, see below)
    python oms_inventory_to_mysql.py --watch 3600

    # Filter to specific SKUs / warehouses
    python oms_inventory_to_mysql.py --sku ABC123,DEF456 --warehouse M60003

    # Skip the (slower) in-transit computation for a quick stock-only run
    python oms_inventory_to_mysql.py --skip-transit

=============================================================================
5. systemd service (for --watch mode, same pattern as your other daemons)
=============================================================================
    [Unit]
    Description=OMS inventory -> MySQL sync
    After=network.target mysql.service

    [Service]
    WorkingDirectory=/path/to/script
    ExecStart=/usr/bin/python3 oms_inventory_to_mysql.py --watch 3600
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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import requests
import pymysql

EASTERN_TZ = ZoneInfo("America/Detroit")

def log(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


OMS_API_BASE = "https://api.xlwms.com/openapi"
INVENTORY_PATH = "/v1/integratedInventory/pageOpen"
PRODUCT_LIST_PATH = "/v1/product/pagelist"
INBOUND_ORDER_LIST_PATH = "/v1/inboundOrder/pageList"
INBOUND_BOX_SKU_LIST_PATH = "/v1/inboundOrder/pageBoxSkuList"

OPEN_INBOUND_STATUSES = (1, 2)  # 1-待入库 2-收货中


def _monthly_windows(start_dt: datetime, end_dt: datetime, max_span_days: int = 30):
    cur = start_dt
    step = timedelta(days=max_span_days)
    while cur < end_dt:
        window_end = min(cur + step, end_dt)
        yield cur, window_end
        cur = window_end


class AuthError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# OMS client (product catalog + stock + in-transit only — trimmed from
# the full oms_inventory.py; no outbound/locked-stock logic, since that
# was already disabled by default there too)
# ----------------------------------------------------------------------
class OmsClient:
    def __init__(self, app_key: str, app_secret: str, base_url: str = OMS_API_BASE):
        if not app_key or not app_secret:
            raise AuthError("Missing OMS_APP_KEY / OMS_APP_SECRET")
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url
        self.session = requests.Session()

    @staticmethod
    def _canonical_json(obj: Any) -> str:
        def sort_key(item):
            return item[0].lower()

        def _sort(o):
            if isinstance(o, dict):
                return {k: _sort(v) for k, v in sorted(o.items(), key=sort_key)}
            if isinstance(o, list):
                return [_sort(v) for v in o]
            return o

        return json.dumps(_sort(obj), ensure_ascii=False, separators=(",", ":"))

    def _authcode(self, data: Dict[str, Any], req_time: str) -> str:
        data_json = self._canonical_json(data)
        plain = f"{self.app_key}{data_json}{req_time}"
        return hmac.new(self.app_secret.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        req_time = str(int(time.time()))
        authcode = self._authcode(data, req_time)
        url = f"{self.base_url}{path}"
        payload = {"appKey": self.app_key, "reqTime": req_time, "data": data}
        resp = self.session.post(url, params={"authcode": authcode}, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 200:
            raise RuntimeError(f"OMS API returned an error ({path}): {result}")
        return result["data"]

    def query_inventory(self, sku_list=None, wh_code_list=None, stock_type=None,
                         page_size: int = 100, lookback_days: int = 3650) -> List[Dict[str, Any]]:
        all_records: List[Dict[str, Any]] = []
        page = 1
        start_time = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
        end_time = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        while True:
            data: Dict[str, Any] = {
                "page": page, "pageSize": page_size, "timeType": "operateTime",
                "startTime": start_time, "endTime": end_time,
            }
            if sku_list:
                data["skuList"] = ",".join(sku_list)
            if wh_code_list:
                data["whCodeList"] = ",".join(wh_code_list)
            if stock_type is not None:
                data["stockType"] = stock_type
            result = self._post(INVENTORY_PATH, data)
            records = result.get("records", []) or []
            all_records.extend(records)
            total = result.get("total", 0)
            if page * page_size >= total or not records:
                break
            page += 1
        return all_records

    def query_product_catalog(self, sku_list=None, page_size: int = 100) -> List[Dict[str, Any]]:
        all_records: List[Dict[str, Any]] = []
        page = 1
        while True:
            data: Dict[str, Any] = {"page": page, "pageSize": page_size}
            if sku_list:
                data["skuList"] = sku_list
            result = self._post(PRODUCT_LIST_PATH, data)
            records = result.get("records", []) or []
            all_records.extend(records)
            total = result.get("total", 0)
            if page * page_size >= total or not records:
                break
            page += 1
        return all_records

    def query_open_inbound_orders(self, page_size: int = 50, lookback_days: int = 60) -> List[Dict[str, Any]]:
        all_orders_by_no: Dict[str, Dict[str, Any]] = {}
        end_dt = datetime.now() + timedelta(days=2)
        start_dt = end_dt - timedelta(days=lookback_days)
        for window_start, window_end in _monthly_windows(start_dt, end_dt):
            start_time = window_start.strftime("%Y-%m-%d %H:%M:%S")
            end_time = window_end.strftime("%Y-%m-%d %H:%M:%S")
            for status in OPEN_INBOUND_STATUSES:
                page = 1
                while True:
                    data: Dict[str, Any] = {
                        "page": page, "pageSize": page_size, "status": status,
                        "startTime": start_time, "endTime": end_time,
                    }
                    result = self._post(INBOUND_ORDER_LIST_PATH, data)
                    records = result.get("records", []) or []
                    for rec in records:
                        order_no = rec.get("inboundOrderNo")
                        if order_no:
                            all_orders_by_no[order_no] = rec
                    total = result.get("total", 0)
                    if page * page_size >= total or not records:
                        break
                    page += 1
        return list(all_orders_by_no.values())

    def query_inbound_box_sku_list(self, inbound_order_no: str, inbound_type: int,
                                    page_size: int = 100) -> List[Dict[str, Any]]:
        all_boxes: List[Dict[str, Any]] = []
        page = 1
        while True:
            data: Dict[str, Any] = {
                "inboundOrderNo": inbound_order_no, "inboundType": inbound_type,
                "page": page, "pageSize": page_size,
            }
            result = self._post(INBOUND_BOX_SKU_LIST_PATH, data)
            records = result.get("records", []) or []
            all_boxes.extend(records)
            total = result.get("total", 0)
            pages = result.get("pages", 1)
            if page >= pages or not records:
                break
            page += 1
        return all_boxes

    def compute_in_transit_by_sku(self, wh_code_filter=None, sku_filter=None,
                                   lookback_days: int = 60) -> Dict[Tuple[str, str], int]:
        in_transit: Dict[Tuple[str, str], int] = {}
        orders = self.query_open_inbound_orders(lookback_days=lookback_days)
        wh_filter_set = set(wh_code_filter) if wh_code_filter else None
        sku_filter_set = set(sku_filter) if sku_filter else None
        log(f"Found {len(orders)} open inbound order(s) to inspect for in-transit quantities.")
        for order in orders:
            wh_code = order.get("whCode", "")
            if wh_filter_set is not None and wh_code not in wh_filter_set:
                continue
            order_no = order.get("inboundOrderNo")
            inbound_type = order.get("inboundType")
            if not order_no or inbound_type is None:
                continue
            try:
                boxes = self.query_inbound_box_sku_list(order_no, inbound_type)
            except Exception as e:
                log(f"Warning: failed to fetch packing detail for inbound order {order_no}: {e}", err=True)
                continue
            for box in boxes:
                for prod in box.get("productList", []) or []:
                    sku = prod.get("sku", "")
                    if not sku:
                        continue
                    if sku_filter_set is not None and sku not in sku_filter_set:
                        continue
                    qty = prod.get("quantity", 0) or 0
                    received = prod.get("receivedQuantity", 0) or 0
                    pending = max(qty - received, 0)
                    if pending <= 0:
                        continue
                    key = (sku, wh_code)
                    in_transit[key] = in_transit.get(key, 0) + pending
        return in_transit


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
    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str, table: str = "inventory_snapshot"):
        if not all([host, user, password, database]):
            raise RuntimeError("Missing DB_HOST / DB_USER / DB_PASSWORD / DB_NAME")
        self.host, self.port, self.user, self.password, self.database, self.table = (
            host, port, user, password, database, table
        )

    def _connect(self):
        return pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            database=self.database, charset="utf8mb4", autocommit=False,
        )

    def write_full(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            log("No data, skipping write to MySQL.")
            return
        table = self.table
        staging_table = f"{table}_staging"
        old_table = f"{table}_old"

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{staging_table}`")
                cur.execute(f"CREATE TABLE `{staging_table}` LIKE `{table}`")

                now = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")
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
                cur.executemany(insert_sql, values)

                cur.execute(f"DROP TABLE IF EXISTS `{old_table}`")
                cur.execute(f"RENAME TABLE `{table}` TO `{old_table}`, `{staging_table}` TO `{table}`")
                cur.execute(f"DROP TABLE IF EXISTS `{old_table}`")

            conn.commit()
            log(f"Wrote {len(rows)} row(s) to MySQL table `{self.database}`.`{table}`")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


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
            host=os.environ.get("DB_HOST", ""),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", ""),
            password=os.environ.get("DB_PASSWORD", ""),
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