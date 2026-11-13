#!/usr/bin/env python3
"""
Shared TikTok Shop Open API client for TU / VB.

Holds everything that is not order- or product-specific:
    - per-store credentials loaded from the project-root .env  (SHOPS)
    - request signing (HMAC-SHA256, API 202309+)
    - access-token refresh, persisted back to .env
    - call_api(): signed request with retries and safe error messages

Used by:
    tkorders/tiktokorders.py
    tkproducts/tkproducts.py
    tkapi/tiktok_webhook.py

Required .env keys:
    TTS_TU_APP_KEY / TTS_TU_APP_SECRET / TTS_TU_ACCESS_TOKEN /
    TTS_TU_REFRESH_TOKEN / TTS_TU_SHOP_CIPHER
    (same for TTS_VB_*)
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH, override=True)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tkapi.envfile import update_env_values


BASE_URL = "https://open-api.tiktokglobalshop.com"
TOKEN_REFRESH_URL = "https://auth.tiktok-shops.com/api/v2/token/refresh"


# ---------------------------------------------------------
# Per-store app config
# ---------------------------------------------------------
def _shop_config(store_id: int, short_name: str) -> dict:
    prefix = f"TTS_{short_name}_"
    return {
        "store_id": store_id,
        "short_name": short_name,
        "app_key": os.environ.get(prefix + "APP_KEY", "").strip(),
        "app_secret": os.environ.get(prefix + "APP_SECRET", "").strip(),
        "access_token": os.environ.get(prefix + "ACCESS_TOKEN", "").strip(),
        "refresh_token": os.environ.get(prefix + "REFRESH_TOKEN", "").strip(),
        "shop_cipher": os.environ.get(prefix + "SHOP_CIPHER", "").strip(),
        "env_prefix": prefix,
    }


SHOPS = {
    1: _shop_config(1, "TU"),
    2: _shop_config(2, "VB"),
}


def shop_is_configured(shop: dict) -> bool:
    return bool(
        shop.get("app_key")
        and shop.get("app_secret")
        and shop.get("access_token")
        and shop.get("shop_cipher")
    )


def resolve_shop(value: str) -> dict:
    value = str(value).strip().upper()

    for shop in SHOPS.values():
        if value in {str(shop["store_id"]), shop["short_name"].upper()}:
            return shop

    raise ValueError(f"Unknown store: {value}. Use TU, VB, 1, or 2.")


def selected_shops(args: list[str]) -> list[dict]:
    if "--store" in args:
        idx = args.index("--store")
        if idx + 1 >= len(args):
            raise ValueError("Usage: --store TU|VB|1|2")
        return [resolve_shop(args[idx + 1])]

    return [shop for shop in SHOPS.values() if shop_is_configured(shop)]


# ---------------------------------------------------------
# Signing
# ---------------------------------------------------------
def sign_request(shop: dict, path: str, params: dict, body_str: str = "") -> str:
    filtered = {
        k: v for k, v in params.items()
        if k not in ("sign", "access_token")
    }
    param_str = "".join(
        f"{k}{v}" for k, v in sorted(filtered.items())
    )
    app_secret = shop["app_secret"]
    base_str = f"{app_secret}{path}{param_str}{body_str}{app_secret}"

    return hmac.new(
        app_secret.encode("utf-8"),
        base_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_common_params(shop: dict, extra: dict) -> dict:
    params = {
        "app_key": shop["app_key"],
        "timestamp": int(time.time()),
        "shop_cipher": shop["shop_cipher"],
    }
    params.update(extra)
    return params


# ---------------------------------------------------------
# Token refresh
# ---------------------------------------------------------
def refresh_access_token(shop: dict):
    if not shop.get("refresh_token"):
        raise RuntimeError(
            f"{shop['short_name']}: access token expired but no refresh token is configured."
        )

    print(f"[{shop['short_name']}] Access token expired — refreshing...")

    params = {
        "app_key": shop["app_key"],
        "app_secret": shop["app_secret"],
        "refresh_token": shop["refresh_token"],
        "grant_type": "refresh_token",
    }

    resp = requests.get(
        TOKEN_REFRESH_URL,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"{shop['short_name']}: token refresh failed: {data}"
        )

    token_data = data["data"]
    shop["access_token"] = token_data["access_token"]
    shop["refresh_token"] = token_data["refresh_token"]

    if not ENV_PATH.exists():
        print(f"Warning: {ENV_PATH} not found; refreshed token was not persisted.")
        return

    prefix = shop["env_prefix"]
    update_env_values({
        prefix + "ACCESS_TOKEN": token_data["access_token"],
        prefix + "REFRESH_TOKEN": token_data["refresh_token"],
    })

    print(f"[{shop['short_name']}] Token refreshed and saved to .env")


# ---------------------------------------------------------
# API call
# ---------------------------------------------------------
class TikTokAPIError(RuntimeError):
    """API failure with safe metadata; never expose signed URLs or tokens."""

    def __init__(self, message, status_code=None, api_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


def call_api(
    shop: dict,
    method: str,
    path: str,
    params: dict,
    body: dict | None = None,
    _retried: bool = False,
):
    """Retry transient failures with a fresh timestamp/signature each attempt."""
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    url = f"{BASE_URL}{path}"
    max_attempts = 6  # initial attempt plus five retries

    for attempt in range(max_attempts):
        # Timestamp and signature must be regenerated for every retry.
        all_params = build_common_params(shop, params)
        all_params["sign"] = sign_request(shop, path, all_params, body_str)
        all_params["access_token"] = shop["access_token"]
        headers = {
            "x-tts-access-token": shop["access_token"],
            "Content-Type": "application/json",
        }

        try:
            if method == "GET":
                resp = requests.get(url, params=all_params, headers=headers, timeout=20)
            else:
                resp = requests.post(
                    url, params=all_params, headers=headers,
                    data=body_str.encode("utf-8"), timeout=20,
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < max_attempts - 1:
                delay = min(32, 2 ** (attempt + 1))
                print(f"[{shop['short_name']}] {method} {path}: network error "
                      f"({type(exc).__name__}), retry {attempt + 1}/5 in {delay}s", flush=True)
                time.sleep(delay)
                continue
            raise TikTokAPIError(
                f"[{shop['short_name']}] {method} {path}: network failure after retries "
                f"({type(exc).__name__})"
            ) from None

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        api_code = payload.get("code") if isinstance(payload, dict) else None

        if resp.status_code == 401 and api_code == 105002 and not _retried:
            refresh_access_token(shop)
            return call_api(shop, method, path, params, body, _retried=True)

        transient = (resp.status_code in (429, 500, 502, 503, 504)
                     or api_code == 36009003)
        if transient and attempt < max_attempts - 1:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_seconds = int(retry_after)
            except (ValueError, TypeError):
                retry_seconds = 0
            delay = max(min(32, 2 ** (attempt + 1)), min(60, retry_seconds))
            print(f"[{shop['short_name']}] {method} {path}: "
                  f"HTTP {resp.status_code}, API code {api_code}; "
                  f"retry {attempt + 1}/5 in {delay}s", flush=True)
            time.sleep(delay)
            continue

        if not resp.ok or not isinstance(payload, dict) or api_code != 0:
            # Do not call raise_for_status(): requests includes access_token in its URL.
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            raise TikTokAPIError(
                f"[{shop['short_name']}] {method} {path}: HTTP {resp.status_code}, "
                f"API code {api_code}, request_id {request_id}",
                status_code=resp.status_code, api_code=api_code,
            )
        return payload["data"]

    raise AssertionError("Unreachable retry state")
