#!/usr/bin/env python3
"""
get_shop_cipher.py

Call the Get Authorized Shops endpoint using an existing access_token,
to retrieve shop_id / shop_cipher — required for calling order/product
endpoints. Only needs to be run once; save the resulting shop_cipher
into your .env file.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)  # .env lives one level up, in the repo root (shared with inventory_snapshot.py etc), not inside tkorders/ itself

APP_KEY = os.environ.get("TTS_APP_KEY", "YOUR_APP_KEY")
APP_SECRET = os.environ.get("TTS_APP_SECRET", "YOUR_APP_SECRET")
ACCESS_TOKEN = os.environ.get("TTS_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
BASE_URL = "https://open-api.tiktokglobalshop.com"
PATH = "/authorization/202309/shops"


def sign_request(path: str, params: dict) -> str:
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    param_str = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    base_str = f"{APP_SECRET}{path}{param_str}{APP_SECRET}"
    return hmac.new(APP_SECRET.encode(), base_str.encode(), hashlib.sha256).hexdigest()


def main():
    params = {
        "app_key": APP_KEY,
        "shop_id": "",
        "timestamp": int(time.time()),
    }
    params["sign"] = sign_request(PATH, params)
    params["access_token"] = ACCESS_TOKEN  # TikTok's own generated cURL sends this as a query param too

    resp = requests.get(
        f"{BASE_URL}{PATH}",
        params=params,
        headers={"x-tts-access-token": ACCESS_TOKEN},
        timeout=15,
    )
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()