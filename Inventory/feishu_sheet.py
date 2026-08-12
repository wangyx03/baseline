"""
feishu_sheet.py — Minimal Feishu (Lark) Sheets v2 client: auth + overwrite-range
grid writes. Not specific to any one script — reusable by any daemon that needs
to push a 2D grid of values into a Feishu sheet (inventory_query_to_feishu.py,
sku_sync.py's stats worker, etc).
"""

import re
import time
from typing import Any, Dict, List

import requests


def extract_spreadsheet_token(sheet_url: str) -> str:
    """Pull the spreadsheet token out of a Feishu sheet URL, e.g.
    https://xcn3xthf3pue.feishu.cn/sheets/UnSRsCAfGhDWkitWOwvcb8o6nRc(?sheet=...)
    -> UnSRsCAfGhDWkitWOwvcb8o6nRc"""
    match = re.search(r"/sheets/([A-Za-z0-9]+)", sheet_url)
    if not match:
        raise RuntimeError(f"Could not parse spreadsheet token out of sheet URL: {sheet_url!r}")
    return match.group(1)


def _col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letters (1 -> A, 27 -> AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


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
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def write_grid(self, spreadsheet_token: str, sheet_id: str, rows: List[List[Any]],
                    clear_rows: int = 2000, clear_cols: int = 10) -> None:
        """Blank a generous fixed-size range first (so stale rows past the end of
        the new result set don't linger), then write the new grid on top."""
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=0)

        blank_row_count = min(clear_rows, max(n_rows, 1) + 50)
        blank_values = [[""] * clear_cols for _ in range(blank_row_count)]
        self._put_values(
            spreadsheet_token,
            f"{sheet_id}!A1:{_col_letter(clear_cols)}{blank_row_count}",
            blank_values,
        )

        if n_rows == 0:
            return

        data_range = f"{sheet_id}!A1:{_col_letter(n_cols)}{n_rows}"
        self._put_values(spreadsheet_token, data_range, rows)

    def write_cell(self, spreadsheet_token: str, sheet_id: str, cell: str, value: Any) -> None:
        """Write a single value to one cell, e.g. a 'last synced at' timestamp
        placed next to the data grid. Call this *after* write_grid, since
        write_grid's blanking step covers a wider range than the data itself."""
        self._put_values(spreadsheet_token, f"{sheet_id}!{cell}:{cell}", [[value]])

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