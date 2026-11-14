#!/usr/bin/env python3
"""TikTok Shop LIVE session info sync -> MySQL table tkliveinfo.

Source API: Get Shop LIVE Performance List (scope data.shop_analytics.public.read).
One row per (store_id, live_id). Only session identity fields are stored as
columns; the full API record is kept in raw_json in case a field name below
turns out to differ from the real response.

Run from the baseline root:
    python tklive/tkliveinfo.py                              # all configured stores, last 7 days
    python tklive/tkliveinfo.py --store TU --days 30         # backfill one store
    python tklive/tkliveinfo.py --store TU --days 7 --dump   # print raw API JSON, no DB write

Scheduled daily by tkliveinfo-sync.timer; the web page calls run_sync() too.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tkapi import client as api  # noqa: E402  (loads .env)
import db  # noqa: E402  (baseline/db.py: get_db(), session time_zone = UTC)


# ---------------------------------------------------------
# API settings: VERIFY against the Partner Center doc page for
# "Get Shop LIVE Performance List" before the first run (use --dump).
# ---------------------------------------------------------
LIVE_LIST_PATH = "/analytics/202509/shop_lives/performance"
LIST_KEY = "shop_lives"            # name of the array inside response data
PAGE_SIZE = 100
MAX_PAGES = 200                    # safety stop
SHOP_TZ = ZoneInfo("America/Los_Angeles")  # analytics dates are in the shop's timezone

# Column -> candidate keys in the API record (first match wins, nested search).
FIELD_KEYS = {
    "live_id":          ("id", "live_id", "live_room_id"),
    "live_title":       ("title", "live_title"),
    "creator_username": ("username", "user_name", "creator_username"),
    "creator_nickname": ("nickname", "nick_name", "creator_nickname"),
    "creator_open_id":  ("open_id", "creator_open_id"),
    "account_type":     ("author_type", "account_type"),
    "start_time":       ("start_time",),
    "end_time":         ("end_time",),
}


# ---------------------------------------------------------
# Schema
# ---------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS tkliveinfo (
    store_id         TINYINT UNSIGNED NOT NULL COMMENT '1=TU, 2=VB (tkapi.client.SHOPS)',
    live_id          VARCHAR(64)      NOT NULL COMMENT 'joins to order line items live_room_id',
    live_title       VARCHAR(255)     NULL,
    creator_username VARCHAR(64)      NULL,
    creator_nickname VARCHAR(128)     NULL,
    creator_open_id  VARCHAR(128)     NULL,
    account_type     VARCHAR(32)      NULL,
    start_time       DATETIME         NULL COMMENT 'UTC',
    end_time         DATETIME         NULL COMMENT 'UTC',
    raw_json         JSON             NOT NULL COMMENT 'full API record',
    first_synced_at  DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced_at   DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (store_id, live_id),
    KEY idx_tkliveinfo_start (store_id, start_time),
    KEY idx_tkliveinfo_live (live_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DDL_LOG = """
CREATE TABLE IF NOT EXISTS tkliveinfo_sync_log (
    id          INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    source      VARCHAR(16)   NOT NULL COMMENT 'cli | timer | web',
    started_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'UTC',
    finished_at DATETIME      NULL COMMENT 'UTC',
    ok          TINYINT(1)    NULL,
    message     VARCHAR(1000) NULL,
    PRIMARY KEY (id),
    KEY idx_tkliveinfo_sync_log_finished (finished_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

UPSERT = """
INSERT INTO tkliveinfo
    (store_id, live_id, live_title, creator_username, creator_nickname,
     creator_open_id, account_type, start_time, end_time, raw_json)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    live_title       = VALUES(live_title),
    creator_username = VALUES(creator_username),
    creator_nickname = VALUES(creator_nickname),
    creator_open_id  = VALUES(creator_open_id),
    account_type     = VALUES(account_type),
    start_time       = VALUES(start_time),
    end_time         = VALUES(end_time),
    raw_json         = VALUES(raw_json),
    last_synced_at   = CURRENT_TIMESTAMP
"""

LOCK_NAME = "tkliveinfo_sync"


def init_schema(conn):
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute(DDL_LOG)
    cur.close()


# ---------------------------------------------------------
# Parsing helpers (defensive: exact response shape not yet confirmed)
# ---------------------------------------------------------
def find(obj, key):
    """First non-empty value for `key` anywhere in nested dicts."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for value in obj.values():
            found = find(value, key)
            if found is not None:
                return found
    return None


def field(rec, column):
    for key in FIELD_KEYS[column]:
        value = find(rec, key)
        if value is not None:
            return value
    return None


def to_utc(value):
    """Epoch seconds/ms or ISO string -> naive UTC datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        ts = float(value)
        if ts <= 0:  # e.g. end_time "0" while the LIVE is still running
            return None
        if ts > 1e12:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt  # assumed UTC; check against --dump output
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return None


def text(value):
    return None if value is None else str(value)


def to_row(store_id, rec):
    live_id = field(rec, "live_id")
    if live_id is None:
        return None
    return (
        store_id,
        str(live_id),
        text(field(rec, "live_title")),
        text(field(rec, "creator_username")),
        text(field(rec, "creator_nickname")),
        text(field(rec, "creator_open_id")),
        text(field(rec, "account_type")),
        to_utc(field(rec, "start_time")),
        to_utc(field(rec, "end_time")),
        json.dumps(rec, ensure_ascii=False),
    )


# ---------------------------------------------------------
# Fetch
# ---------------------------------------------------------
def fetch_lives(shop, start: date, end_exclusive: date, dump=False):
    page_token = None
    for _ in range(MAX_PAGES):
        params = {
            "start_date_ge": start.isoformat(),
            "end_date_lt": end_exclusive.isoformat(),
            "page_size": PAGE_SIZE,
        }
        if page_token:
            params["page_token"] = page_token

        data = api.call_api(shop, "GET", LIVE_LIST_PATH, params)

        if dump:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return

        records = data.get(LIST_KEY)
        if records is None:
            # Fall back to the first list in the response if the key name differs.
            records = next((v for v in data.values() if isinstance(v, list)), [])
            if records:
                print(f"[{shop['short_name']}] note: list key is not '{LIST_KEY}'; "
                      f"keys in response: {list(data.keys())}", flush=True)
        for rec in records:
            yield rec

        page_token = data.get("next_page_token")
        if not page_token:
            return
    print(f"[{shop['short_name']}] stopped after {MAX_PAGES} pages", flush=True)


# ---------------------------------------------------------
# Sync
# ---------------------------------------------------------
def sync_store(conn, shop, days: int) -> str:
    today = datetime.now(SHOP_TZ).date()
    start = today - timedelta(days=days)
    end_exclusive = today + timedelta(days=1)

    cur = conn.cursor()
    written = skipped = 0
    for rec in fetch_lives(shop, start, end_exclusive):
        row = to_row(shop["store_id"], rec)
        if row is None:
            skipped += 1
            continue
        cur.execute(UPSERT, row)
        written += 1
    conn.commit()
    cur.close()
    msg = f"[{shop['short_name']}] {start}..{today}: upserted {written}, skipped {skipped} (no id)"
    print(msg, flush=True)
    return msg


class SyncBusy(RuntimeError):
    """Another sync (timer / other web worker) is already running."""


def run_sync(store=None, days: int = 7, source: str = "cli") -> dict:
    """Sync one store (TU/VB/1/2) or all configured stores.

    Shared by the CLI, the systemd timer and the web page. A MySQL named lock
    makes concurrent runs skip instead of double-calling the API.
    Returns {"ok": bool, "messages": [...]}; raises SyncBusy if locked.
    """
    shops = ([api.resolve_shop(store)] if store
             else [s for s in api.SHOPS.values() if api.shop_is_configured(s)])

    conn = db.get_db()
    try:
        init_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, 0)", (LOCK_NAME,))
        if cur.fetchone()[0] != 1:
            cur.close()
            raise SyncBusy("sync already running")

        cur.execute("INSERT INTO tkliveinfo_sync_log (source) VALUES (%s)", (source,))
        log_id = cur.lastrowid
        conn.commit()

        messages, ok = [], True
        try:
            for shop in shops:
                try:
                    messages.append(sync_store(conn, shop, days))
                except api.TikTokAPIError as exc:
                    conn.rollback()
                    ok = False
                    messages.append(f"ERROR {exc}")
                    print(f"ERROR {exc}", flush=True)
        finally:
            cur.execute(
                "UPDATE tkliveinfo_sync_log SET finished_at = CURRENT_TIMESTAMP, ok = %s, message = %s "
                "WHERE id = %s",
                (ok, "\n".join(messages)[:1000], log_id),
            )
            conn.commit()
            cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            cur.fetchone()
            cur.close()
        return {"ok": ok, "messages": messages}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync TikTok Shop LIVE session info")
    parser.add_argument("--store", help="TU | VB | 1 | 2 (default: all configured)")
    parser.add_argument("--days", type=int, default=7, help="look-back window in days (default 7)")
    parser.add_argument("--dump", action="store_true", help="print first raw API page and exit")
    parser.add_argument("--source", default="cli", help="label written to tkliveinfo_sync_log")
    args = parser.parse_args()

    if args.dump:
        shops = ([api.resolve_shop(args.store)] if args.store
                 else [s for s in api.SHOPS.values() if api.shop_is_configured(s)])
        today = datetime.now(SHOP_TZ).date()
        for shop in shops:
            list(fetch_lives(shop, today - timedelta(days=args.days),
                             today + timedelta(days=1), dump=True))
        return

    try:
        result = run_sync(args.store, args.days, source=args.source)
    except SyncBusy:
        print("Another sync is running; skipped.", flush=True)
        return
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
