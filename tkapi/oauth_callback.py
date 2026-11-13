#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TikTok Shop OAuth Callback

支持：
    Truth Unfolds (TU)
    Vesper Books   (VB)

Callback URL:
    /tiktok/tu/callback
    /tiktok/vb/callback

Run (from the baseline root):
    python tkapi/oauth_callback.py
    or: gunicorn -b 127.0.0.1:8504 tkapi.oauth_callback:app

流程：
    1. TikTok 回调 code
    2. code -> access_token / refresh_token
    3. 调用 Get Authorized Shops
    4. 获取 shop_cipher
    5. 自动写回项目根目录 .env

.env:
    TTS_TU_APP_KEY=
    TTS_TU_APP_SECRET=
    TTS_TU_ACCESS_TOKEN=
    TTS_TU_REFRESH_TOKEN=
    TTS_TU_SHOP_CIPHER=

    TTS_VB_APP_KEY=
    TTS_VB_APP_SECRET=
    TTS_VB_ACCESS_TOKEN=
    TTS_VB_REFRESH_TOKEN=
    TTS_VB_SHOP_CIPHER=
"""

import hashlib
import hmac
import html
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request


# ============================================================
# Basic config
# ============================================================

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

TOKEN_URL = "https://auth.tiktok-shops.com/api/v2/token/get"

SHOPS_URL = (
    "https://open-api.tiktokglobalshop.com"
    "/authorization/202309/shops"
)

HTTP_TIMEOUT = 20


# ============================================================
# Store definitions
# ============================================================

STORE_CONFIG = {
    "tu": {
        "name": "Truth Unfolds",
        "prefix": "TTS_TU_",
    },

    "vb": {
        "name": "Vesper Books",
        "prefix": "TTS_VB_",
    },
}


# ============================================================
# .env read / write: shared with tkapi.client (tkapi/envfile.py)
# ============================================================

from tkapi.envfile import ENV_FILE, read_env_file, update_env_values


# ============================================================
# Get store credentials
# ============================================================

def get_store_config(store: str) -> dict:

    store = store.lower().strip()

    if store not in STORE_CONFIG:
        raise RuntimeError(
            f"Unsupported store: {store}"
        )

    definition = STORE_CONFIG[store]

    prefix = definition["prefix"]

    env = read_env_file()

    app_key = env.get(
        prefix + "APP_KEY",
        "",
    ).strip()

    app_secret = env.get(
        prefix + "APP_SECRET",
        "",
    ).strip()

    if not app_key:
        raise RuntimeError(
            f"{prefix}APP_KEY is empty"
        )

    if not app_secret:
        raise RuntimeError(
            f"{prefix}APP_SECRET is empty"
        )

    return {
        "code": store,
        "name": definition["name"],
        "prefix": prefix,
        "app_key": app_key,
        "app_secret": app_secret,
    }


# ============================================================
# TikTok API signature
# ============================================================

def generate_signature(
    url: str,
    params: dict,
    app_secret: str,
    body: str = "",
) -> str:
    """
    TikTok Shop Open API 202309+ signature.

    规则：

    1. 排除 sign / access_token
    2. query key 按字母排序
    3. 拼：
         path
         + key1 + value1
         + key2 + value2
         ...
    4. 非 multipart/form-data 时继续拼 body
    5. app_secret 作为 HMAC-SHA256 key
    """

    parsed = urlparse(url)

    path = parsed.path

    sign_params = {}

    for key, value in params.items():

        if key in (
            "sign",
            "access_token",
        ):
            continue

        if value is None:
            continue

        sign_params[key] = value

    pieces = [path]

    for key in sorted(sign_params.keys()):

        pieces.append(str(key))
        pieces.append(str(sign_params[key]))

    if body:
        pieces.append(body)

    message = app_secret + "".join(pieces) + app_secret

    signature = hmac.new(
        app_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature


# ============================================================
# OAuth code -> tokens
# ============================================================

def exchange_code_for_token(
    auth_code: str,
    app_key: str,
    app_secret: str,
) -> dict:

    params = {
        "app_key": app_key,
        "app_secret": app_secret,
        "auth_code": auth_code,
        "grant_type": "authorized_code",
    }

    response = requests.get(
        TOKEN_URL,
        params=params,
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except Exception:
        raise RuntimeError(
            "TikTok token API returned invalid JSON"
        )

    if payload.get("code") != 0:

        code = payload.get("code")
        message = payload.get("message")

        raise RuntimeError(
            f"TikTok token API failed: "
            f"code={code}, "
            f"message={message}"
        )

    data = payload.get("data") or {}

    access_token = data.get(
        "access_token"
    )

    refresh_token = data.get(
        "refresh_token"
    )

    if not access_token:
        raise RuntimeError(
            "TikTok did not return access_token"
        )

    if not refresh_token:
        raise RuntimeError(
            "TikTok did not return refresh_token"
        )

    return data


# ============================================================
# Get Authorized Shops
# ============================================================

def get_authorized_shops(
    access_token: str,
    app_key: str,
    app_secret: str,
) -> list:

    timestamp = int(time.time())

    params = {
        "app_key": app_key,
        "timestamp": timestamp,
    }

    sign = generate_signature(
        url=SHOPS_URL,
        params=params,
        app_secret=app_secret,
    )

    params["sign"] = sign

    headers = {
        "Content-Type": "application/json",
        "x-tts-access-token": access_token,
    }

    response = requests.get(
        SHOPS_URL,
        params=params,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )

    if not response.ok:
        try:
            error = response.json()
            app.logger.error(
                "Get Authorized Shops failed: HTTP=%s, code=%s, message=%s, request_id=%s",
                response.status_code,
                error.get("code"),
                error.get("message"),
                error.get("request_id"),
            )
        except ValueError:
            app.logger.error(
                "Get Authorized Shops failed: HTTP=%s, non-JSON response",
                response.status_code,
            )

    response.raise_for_status()

    try:
        payload = response.json()

    except Exception:

        raise RuntimeError(
            "TikTok shops API returned invalid JSON"
        )

    if payload.get("code") != 0:

        code = payload.get("code")
        message = payload.get("message")
        request_id = payload.get("request_id")

        raise RuntimeError(
            f"TikTok Get Authorized Shops failed: "
            f"code={code}, "
            f"message={message}, "
            f"request_id={request_id}"
        )

    data = payload.get("data") or {}

    shops = data.get("shops") or []

    if not shops:
        raise RuntimeError(
            "No authorized shops returned by TikTok"
        )

    return shops


# ============================================================
# Pick shop
# ============================================================

def select_shop(shops: list) -> dict:
    """
    当前 TU 和 VB 分别是独立 App。

    每个 App 授权自己的店，因此正常情况下
    Authorized Shops 应只返回当前授权店。

    如果返回多个：
    暂时使用第一个。
    """

    if not shops:
        raise RuntimeError(
            "Authorized shop list is empty"
        )

    shop = shops[0]

    cipher = (
        shop.get("cipher")
        or ""
    ).strip()

    if not cipher:
        raise RuntimeError(
            "TikTok shop response does not contain cipher"
        )

    return shop


# ============================================================
# HTML
# ============================================================

def success_page(
    store_name: str,
) -> str:

    safe_name = html.escape(
        store_name
    )

    return f"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>TikTok Shop Authorization</title>

<style>

body {{
    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background: #f6f7f9;

    margin: 0;

    padding: 40px;
}}

.card {{

    max-width: 600px;

    margin: 60px auto;

    background: white;

    padding: 32px;

    border-radius: 12px;

    box-shadow:
        0 4px 20px
        rgba(0,0,0,0.08);
}}

.success {{

    color: #14804a;

    font-size: 26px;

    font-weight: 600;
}}

.store {{

    margin-top: 24px;

    font-size: 18px;
}}

.message {{

    margin-top: 20px;

    line-height: 1.6;

    color: #444;
}}

</style>

</head>

<body>

<div class="card">

<div class="success">
Authorization successful
</div>

<div class="store">
Store:
<strong>{safe_name}</strong>
</div>

<div class="message">

Access token,
refresh token
and shop cipher
have been saved successfully.

<br><br>

You may close this page.

</div>

</div>

</body>

</html>
"""


def error_page(
    message: str,
) -> str:

    safe_message = html.escape(
        message
    )

    return f"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>TikTok Shop Authorization Error</title>

<style>

body {{

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background: #f6f7f9;

    margin: 0;

    padding: 40px;
}}

.card {{

    max-width: 700px;

    margin: 60px auto;

    background: white;

    padding: 32px;

    border-radius: 12px;

    box-shadow:
        0 4px 20px
        rgba(0,0,0,0.08);
}}

.error {{

    color: #c62828;

    font-size: 26px;

    font-weight: 600;
}}

.message {{

    margin-top: 20px;

    line-height: 1.6;

    color: #444;

    word-break: break-word;
}}

</style>

</head>

<body>

<div class="card">

<div class="error">
Authorization failed
</div>

<div class="message">
{safe_message}
</div>

</div>

</body>

</html>
"""


# ============================================================
# Callback handler
# ============================================================

def handle_callback(
    store: str,
):

    auth_code = (
        request.args.get(
            "code",
            "",
        )
        or ""
    ).strip()

    if not auth_code:

        return error_page(
            "Missing OAuth authorization code."
        ), 400

    try:

        config = get_store_config(
            store
        )

        # ----------------------------------------------------
        # Step 1
        # auth code -> access token + refresh token
        # ----------------------------------------------------

        token_data = exchange_code_for_token(
            auth_code=auth_code,
            app_key=config["app_key"],
            app_secret=config["app_secret"],
        )

        access_token = token_data[
            "access_token"
        ]

        refresh_token = token_data[
            "refresh_token"
        ]

        # ----------------------------------------------------
        # Step 2
        # access token -> authorized shops
        # ----------------------------------------------------

        shops = get_authorized_shops(
            access_token=access_token,
            app_key=config["app_key"],
            app_secret=config["app_secret"],
        )

        shop = select_shop(
            shops
        )

        shop_cipher = shop[
            "cipher"
        ]

        # ----------------------------------------------------
        # Step 3
        # Save current store only
        # ----------------------------------------------------

        prefix = config[
            "prefix"
        ]

        update_env_values({

            prefix
            + "ACCESS_TOKEN":
                access_token,

            prefix
            + "REFRESH_TOKEN":
                refresh_token,

            prefix
            + "SHOP_CIPHER":
                shop_cipher,
        })

        # ----------------------------------------------------
        # Do NOT print tokens
        # ----------------------------------------------------

        app.logger.info(
            "TikTok authorization successful: store=%s",
            store,
        )

        return success_page(
            config["name"]
        ), 200

    except requests.RequestException as exc:

        app.logger.exception(
            "TikTok HTTP request failed: store=%s",
            store,
        )

        return error_page(
            f"TikTok API request failed: "
            f"{type(exc).__name__}"
        ), 502

    except Exception as exc:

        app.logger.exception(
            "TikTok OAuth failed: store=%s",
            store,
        )

        return error_page(
            str(exc)
        ), 500


# ============================================================
# Routes
# ============================================================

@app.route(
    "/tiktok/tu/callback",
    methods=["GET"],
)
def tiktok_tu_callback():

    return handle_callback(
        "tu"
    )


@app.route(
    "/tiktok/vb/callback",
    methods=["GET"],
)
def tiktok_vb_callback():

    return handle_callback(
        "vb"
    )


# ============================================================
# Health
# ============================================================

@app.route(
    "/health",
    methods=["GET"],
)
def health():

    return {
        "status": "ok",
        "service": "tiktok-oauth",
        "env_file": str(ENV_FILE),
    }, 200


# ============================================================
# Root
# ============================================================

@app.route(
    "/",
    methods=["GET"],
)
def root():

    return {
        "service": "TikTok Shop OAuth Callback",
        "status": "running",
        "callbacks": {
            "TU": "/tiktok/tu/callback",
            "VB": "/tiktok/vb/callback",
        },
    }, 200


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=8504,
        debug=False,
    )