"""Flask blueprint: TikTok LIVE session info table.

- GET  /tkliveinfo/          page (filters: from/to start date in ET)
- POST /tkliveinfo/refresh   pull from TikTok API now (same code as the timer)

The page calls /refresh once on open; the Refresh button calls it again.

Register in the baseline web app:
    from tklive.tkliveinfo_web import bp as tkliveinfo_bp
    app.register_blueprint(tkliveinfo_bp)

Access: users with the TikTok LIVE module (MODULE_TIKTOK_LIVE = 9).
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

import db
from permissions.permissions import MODULE_TIKTOK_LIVE, module_required
from tkapi import client as api
from tklive.tkliveinfo import SyncBusy, init_schema, run_sync

bp = Blueprint("tkliveinfo", __name__, url_prefix="/tkliveinfo", template_folder="templates")

DISPLAY_TZ = ZoneInfo("America/New_York")
REFRESH_DAYS = 7            # API look-back per refresh
COOLDOWN_SEC = 30           # page-open refresh is skipped if a sync finished this recently
STORE_NAMES = {s["store_id"]: s["short_name"] for s in api.SHOPS.values()}


@bp.before_request
@module_required(MODULE_TIKTOK_LIVE)
def require_tiktok_live_access():
    pass


def _parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _et_day_to_utc(day):
    """Midnight ET of `day` -> naive UTC datetime (DB stores UTC)."""
    local = datetime.combine(day, time.min, tzinfo=DISPLAY_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _fmt(dt):
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


@bp.get("/")
@login_required
def page():
    date_from = _parse_day(request.args.get("from"))
    date_to = _parse_day(request.args.get("to"))

    sql = (
        "SELECT store_id, live_id, live_title, creator_username, start_time, end_time "
        "FROM tkliveinfo WHERE 1=1"
    )
    params = []
    if date_from:
        sql += " AND start_time >= %s"
        params.append(_et_day_to_utc(date_from))
    if date_to:
        sql += " AND start_time < %s"
        params.append(_et_day_to_utc(date_to + timedelta(days=1)))
    sql += " ORDER BY start_time DESC"

    conn = db.get_db()
    try:
        init_schema(conn)
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    for r in rows:
        r["store"] = STORE_NAMES.get(r["store_id"], r["store_id"])
        r["start_local"] = _fmt(r["start_time"])
        r["end_local"] = _fmt(r["end_time"])

    return render_template(
        "tkliveinfo.html",
        rows=rows,
        params=request.args,
    )


@bp.post("/refresh")
@login_required
def refresh():
    force = request.args.get("force") == "1"

    if not force:
        conn = db.get_db()
        try:
            init_schema(conn)
            cur = conn.cursor()
            cur.execute(
                "SELECT ok, TIMESTAMPDIFF(SECOND, finished_at, UTC_TIMESTAMP()) "
                "FROM tkliveinfo_sync_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            )
            last = cur.fetchone()
            cur.close()
        finally:
            conn.close()
        if last and last[0] and last[1] is not None and last[1] < COOLDOWN_SEC:
            return jsonify(status="recent", message=f"Synced {last[1]}s ago")

    try:
        result = run_sync(days=REFRESH_DAYS, source="web")
    except SyncBusy:
        return jsonify(status="busy", message="Another sync is running, try again shortly"), 409
    except Exception as exc:  # TikTokAPIError messages never contain tokens
        return jsonify(status="error", message=type(exc).__name__), 500

    return jsonify(status="ok" if result["ok"] else "error",
                   message="\n".join(result["messages"])), (200 if result["ok"] else 502)
