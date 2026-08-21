from datetime import datetime, time, timedelta
from typing import Dict, Iterable, List, Optional

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from db import get_db
from utils import EASTERN_ZONE, UTC_ZONE, format_et

from permissions.permissions import (
    module_required,
    MODULE_BOOK_SELECTION
)



next_week_candidates_bp = Blueprint(
    "next_week_candidates",
    __name__,
    template_folder="templates"
)

@next_week_candidates_bp.before_request
@module_required(
    MODULE_BOOK_SELECTION
)
def require_book_selection_access():
    pass



# =========================================================
# Page: Next Week Candidates
# =========================================================

@next_week_candidates_bp.route(
    "/next-week-candidates"
)
@login_required
def next_week_candidates_page():

    return render_template(
        "next_week_candidates.html",
        extra_nav_links=[
            {
                "label": "Resident Books",
                "url": "/resident-books"
            },
            {
                "label": "Book Selection",
                "url": "/book-selection"
            }
        ]
    )



def _normalize_selected_lives(selected_lives: Iterable[dict]) -> List[dict]:
    """
    Normalize and deduplicate selected LIVE sessions.

    Each item must contain:
        week_id
        store_id
        live_id

    Deduplication key:
        (week_id, store_id, live_id)
    """
    normalized = []
    seen = set()

    for item in selected_lives or []:
        week_id = str(item.get("week_id", "")).strip()
        live_id = str(item.get("live_id", "")).strip()
        store_id = item.get("store_id")

        if not week_id or not live_id or store_id is None:
            continue

        try:
            store_id = int(store_id)
        except (TypeError, ValueError):
            continue

        key = (week_id, store_id, live_id)

        if key in seen:
            continue

        seen.add(key)

        normalized.append({
            "week_id": week_id,
            "store_id": store_id,
            "live_id": live_id,
        })

    return normalized


def get_selectable_live_sessions(
    cursor,
    week_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 200
) -> List[dict]:
    """
    Return LIVE sessions for manual selection.

    Optional filters:
        week_id
        start_date / end_date in America/New_York calendar dates

    created_at is stored/read as UTC in MySQL, but the date picker
    represents Eastern Time. Date boundaries are converted to UTC
    before querying.
    """

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200

    limit = max(1, min(limit, 1000))

    filters = []
    params = []

    if week_id:
        filters.append(
            "ls.week_id = %s"
        )
        params.append(
            str(week_id).strip()
        )

    if start_date:

        start_day = datetime.strptime(
            str(start_date).strip(),
            "%Y-%m-%d"
        ).date()

        start_et = datetime.combine(
            start_day,
            time.min,
            tzinfo=EASTERN_ZONE
        )

        start_utc = (
            start_et
            .astimezone(UTC_ZONE)
            .replace(tzinfo=None)
        )

        filters.append(
            "ls.created_at >= %s"
        )
        params.append(start_utc)

    if end_date:

        end_day = datetime.strptime(
            str(end_date).strip(),
            "%Y-%m-%d"
        ).date()

        end_exclusive_et = datetime.combine(
            end_day + timedelta(days=1),
            time.min,
            tzinfo=EASTERN_ZONE
        )

        end_exclusive_utc = (
            end_exclusive_et
            .astimezone(UTC_ZONE)
            .replace(tzinfo=None)
        )

        filters.append(
            "ls.created_at < %s"
        )
        params.append(
            end_exclusive_utc
        )

    sql = """
        SELECT
            ls.week_id,
            ls.store_id,
            COALESCE(
                s.short_name,
                ''
            ) AS store_code,
            COALESCE(
                s.store_name,
                ''
            ) AS store_name,
            ls.live_id,
            ls.created_at,

            CASE
                WHEN EXISTS (
                    SELECT 1

                    FROM inventory_locked il

                    WHERE il.week_id =
                              ls.week_id
                      AND il.store_id =
                              ls.store_id
                      AND il.live_id =
                              ls.live_id
                )
                THEN 'locked'
                ELSE 'recording'
            END AS source

        FROM live_sessions ls

        LEFT JOIN stores s
            ON s.store_id =
                   ls.store_id
    """

    if filters:

        sql += """
            WHERE
        """ + "\n AND ".join(filters)

    sql += """
        ORDER BY
            ls.created_at DESC,
            ls.session_id DESC

        LIMIT %s
    """

    params.append(limit)

    cursor.execute(
        sql,
        params
    )

    return cursor.fetchall()

def get_candidate_books(
    cursor,
    current_week_id: str,
    selected_lives: Iterable[dict],
    include_weekly_remaining: bool = True
) -> List[dict]:
    """
    Build the next-week candidate book list.

    Formula:
        candidate_stock
        =
        available_stock
        - selected_live_sales
        - weekly_remaining (only when enabled)

    Inventory source:
        inventory_snapshot
        stock_type = 'Good'
        available_stock != 0
        grouped by SKU
        only keep SUM(available_stock) > 0

    Selected LIVE sales rule:
        For each selected (week_id, store_id, live_id):
        - if inventory_locked has that LIVE, use inventory_locked ONLY
        - otherwise use sku_recording
        Then aggregate by SKU.

    Weekly remaining:
        Uses the same rule as recording/inventory_service.py:
            used = locked_used + draft_used
            remaining = planned_qty - used

        draft_used only includes sku_recording LIVE IDs that do not already
        exist in inventory_locked for the same week/store/live.

        Because the physical inventory pool is shared, remaining is summed
        across all stores for current_week_id.

    Output only contains rows with candidate_stock > 0.
    """

    current_week_id = str(current_week_id or "").strip()

    if not current_week_id:
        raise ValueError("current_week_id is required")

    selected = _normalize_selected_lives(selected_lives)

    # ---------------------------------------------------------
    # Build selected LIVE CTE dynamically.
    # When no LIVE is selected, create an empty compatible CTE.
    # ---------------------------------------------------------
    selected_params = []

    if selected:
        selected_sql_parts = []

        for i, item in enumerate(selected):
            if i == 0:
                selected_sql_parts.append(
                    "SELECT %s AS week_id, %s AS store_id, %s AS live_id"
                )
            else:
                selected_sql_parts.append(
                    "UNION ALL SELECT %s, %s, %s"
                )

            selected_params.extend([
                item["week_id"],
                item["store_id"],
                item["live_id"],
            ])

        selected_cte = "\n".join(selected_sql_parts)

    else:
        selected_cte = """
            SELECT
                CAST(NULL AS CHAR(10)) AS week_id,
                CAST(NULL AS UNSIGNED) AS store_id,
                CAST(NULL AS CHAR(50)) AS live_id
            WHERE 1 = 0
        """

    sql = f"""
        WITH

        selected_lives AS (
            {selected_cte}
        ),

        /* =====================================================
           Current available inventory
           ===================================================== */
        available_inventory AS (
            SELECT
                sku,
                SUM(available_stock) AS available_stock

            FROM inventory_snapshot

            WHERE stock_type = 'Good'
              AND available_stock != 0

            GROUP BY sku

            HAVING SUM(available_stock) > 0
        ),

        /* =====================================================
           Selected LIVE sales - locked source
           ===================================================== */
        selected_locked AS (
            SELECT
                il.week_id,
                il.store_id,
                il.live_id,
                il.sku,
                SUM(il.quantity) AS quantity

            FROM inventory_locked il

            INNER JOIN selected_lives sl
                ON sl.week_id = il.week_id
               AND sl.store_id = il.store_id
               AND sl.live_id = il.live_id

            GROUP BY
                il.week_id,
                il.store_id,
                il.live_id,
                il.sku
        ),

        /* =====================================================
           Selected LIVE sales - recording fallback

           If ANY locked row exists for the selected LIVE,
           the whole LIVE uses inventory_locked and recording
           is ignored.
           ===================================================== */
        selected_recording AS (
            SELECT
                sr.week_id,
                sr.store_id,
                sr.live_id,
                sr.sku,
                SUM(sr.quantity) AS quantity

            FROM sku_recording sr

            INNER JOIN selected_lives sl
                ON sl.week_id = sr.week_id
               AND sl.store_id = sr.store_id
               AND sl.live_id = sr.live_id

            WHERE NOT EXISTS (
                SELECT 1

                FROM inventory_locked il

                WHERE il.week_id = sr.week_id
                  AND il.store_id = sr.store_id
                  AND il.live_id = sr.live_id
            )

            GROUP BY
                sr.week_id,
                sr.store_id,
                sr.live_id,
                sr.sku
        ),

        selected_live_sales AS (
            SELECT
                sku,
                SUM(quantity) AS selected_live_sales

            FROM (
                SELECT
                    sku,
                    quantity
                FROM selected_locked

                UNION ALL

                SELECT
                    sku,
                    quantity
                FROM selected_recording
            ) x

            GROUP BY sku
        ),

        /* =====================================================
           Weekly locked usage
           ===================================================== */
        weekly_locked AS (
            SELECT
                il.store_id,
                il.sku,
                SUM(il.quantity) AS locked_used

            FROM inventory_locked il

            WHERE il.week_id = %s

            GROUP BY
                il.store_id,
                il.sku
        ),

        /* =====================================================
           Weekly recording usage that has NOT been locked
           ===================================================== */
        weekly_draft AS (
            SELECT
                sr.store_id,
                sr.sku,
                SUM(sr.quantity) AS draft_used

            FROM sku_recording sr

            WHERE sr.week_id = %s

              AND NOT EXISTS (
                  SELECT 1

                  FROM inventory_locked il

                  WHERE il.week_id = sr.week_id
                    AND il.store_id = sr.store_id
                    AND il.live_id = sr.live_id
              )

            GROUP BY
                sr.store_id,
                sr.sku
        ),

        /* =====================================================
           Remaining for each store/SKU
           ===================================================== */
        weekly_remaining_by_store AS (
            SELECT
                wi.store_id,
                wi.sku,

                wi.planned_qty
                -
                (
                    COALESCE(wl.locked_used, 0)
                    +
                    COALESCE(wd.draft_used, 0)
                ) AS remaining

            FROM weekly_inventory wi

            LEFT JOIN weekly_locked wl
                ON wl.store_id = wi.store_id
               AND wl.sku = wi.sku

            LEFT JOIN weekly_draft wd
                ON wd.store_id = wi.store_id
               AND wd.sku = wi.sku

            WHERE wi.week_id = %s
        ),

        /* =====================================================
           Shared inventory pool:
           sum current-week remaining across all stores
           ===================================================== */
        weekly_remaining AS (
            SELECT
                sku,
                SUM(remaining) AS weekly_remaining

            FROM weekly_remaining_by_store

            GROUP BY sku
        )

        SELECT
            ai.sku,

            COALESCE(bs.book_title, '') AS book_title,

            bs.spice_type,

            ai.available_stock,

            COALESCE(
                sls.selected_live_sales,
                0
            ) AS selected_live_sales,

            CASE
                WHEN %s
                THEN COALESCE(
                    wr.weekly_remaining,
                    0
                )
                ELSE 0
            END AS weekly_remaining,

            GREATEST(
                ai.available_stock
                -
                COALESCE(
                    sls.selected_live_sales,
                    0
                )
                -
                CASE
                    WHEN %s
                    THEN COALESCE(
                        wr.weekly_remaining,
                        0
                    )
                    ELSE 0
                END,
                0
            ) AS candidate_stock

        FROM available_inventory ai

        LEFT JOIN book_sku bs
            ON bs.isbn = ai.sku

        LEFT JOIN selected_live_sales sls
            ON sls.sku = ai.sku

        LEFT JOIN weekly_remaining wr
            ON wr.sku = ai.sku

        WHERE
            (
                ai.available_stock
                -
                COALESCE(
                    sls.selected_live_sales,
                    0
                )
                -
                CASE
                    WHEN %s
                    THEN COALESCE(
                        wr.weekly_remaining,
                        0
                    )
                    ELSE 0
                END
            ) > 0

        ORDER BY
            candidate_stock DESC,
            ai.sku ASC
    """

    params = (
        selected_params
        + [
            current_week_id,
            current_week_id,
            current_week_id,
            bool(include_weekly_remaining),
            bool(include_weekly_remaining),
            bool(include_weekly_remaining),
        ]
    )

    cursor.execute(sql, params)

    return cursor.fetchall()

# =========================================================
# API: selectable LIVE sessions
# =========================================================

@next_week_candidates_bp.route(
    "/api/next-week-candidates/lives",
    methods=["GET"]
)
@login_required
def api_next_week_candidate_lives():

    week_id = str(
        request.args.get(
            "week_id",
            ""
        )
    ).strip()

    limit = request.args.get(
        "limit",
        default=200,
        type=int
    )

    start_date = str(
        request.args.get(
            "start_date",
            ""
        )
    ).strip()

    end_date = str(
        request.args.get(
            "end_date",
            ""
        )
    ).strip()


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        rows = get_selectable_live_sessions(
            cursor=cursor,
            week_id=week_id or None,
            start_date=start_date or None,
            end_date=end_date or None,
            limit=limit
        )

        for row in rows:

            row["created_at"] = format_et(
                row.get("created_at")
            )

        return jsonify({
            "success": True,
            "week_id": week_id or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "items": rows
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# API: saved candidate batches
# =========================================================

@next_week_candidates_bp.route(
    "/api/next-week-candidates/batches",
    methods=["GET"]
)
@login_required
def api_candidate_batches():

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                b.batch_id,
                b.week_id,
                b.include_weekly_remaining,
                b.generated_at,
                COUNT(DISTINCT bl.batch_live_id) AS live_count,
                COUNT(DISTINCT c.candidate_id) AS candidate_count
            FROM book_selection_batches b

            LEFT JOIN book_selection_batch_lives bl
                ON bl.batch_id = b.batch_id

            LEFT JOIN book_selection_candidates c
                ON c.batch_id = b.batch_id

            GROUP BY
                b.batch_id,
                b.week_id,
                b.include_weekly_remaining,
                b.generated_at

            ORDER BY
                b.batch_id DESC

            LIMIT 100
            """
        )

        items = cursor.fetchall()

        for item in items:
            item["generated_at"] = format_et(
                item.get("generated_at")
            )

        return jsonify({
            "success": True,
            "items": items
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        cursor.close()
        db.close()


@next_week_candidates_bp.route(
    "/api/next-week-candidates/batches/<int:batch_id>",
    methods=["GET"]
)
@login_required
def api_candidate_batch_detail(batch_id):

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                batch_id,
                week_id,
                include_weekly_remaining,
                generated_at
            FROM book_selection_batches
            WHERE batch_id = %s
            LIMIT 1
            """,
            (batch_id,)
        )

        batch = cursor.fetchone()

        if not batch:
            return jsonify({
                "success": False,
                "message": "Candidate Batch not found."
            }), 404

        batch["generated_at"] = format_et(
            batch.get("generated_at")
        )

        cursor.execute(
            """
            SELECT
                bl.week_id,
                bl.store_id,
                COALESCE(s.short_name, '') AS store_code,
                COALESCE(s.store_name, '') AS store_name,
                bl.live_id
            FROM book_selection_batch_lives bl

            LEFT JOIN stores s
                ON s.store_id = bl.store_id

            WHERE bl.batch_id = %s

            ORDER BY
                bl.batch_live_id
            """,
            (batch_id,)
        )

        lives = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                c.sku,
                COALESCE(bs.book_title, '') AS book_title,
                bs.spice_type,
                c.available_stock,
                c.selected_live_sales,
                c.weekly_remaining,
                c.candidate_stock
            FROM book_selection_candidates c

            LEFT JOIN book_sku bs
                ON bs.isbn = c.sku

            WHERE c.batch_id = %s

            ORDER BY
                c.candidate_stock DESC,
                c.sku ASC
            """,
            (batch_id,)
        )

        items = cursor.fetchall()

        return jsonify({
            "success": True,
            "batch": batch,
            "selected_lives": lives,
            "selected_live_count": len(lives),
            "candidate_count": len(items),
            "items": items
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        cursor.close()
        db.close()


# =========================================================
# API: generate candidate books
# =========================================================

@next_week_candidates_bp.route(
    "/api/next-week-candidates/generate",
    methods=["POST"]
)
@login_required
def api_generate_next_week_candidates():

    data = request.get_json() or {}

    current_week_id = str(
        data.get(
            "current_week_id",
            ""
        )
    ).strip()

    selected_lives = data.get(
        "selected_lives",
        []
    )

    include_weekly_remaining = bool(
        data.get(
            "include_weekly_remaining",
            True
        )
    )


    if not current_week_id:

        return jsonify({
            "success": False,
            "message":
                "Current Week ID is required"
        }), 400

    if not isinstance(
        selected_lives,
        list
    ):

        return jsonify({
            "success": False,
            "message":
                "selected_lives must be a list"
        }), 400

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        normalized_lives = (
            _normalize_selected_lives(
                selected_lives
            )
        )

        items = get_candidate_books(
            cursor=cursor,
            current_week_id=
                current_week_id,
            selected_lives=
                normalized_lives,
            include_weekly_remaining=
                include_weekly_remaining
        )

        # =====================================================
        # Save this generation as one batch
        # =====================================================

        cursor.execute(
            """
            INSERT INTO book_selection_batches
            (
                week_id,
                include_weekly_remaining
            )
            VALUES
            (
                %s,
                %s
            )
            """,
            (
                current_week_id,
                bool(
                    include_weekly_remaining
                )
            )
        )

        batch_id = cursor.lastrowid

        # =====================================================
        # Save selected LIVE sessions
        #
        # A selected LIVE can belong to a different week than
        # current_week_id, so its own week_id must be retained.
        # =====================================================

        if normalized_lives:

            live_rows = [
                (
                    batch_id,
                    item["week_id"],
                    item["store_id"],
                    item["live_id"]
                )
                for item
                in normalized_lives
            ]

            cursor.executemany(
                """
                INSERT INTO book_selection_batch_lives
                (
                    batch_id,
                    week_id,
                    store_id,
                    live_id
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                live_rows
            )

        # =====================================================
        # Save candidate detail
        # =====================================================

        if items:

            candidate_rows = [
                (
                    batch_id,
                    item["sku"],
                    item.get(
                        "available_stock",
                        0
                    ),
                    item.get(
                        "selected_live_sales",
                        0
                    ),
                    item.get(
                        "weekly_remaining",
                        0
                    ),
                    item.get(
                        "candidate_stock",
                        0
                    )
                )
                for item
                in items
            ]

            cursor.executemany(
                """
                INSERT INTO book_selection_candidates
                (
                    batch_id,
                    sku,
                    available_stock,
                    selected_live_sales,
                    weekly_remaining,
                    candidate_stock
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                candidate_rows
            )

        db.commit()

        return jsonify({
            "success": True,
            "batch_id":
                batch_id,
            "current_week_id":
                current_week_id,
            "selected_lives":
                normalized_lives,
            "selected_live_count":
                len(normalized_lives),
            "include_weekly_remaining":
                include_weekly_remaining,
            "candidate_count":
                len(items),
            "items":
                items
        })

    except ValueError as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 400

    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

