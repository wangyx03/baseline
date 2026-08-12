"""
inventory_query.py — TU/Vb locked-inventory query against
inventory_snapshot + book_sku + weekly_inventory, shaped into a grid
ready to hand to a Feishu writer. Uses the project's shared db.get_db().
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

# db.py lives one directory up (baseline/), this file lives in baseline/inventory/.
# Make sure the parent dir is importable no matter how this module gets run
# (direct script, systemd, or imported as part of the inventory package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_db

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


def fetch_locked_inventory() -> List[Dict[str, Any]]:
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(QUERY)
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        db.close()


def to_grid(records: List[Dict[str, Any]]) -> List[List[Any]]:
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