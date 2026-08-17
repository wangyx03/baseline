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
    MODULE_WEEKLY_INVENTORY,
    MODULE_RECORDING
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

    return render_template(
        "weekly_inventory.html",
        extra_nav_links=extra_nav_links
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


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                wi.sku,

                COALESCE(
                    bs.book_title,
                    ''
                ) AS title,

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
                    week_id,
                    store_id,
                    sku,
                    SUM(quantity)
                        AS locked_used

                FROM inventory_locked

                GROUP BY
                    week_id,
                    store_id,
                    sku

            ) locked_data

                ON locked_data.week_id =
                    wi.week_id

                AND locked_data.store_id =
                    wi.store_id

                AND locked_data.sku =
                    wi.sku

            LEFT JOIN (
                SELECT
                    sr.week_id,
                    sr.store_id,
                    sr.sku,

                    SUM(
                        sr.quantity
                    ) AS draft_used

                FROM sku_recording sr

                WHERE NOT EXISTS (
                    SELECT 1

                    FROM inventory_locked il2

                    WHERE il2.week_id =
                        sr.week_id

                      AND il2.store_id =
                        sr.store_id

                      AND il2.live_id =
                        sr.live_id
                )

                GROUP BY
                    sr.week_id,
                    sr.store_id,
                    sr.sku

            ) draft_data

                ON draft_data.week_id =
                    wi.week_id

                AND draft_data.store_id =
                    wi.store_id

                AND draft_data.sku =
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
                store_id
            )
        )

        rows = cursor.fetchall()


        return jsonify({
            "success": True,
            "week_id":
                week_id,
            "store_id":
                store_id,
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
