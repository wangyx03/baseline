from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
    url_for
)
from flask_login import login_required

from permissions.permissions import(
    module_required,
    has_module,
    has_permission,
    MODULE_WEEKLY_INVENTORY,
    MODULE_RECORDING,
    PERMISSION_WEEKLY_ACTUAL_STOCK
)

from db import get_db

weekly_bp = Blueprint(
    "weekly",
    __name__,
    template_folder="templates"
)

@weekly_bp.before_request
@module_required(MODULE_WEEKLY_INVENTORY)
def require_weekly_inventory_access():
    pass

@weekly_bp.route(
    "/weekly-inventory"
)
@login_required
def weekly_inventory_page():

    extra_nav_links = []

    if has_module(MODULE_RECORDING):

        extra_nav_links.extend([
            {
                "label": "TU Recording",
                "url": url_for(
                    "recording.recording",
                    store_code="TU"
                )
            },
            {
                "label": "VB Recording",
                "url": url_for(
                    "recording.recording",
                    store_code="VB"
                )
            }
        ])

    can_view_actual_stock = has_permission(
        PERMISSION_WEEKLY_ACTUAL_STOCK
    )

    return render_template(
        "weekly_inventory.html",
        extra_nav_links=extra_nav_links,
        can_view_actual_stock=can_view_actual_stock
    )

@weekly_bp.route(
    "/api/weekly-weeks",
    methods=["GET"]
)
@login_required
def get_weekly_weeks():

    store_id = request.args.get(
        "store_id",
        type=int
    )

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        if store_id is None:

            cursor.execute(
                """
                SELECT DISTINCT
                    week_id

                FROM weekly_inventory

                WHERE week_id IS NOT NULL
                  AND week_id <> ''

                ORDER BY
                    week_id DESC
                """
            )

        else:

            cursor.execute(
                """
                SELECT DISTINCT
                    week_id

                FROM weekly_inventory

                WHERE week_id IS NOT NULL
                  AND week_id <> ''
                  AND store_id = %s

                ORDER BY
                    week_id DESC
                """,
                (
                    store_id,
                )
            )

        rows = cursor.fetchall()


        return jsonify({
            "success": True,

            "weeks": [
                row["week_id"]
                for row in rows
            ]
        })


    except Exception as e:

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

@weekly_bp.route(
    "/api/weekly-inventory/version",
    methods=["GET"]
)
@login_required
def get_weekly_inventory_version():

    week_id = str(
        request.args.get(
            "week_id",
            ""
        )
    ).strip()

    store_id = request.args.get(
        "store_id",
        type=int
    )

    if not week_id:

        return jsonify({
            "success": False,
            "message": "Week ID is required"
        }), 400

    if store_id is None:

        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                COALESCE(
                    MAX(log_id),
                    0
                ) AS version

            FROM sku_recording_log

            WHERE week_id = %s
              AND store_id = %s
            """,
            (
                week_id,
                store_id
            )
        )

        row = cursor.fetchone()

        return jsonify({
            "success": True,
            "version": int(
                row["version"]
                or 0
            )
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


@weekly_bp.route(
    "/api/weekly-inventory",
    methods=["GET"]
)
@login_required
def get_weekly_inventory():

    week_id = str(
        request.args.get(
            "week_id",
            ""
        )
    ).strip()

    store_id = request.args.get(
        "store_id",
        type=int
    )


    if not week_id:

        return jsonify({
            "success": False,
            "message":
                "Week ID is required"
        }), 400


    if store_id is None:

        return jsonify({
            "success": False,
            "message":
                "Store ID is required"
        }), 400


    can_view_actual_stock = has_permission(
        PERMISSION_WEEKLY_ACTUAL_STOCK
    )

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        # Capture the current log version before the heavier inventory
        # calculation. If another recording change happens while this
        # query is running, the next lightweight version check will
        # detect it and trigger another refresh.
        cursor.execute(
            """
            SELECT
                COALESCE(
                    MAX(log_id),
                    0
                ) AS version

            FROM sku_recording_log

            WHERE week_id = %s
              AND store_id = %s
            """,
            (
                week_id,
                store_id
            )
        )

        version_row = cursor.fetchone()

        current_version = int(
            version_row["version"]
            or 0
        )

        cursor.execute(
            """
            SELECT
                wi.sku,

                COALESCE(
                    bs.book_title,
                    ''
                ) AS title,

                COALESCE(
                    stock_data.actual_stock,
                    0
                ) AS actual_stock,

                wi.planned_qty
                    AS planned,

                (
                    COALESCE(
                        locked_data.locked_used,
                        0
                    )
                    +
                    COALESCE(
                        draft_data.draft_used,
                        0
                    )
                ) AS used,

                wi.planned_qty
                -
                (
                    COALESCE(
                        locked_data.locked_used,
                        0
                    )
                    +
                    COALESCE(
                        draft_data.draft_used,
                        0
                    )
                ) AS remaining

            FROM weekly_inventory wi

            LEFT JOIN book_sku bs
                ON bs.isbn = wi.sku

            LEFT JOIN (
                SELECT
                    sku,
                    SUM(available_stock)
                        AS actual_stock

                FROM inventory_snapshot

                WHERE stock_type = 'Good'

                GROUP BY
                    sku

            ) stock_data
                ON stock_data.sku =
                    wi.sku

            LEFT JOIN (
                SELECT
                    il.sku,
                    SUM(il.quantity)
                        AS locked_used

                FROM inventory_locked il

                WHERE il.week_id = %s
                  AND il.store_id = %s

                GROUP BY
                    il.sku

            ) locked_data
                ON locked_data.sku =
                    wi.sku

            LEFT JOIN (
                SELECT
                    sr.sku,

                    SUM(
                        sr.quantity
                    ) AS draft_used

                FROM sku_recording sr

                LEFT JOIN (
                    SELECT DISTINCT
                        week_id,
                        store_id,
                        live_id

                    FROM inventory_locked

                    WHERE week_id = %s
                      AND store_id = %s
                ) locked_live

                    ON locked_live.week_id =
                        sr.week_id

                    AND locked_live.store_id =
                        sr.store_id

                    AND locked_live.live_id =
                        sr.live_id

                WHERE sr.week_id = %s
                  AND sr.store_id = %s
                  AND locked_live.live_id IS NULL

                GROUP BY
                    sr.sku

            ) draft_data
                ON draft_data.sku =
                    wi.sku

            WHERE wi.week_id = %s
              AND wi.store_id = %s

            ORDER BY
                used ASC,
                remaining DESC,
                wi.sku ASC
            """,
            (
                week_id,
                store_id,
                week_id,
                store_id,
                week_id,
                store_id,
                week_id,
                store_id
            )
        )

        rows = cursor.fetchall()

        if not can_view_actual_stock:

            for row in rows:

                row.pop(
                    "actual_stock",
                    None
                )


        return jsonify({
            "success": True,
            "week_id":
                week_id,
            "store_id":
                store_id,
            "version":
                current_version,
            "can_view_actual_stock":
                can_view_actual_stock,
            "items":
                rows
        })


    except Exception as e:

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()
