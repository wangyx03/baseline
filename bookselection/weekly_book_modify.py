import json
from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user

from db import get_db
from permissions.permissions import module_required, MODULE_BOOK_SELECTION

weekly_book_modify_bp = Blueprint(
    "weekly_book_modify",
    __name__,
    template_folder="templates",
)


@weekly_book_modify_bp.before_request
@module_required(MODULE_BOOK_SELECTION)
def require_book_selection_access():
    pass


def _stores(cursor):
    cursor.execute("""
        SELECT store_id, short_name
        FROM stores
        WHERE short_name IN ('VB', 'TU')
    """)
    rows = cursor.fetchall()
    out = {str(r["short_name"]).strip(): int(r["store_id"]) for r in rows}
    for code in ("VB", "TU"):
        if code not in out:
            raise ValueError(f"Store {code} was not found.")
    return out


def _weekly_rows(cursor, week_id, store_id):
    # Same Used / Remaining rule as Weekly Inventory:
    # locked + recording rows from lives that are not already locked.
    cursor.execute("""
        SELECT
            wi.sku,
            COALESCE(bs.book_title, '') AS title,
            COALESCE(bs.book_author, '') AS author,
            COALESCE(bs.spice_type, '') AS spice_type,
            COALESCE(stock_data.actual_stock, 0) AS actual_stock,
            wi.planned_qty AS planned,
            (
                COALESCE(locked_data.locked_used, 0)
                + COALESCE(draft_data.draft_used, 0)
            ) AS used,
            wi.planned_qty - (
                COALESCE(locked_data.locked_used, 0)
                + COALESCE(draft_data.draft_used, 0)
            ) AS remaining,
            CASE WHEN EXISTS (
                SELECT 1
                FROM book_selection_resident r
                WHERE r.store_id = wi.store_id
                  AND r.sku = wi.sku
            ) THEN 1 ELSE 0 END AS is_resident,
            CASE WHEN EXISTS (
                SELECT 1
                FROM weekly_inventory wi2
                WHERE wi2.week_id = wi.week_id
                  AND wi2.sku = wi.sku
                  AND wi2.store_id <> wi.store_id
            ) THEN 1 ELSE 0 END AS shared_across_store
        FROM weekly_inventory wi
        LEFT JOIN book_sku bs ON bs.isbn = wi.sku
        LEFT JOIN (
            SELECT sku, SUM(available_stock) AS actual_stock
            FROM inventory_snapshot
            WHERE stock_type = 'Good'
            GROUP BY sku
        ) stock_data ON stock_data.sku = wi.sku
        LEFT JOIN (
            SELECT sku, SUM(quantity) AS locked_used
            FROM inventory_locked
            WHERE week_id = %s AND store_id = %s
            GROUP BY sku
        ) locked_data ON locked_data.sku = wi.sku
        LEFT JOIN (
            SELECT sr.sku, SUM(sr.quantity) AS draft_used
            FROM sku_recording sr
            LEFT JOIN (
                SELECT DISTINCT week_id, store_id, live_id
                FROM inventory_locked
                WHERE week_id = %s AND store_id = %s
            ) locked_live
              ON locked_live.week_id = sr.week_id
             AND locked_live.store_id = sr.store_id
             AND locked_live.live_id = sr.live_id
            WHERE sr.week_id = %s
              AND sr.store_id = %s
              AND locked_live.live_id IS NULL
            GROUP BY sr.sku
        ) draft_data ON draft_data.sku = wi.sku
        WHERE wi.week_id = %s AND wi.store_id = %s
        ORDER BY wi.sku
    """, (
        week_id, store_id,
        week_id, store_id,
        week_id, store_id,
        week_id, store_id,
    ))
    return cursor.fetchall()


@weekly_book_modify_bp.route("/weekly-book-modify")
@login_required
def page():
    return render_template("weekly_book_modify.html")


@weekly_book_modify_bp.route("/api/weekly-book-modify/weeks", methods=["GET"])
@login_required
def weeks():
    db = get_db(); cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT week_id
            FROM weekly_inventory
            WHERE week_id IS NOT NULL AND week_id <> ''
            ORDER BY week_id DESC
        """)
        return jsonify(success=True, weeks=[r["week_id"] for r in cursor.fetchall()])
    except Exception as e:
        return jsonify(success=False, message=str(e)), 500
    finally:
        cursor.close(); db.close()


@weekly_book_modify_bp.route("/api/weekly-book-modify/load", methods=["GET"])
@login_required
def load():
    week_id = str(request.args.get("week_id", "")).strip()
    if not week_id:
        return jsonify(success=False, message="Week ID is required."), 400

    db = get_db(); cursor = db.cursor(dictionary=True)
    try:
        store_ids = _stores(cursor)
        vb = _weekly_rows(cursor, week_id, store_ids["VB"])
        tu = _weekly_rows(cursor, week_id, store_ids["TU"])
        return jsonify(
            success=True,
            week_id=week_id,
            items_by_store={"VB": vb, "TU": tu},
        )
    except Exception as e:
        return jsonify(success=False, message=str(e)), 500
    finally:
        cursor.close(); db.close()


@weekly_book_modify_bp.route("/api/weekly-book-modify/unused", methods=["GET"])
@login_required
def unused():
    # This endpoint keeps the old URL for frontend compatibility,
    # but it now returns the full Whole Inventory pool:
    #   1) Snapshot Good-stock SKUs above Min Stock Filter
    #   2) UNION every SKU already in this week's VB/TU weekly_inventory
    week_id = str(request.args.get("week_id", "")).strip()
    q = str(request.args.get("q", "")).strip()
    min_stock = request.args.get("min_stock", default=0, type=int)

    if not week_id:
        return jsonify(success=False, message="Week ID is required."), 400

    if min_stock is None or min_stock < 0:
        min_stock = 0

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:
        like = f"%{q}%"

        cursor.execute("""
            WITH stock_data AS (
                SELECT
                    sku,
                    SUM(available_stock) AS actual_stock
                FROM inventory_snapshot
                WHERE stock_type = 'Good'
                GROUP BY sku
            ),
            weekly_skus AS (
                SELECT DISTINCT
                    wi.sku
                FROM weekly_inventory wi
                INNER JOIN stores st
                    ON st.store_id = wi.store_id
                WHERE wi.week_id = %s
                  AND st.short_name IN ('VB', 'TU')
            ),
            whole_skus AS (
                SELECT sku
                FROM stock_data
                WHERE actual_stock > %s

                UNION

                SELECT sku
                FROM weekly_skus
            )
            SELECT
                ws.sku,
                COALESCE(bs.book_title, '') AS title,
                COALESCE(bs.book_author, '') AS author,
                COALESCE(bs.spice_type, '') AS spice_type,
                COALESCE(sd.actual_stock, 0) AS actual_stock
            FROM whole_skus ws
            LEFT JOIN stock_data sd
                ON sd.sku = ws.sku
            LEFT JOIN book_sku bs
                ON bs.isbn = ws.sku
            WHERE (
                %s = ''
                OR ws.sku LIKE %s
                OR COALESCE(bs.book_title, '') LIKE %s
                OR COALESCE(bs.book_author, '') LIKE %s
            )
            ORDER BY
                COALESCE(sd.actual_stock, 0) DESC,
                ws.sku
        """, (
            week_id,
            min_stock,
            q,
            like,
            like,
            like,
        ))

        return jsonify(
            success=True,
            items=cursor.fetchall(),
            min_stock=min_stock,
        )

    except Exception as e:
        return jsonify(success=False, message=str(e)), 500

    finally:
        cursor.close()
        db.close()


@weekly_book_modify_bp.route("/api/weekly-book-modify/confirm", methods=["POST"])
@login_required
def confirm():
    data = request.get_json(silent=True) or {}
    week_id = str(data.get("week_id") or "").strip()
    changes = data.get("changes") or []
    min_stock_filter = data.get("min_stock_filter", 0)
    try:
        min_stock_filter = max(0, int(min_stock_filter or 0))
    except (TypeError, ValueError):
        return jsonify(success=False, message="Min Stock Filter must be a non-negative integer."), 400

    if not week_id:
        return jsonify(success=False, message="Week ID is required."), 400
    if not isinstance(changes, list):
        return jsonify(success=False, message="Changes must be a list."), 400

    db = get_db(); cursor = db.cursor(dictionary=True)
    applied = []
    increased_skus = set()
    try:
        store_ids = _stores(cursor)
        code_by_id = {v: k for k, v in store_ids.items()}

        for change in changes:
            action = str(change.get("action") or "").upper().strip()
            store = str(change.get("store") or "").upper().strip()
            sku = str(change.get("sku") or "").strip()
            if store not in store_ids or not sku:
                raise ValueError("Every change requires a valid store and SKU.")
            store_id = store_ids[store]

            if action in ("MODIFY", "REMOVE"):
                cursor.execute("""
                    SELECT planned_qty
                    FROM weekly_inventory
                    WHERE week_id=%s AND store_id=%s AND sku=%s
                    FOR UPDATE
                """, (week_id, store_id, sku))
                existing = cursor.fetchone()
                if not existing:
                    raise ValueError(f"{store} / {sku} is no longer in Weekly Inventory. Refresh first.")

                # Re-check current Used at Confirm time only for safety.
                current_rows = _weekly_rows(cursor, week_id, store_id)
                current = next((r for r in current_rows if str(r["sku"]) == sku), None)
                used = int((current or {}).get("used") or 0)

                if action == "REMOVE":
                    if used > 0:
                        raise ValueError(f"{store} / {sku} has Used {used}; it cannot be removed. Set Planned to Used instead.")
                    cursor.execute("""
                        DELETE FROM weekly_inventory
                        WHERE week_id=%s AND store_id=%s AND sku=%s
                    """, (week_id, store_id, sku))
                    applied.append({"action":"REMOVE","store":store,"sku":sku,"old_planned":int(existing["planned_qty"] or 0)})
                else:
                    planned = int(change.get("planned_qty"))
                    if planned < used:
                        raise ValueError(f"{store} / {sku}: Planned {planned} cannot be below Used {used}.")
                    old_planned = int(existing["planned_qty"] or 0)
                    cursor.execute("""
                        UPDATE weekly_inventory SET planned_qty=%s
                        WHERE week_id=%s AND store_id=%s AND sku=%s
                    """, (planned, week_id, store_id, sku))
                    if planned > old_planned:
                        increased_skus.add(sku)
                    applied.append({"action":"MODIFY","store":store,"sku":sku,"old_planned":old_planned,"new_planned":planned})

            elif action == "ADD":
                planned = int(change.get("planned_qty"))
                if planned < 0:
                    raise ValueError("Planned Qty cannot be negative.")
                cursor.execute("""
                    SELECT 1 FROM weekly_inventory
                    WHERE week_id=%s AND store_id=%s AND sku=%s
                    FOR UPDATE
                """, (week_id, store_id, sku))
                if cursor.fetchone():
                    raise ValueError(f"{store} / {sku} already exists. Refresh first.")
                cursor.execute("""
                    INSERT INTO weekly_inventory (week_id, store_id, sku, planned_qty)
                    VALUES (%s,%s,%s,%s)
                """, (week_id, store_id, sku, planned))
                if planned > 0:
                    increased_skus.add(sku)
                applied.append({"action":"ADD","store":store,"sku":sku,"new_planned":planned})
            else:
                raise ValueError(f"Unsupported action: {action}")

        # Final reserve validation after all changes are staged in this transaction.
        # Only SKUs whose Planned increased need this reserve check.
        for sku in increased_skus:
            cursor.execute("""
                SELECT COALESCE(SUM(available_stock), 0) AS actual_stock
                FROM inventory_snapshot
                WHERE stock_type = 'Good'
                  AND sku = %s
            """, (sku,))
            actual_stock = int((cursor.fetchone() or {}).get("actual_stock") or 0)

            total_remaining = 0
            for store_code in ("VB", "TU"):
                rows = _weekly_rows(cursor, week_id, store_ids[store_code])
                row = next((r for r in rows if str(r["sku"]) == str(sku)), None)
                if row:
                    total_remaining += int(row.get("remaining") or 0)

            buffer_qty = actual_stock - total_remaining
            if buffer_qty < min_stock_filter:
                raise ValueError(
                    f"{sku}: Actual Stock {actual_stock} - Remaining Total "
                    f"{total_remaining} = {buffer_qty}, below Min Stock Filter "
                    f"{min_stock_filter}."
                )

        # Log once if the existing log table is available. This is intentionally
        # non-fatal for installations whose weekly_inventory_log schema differs.
        if applied:
            try:
                operator_id = getattr(current_user, "user_id", None)
                if operator_id is None:
                    operator_id = getattr(current_user, "id", None)
                cursor.execute("""
                    INSERT INTO weekly_inventory_log
                        (week_id, action, operator_user_id, change_count, change_data)
                    VALUES (%s, 'MODIFY', %s, %s, %s)
                """, (week_id, operator_id, len(applied), json.dumps({"changes": applied}, ensure_ascii=False)))
            except Exception:
                # Do not let an older/different optional log schema break inventory edits.
                pass

        db.commit()
        return jsonify(success=True, applied_count=len(applied), message=f"Confirmed {len(applied)} change(s).")
    except (ValueError, TypeError) as e:
        db.rollback()
        return jsonify(success=False, message=str(e)), 400
    except Exception as e:
        db.rollback()
        return jsonify(success=False, message=str(e)), 500
    finally:
        cursor.close(); db.close()
