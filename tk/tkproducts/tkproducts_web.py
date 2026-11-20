"""Flask blueprint: TikTok Shop product catalog (TU / VB).

- GET  /tkproducts/              page: one row per SKU, filters + pagination
- POST /tkproducts/sync          start a full catalog sync from TikTok (background)
- GET  /tkproducts/sync/status   is a sync running + last result per store

A full sync pages through every product, so it can take longer than the
gunicorn request timeout. It runs in a background thread and holds the MySQL
named lock below, so only one sync runs at a time across all workers.
Normal updates still arrive through the product webhook worker.

Access: users with the TikTok Products module (MODULE_TIKTOK_PRODUCTS = 10).
"""

import threading
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from api.tkapi import client as api
from permissions.permissions import MODULE_TIKTOK_PRODUCTS, module_required
from tk.tkproducts import tkproducts_db as pdb
from tk.tkproducts.tkproducts import sync_shop

bp = Blueprint("tkproducts", __name__, url_prefix="/tkproducts", template_folder="templates")

DISPLAY_TZ = ZoneInfo("America/New_York")
SYNC_LOCK = "tkproducts_full_sync"
PER_PAGE = 100
STORE_NAMES = {s["store_id"]: s["short_name"] for s in api.SHOPS.values()}
STORE_IDS = {s["short_name"]: s["store_id"] for s in api.SHOPS.values()}
SORTS = {
    "updated": "p.update_time DESC, p.product_id, s.sku_id",
    "created": "p.create_time DESC, p.product_id, s.sku_id",
    "name": "p.product_name, s.sku_id",
    "stock_desc": "s.stock DESC, p.product_name",
    "stock_asc": "s.stock ASC, p.product_name",
    "price_desc": "s.retail_price DESC, p.product_name",
    "price_asc": "s.retail_price ASC, p.product_name",
}

_schema_ready = False


@bp.before_request
@module_required(MODULE_TIKTOK_PRODUCTS)
def require_tiktok_products_access():
    pass


def _conn():
    global _schema_ready
    conn = pdb.get_connection()
    if not _schema_ready:
        pdb.init_schema(conn)
        _schema_ready = True
    return conn


def _fmt(dt):
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def _int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _filters(args):
    """WHERE clause for the SKU list from the query string."""
    where, params = ["1=1"], []
    store = args.get("store", "")
    if store in STORE_IDS:
        where.append("p.store_id = %s")
        params.append(STORE_IDS[store])
    status = args.get("status", "")
    if status == "_not_deleted":
        where.append("COALESCE(UPPER(p.status),'') <> 'DELETED'")
    elif status:
        where.append("p.status = %s")
        params.append(status)
    stock = args.get("stock", "")
    if stock == "in":
        where.append("s.stock > 0")
    elif stock == "out":
        where.append("COALESCE(s.stock, 0) = 0")
    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(p.product_name LIKE %s OR s.seller_sku LIKE %s "
                     "OR p.product_id = %s OR s.sku_id = %s)")
        params.extend([like, like, q, q])
    return " AND ".join(where), params


def _sync_state(cur):
    cur.execute("SELECT store_id, last_full_sync_at, last_product_count, last_sku_count "
                "FROM tkproducts_sync_state")
    state = {r["store_id"]: r for r in cur.fetchall()}
    out = []
    for store_id, name in STORE_NAMES.items():
        r = state.get(store_id) or {}
        out.append({
            "store": name,
            "last_full_sync": _fmt(r.get("last_full_sync_at")) or "never",
            "products": r.get("last_product_count") or 0,
            "skus": r.get("last_sku_count") or 0,
        })
    return out


def _sync_running(cur):
    cur.execute("SELECT IS_USED_LOCK(%s) AS holder", (SYNC_LOCK,))
    row = cur.fetchone()
    return bool(row and row["holder"])


def _last_sync_log(cur):
    cur.execute("SELECT source, stores, started_at, finished_at, ok, message "
                "FROM tkproducts_sync_log ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return {
        "source": row["source"],
        "stores": row["stores"],
        "started": _fmt(row["started_at"]),
        "finished": _fmt(row["finished_at"]),
        "ok": None if row["ok"] is None else bool(row["ok"]),
        "message": row["message"] or "",
    }


@bp.get("/")
def page():
    args = request.args
    where, params = _filters(args)
    sort = args.get("sort") if args.get("sort") in SORTS else "updated"
    page_no = _int(args.get("page"), 1)

    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT p.store_id, p.product_id) AS products "
            "FROM tkproducts_skus s JOIN tkproducts p "
            "ON p.store_id = s.store_id AND p.product_id = s.product_id "
            f"WHERE {where}", params)
        totals = cur.fetchone()
        total = totals["n"] or 0
        pages = max(1, -(-total // PER_PAGE))
        page_no = min(page_no, pages)

        cur.execute(
            "SELECT p.store_id, p.product_id, p.product_name, p.status, p.image_url, "
            "p.create_time, p.update_time, s.sku_id, s.seller_sku, s.sku_name, "
            "s.retail_price, s.currency, s.stock "
            "FROM tkproducts_skus s JOIN tkproducts p "
            "ON p.store_id = s.store_id AND p.product_id = s.product_id "
            f"WHERE {where} ORDER BY {SORTS[sort]} LIMIT %s OFFSET %s",
            params + [PER_PAGE, (page_no - 1) * PER_PAGE])
        rows = cur.fetchall()

        cur.execute("SELECT p.store_id, COALESCE(p.status,'(none)') AS status, COUNT(*) AS n "
                    "FROM tkproducts p GROUP BY p.store_id, p.status ORDER BY n DESC")
        status_counts = cur.fetchall()
        statuses = sorted({r["status"] for r in status_counts if r["status"] != "(none)"})

        cur.execute("SELECT status, COUNT(*) AS n FROM tkproducts_webhook_inbox "
                    "WHERE status IN ('PENDING','PROCESSING','FAILED') GROUP BY status")
        inbox = {r["status"]: r["n"] for r in cur.fetchall()}

        sync_state = _sync_state(cur)
        running = _sync_running(cur)
        last_log = _last_sync_log(cur)
        cur.close()
    finally:
        conn.close()

    for r in rows:
        r["store"] = STORE_NAMES.get(r["store_id"], r["store_id"])
        r["updated_local"] = _fmt(r["update_time"])
        r["created_local"] = _fmt(r["create_time"])

    summary = {}
    for r in status_counts:
        summary.setdefault(STORE_NAMES.get(r["store_id"], r["store_id"]), []).append(r)

    query = {k: v for k, v in args.items() if k != "page" and v}
    return render_template(
        "tkproducts.html",
        rows=rows,
        total=total,
        product_total=totals["products"] or 0,
        page=page_no,
        pages=pages,
        per_page=PER_PAGE,
        query=query,
        params=args,
        sort=sort,
        statuses=statuses,
        stores=list(STORE_IDS),
        summary=summary,
        inbox=inbox,
        sync_state=sync_state,
        sync_running=running,
        last_log=last_log,
    )


def _run_sync(shops, source):
    """Background thread: full sync for the given shops under the named lock."""
    conn = pdb.get_connection()
    log_id = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, 0)", (SYNC_LOCK,))
        if cur.fetchone()[0] != 1:
            cur.close()
            return
        cur.execute("INSERT INTO tkproducts_sync_log (source, stores) VALUES (%s, %s)",
                    (source, ",".join(s["short_name"] for s in shops)))
        log_id = cur.lastrowid
        conn.commit()
        cur.close()

        messages, ok = [], True
        for shop in shops:
            if not api.shop_is_configured(shop):
                messages.append(f"{shop['short_name']}: skipped, credentials not configured")
                continue
            try:
                products, skus = sync_shop(conn, shop)
                messages.append(f"{shop['short_name']}: {products} products, {skus} SKUs")
            except Exception as exc:   # TikTokAPIError messages never contain tokens
                conn.rollback()
                ok = False
                messages.append(f"{shop['short_name']}: failed ({type(exc).__name__}: {exc})")

        cur = conn.cursor()
        cur.execute("UPDATE tkproducts_sync_log SET finished_at=UTC_TIMESTAMP(), ok=%s, "
                    "message=%s WHERE id=%s", (1 if ok else 0, "\n".join(messages)[:1000], log_id))
        conn.commit()
        cur.close()
    except Exception as exc:
        if log_id:
            try:
                conn.rollback()
                cur = conn.cursor()
                cur.execute("UPDATE tkproducts_sync_log SET finished_at=UTC_TIMESTAMP(), ok=0, "
                            "message=%s WHERE id=%s", (f"{type(exc).__name__}: {exc}"[:1000], log_id))
                conn.commit()
                cur.close()
            except Exception:
                pass
    finally:
        try:
            cur = conn.cursor()
            cur.execute("SELECT RELEASE_LOCK(%s)", (SYNC_LOCK,))
            cur.fetchall()
            cur.close()
        except Exception:
            pass
        conn.close()


@bp.post("/sync")
def sync():
    store = (request.get_json(silent=True) or {}).get("store", "ALL")
    if store == "ALL":
        shops = list(api.SHOPS.values())
    elif store in STORE_IDS:
        shops = [api.SHOPS[STORE_IDS[store]]]
    else:
        return jsonify(status="error", message=f"Unknown store {store!r}"), 400

    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        running = _sync_running(cur)
        cur.close()
    finally:
        conn.close()
    if running:
        return jsonify(status="busy", message="A product sync is already running"), 409

    source = f"web:{getattr(current_user, 'username', None) or current_user.get_id()}"[:32]
    threading.Thread(target=_run_sync, args=(shops, source), daemon=True,
                     name="tkproducts-sync").start()
    return jsonify(status="started", message="Product sync started"), 202


@bp.get("/sync/status")
def sync_status():
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        data = {
            "running": _sync_running(cur),
            "last": _last_sync_log(cur),
            "stores": _sync_state(cur),
        }
        cur.close()
    finally:
        conn.close()
    return jsonify(data)
