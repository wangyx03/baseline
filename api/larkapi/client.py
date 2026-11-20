"""Feishu API client; sheet-specific business logic remains in inventory/to_sheet.py."""
import time
from typing import Any, Dict, List
import requests

def log(msg: str = "", err: bool = False) -> None:
    import sys
    print(msg, file=sys.stderr if err else sys.stdout)

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


def _col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letters (1 -> A, 27 -> AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"

