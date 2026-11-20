"""Shared XLWMS OMS API client; extracted from inventory_snapshot.py."""
import time
import hmac
import json
import hashlib
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
import requests


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

