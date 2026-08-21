from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from db import get_db

from permissions.permissions import (
    module_required,
    MODULE_BOOK_SELECTION
)


resident_books_bp = Blueprint(
    "resident_books",
    __name__,
    template_folder="templates"
)


@resident_books_bp.before_request
@module_required(
    MODULE_BOOK_SELECTION
)
def require_book_selection_access():
    pass


@resident_books_bp.route(
    "/resident-books"
)
@login_required
def resident_books_page():

    return render_template(
        "resident_books.html",
        extra_nav_links=[
            {
                "label": "Candidate List",
                "url": "/next-week-candidates"
            }
        ]
    )


@resident_books_bp.route(
    "/api/resident-books/options"
)
@login_required
def resident_books_options():

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                store_id,
                short_name,
                store_name
            FROM stores
            ORDER BY store_id
            """
        )

        stores = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                batch_id,
                week_id,
                include_weekly_remaining,
                generated_at
            FROM book_selection_batches
            ORDER BY batch_id DESC
            LIMIT 50
            """
        )

        batches = cursor.fetchall()

        for item in batches:
            if item.get("generated_at"):
                item["generated_at"] = (
                    item["generated_at"]
                    .strftime("%Y-%m-%d %H:%M:%S")
                )

        return jsonify({
            "success": True,
            "stores": stores,
            "batches": batches,
            "latest_batch_id":
                batches[0]["batch_id"]
                if batches
                else None
        })

    finally:
        cursor.close()
        db.close()


@resident_books_bp.route(
    "/api/resident-books"
)
@login_required
def resident_books_list():

    batch_id = request.args.get(
        "batch_id",
        type=int
    )

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        if batch_id is None:

            cursor.execute(
                """
                SELECT batch_id
                FROM book_selection_batches
                ORDER BY batch_id DESC
                LIMIT 1
                """
            )

            latest = cursor.fetchone()

            batch_id = (
                latest["batch_id"]
                if latest
                else None
            )

        cursor.execute(
            """
            SELECT
                r.resident_id,
                r.store_id,
                s.short_name AS store_code,
                s.store_name,
                r.sku,
                COALESCE(
                    b.book_title,
                    ''
                ) AS book_title,
                r.active,

                c.candidate_stock,

                CASE
                    WHEN shared.store_count > 1
                    THEN 1
                    ELSE 0
                END AS is_shared

            FROM book_selection_resident r

            JOIN stores s
                ON s.store_id =
                       r.store_id

            JOIN book_sku b
                ON b.isbn =
                       r.sku

            LEFT JOIN book_selection_candidates c
                ON c.batch_id = %s
               AND c.sku =
                       r.sku

            LEFT JOIN (
                SELECT
                    sku,
                    COUNT(
                        DISTINCT store_id
                    ) AS store_count
                FROM book_selection_resident
                WHERE active = 1
                GROUP BY sku
            ) shared
                ON shared.sku =
                       r.sku

            ORDER BY
                r.active DESC,
                is_shared DESC,
                s.short_name,
                b.book_title,
                r.sku
            """,
            (batch_id,)
        )

        items = cursor.fetchall()

        return jsonify({
            "success": True,
            "batch_id": batch_id,
            "items": items
        })

    finally:
        cursor.close()
        db.close()


@resident_books_bp.route(
    "/api/resident-books/add",
    methods=["POST"]
)
@login_required
def resident_books_add():

    payload = request.get_json(
        silent=True
    ) or {}

    store_id = payload.get("store_id")
    sku = str(
        payload.get("sku", "")
    ).strip()

    if not store_id or not sku:

        return jsonify({
            "success": False,
            "message":
                "Store and SKU are required."
        }), 400

    try:
        store_id = int(store_id)
    except (TypeError, ValueError):

        return jsonify({
            "success": False,
            "message": "Invalid store."
        }), 400

    if len(sku) != 13 or not sku.isdigit():

        return jsonify({
            "success": False,
            "message":
                "SKU must be a 13-digit ISBN."
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                isbn,
                book_title
            FROM book_sku
            WHERE isbn = %s
            LIMIT 1
            """,
            (sku,)
        )

        book = cursor.fetchone()

        if not book:

            return jsonify({
                "success": False,
                "message":
                    "SKU was not found in book_sku."
            }), 404

        cursor.execute(
            """
            SELECT store_id
            FROM stores
            WHERE store_id = %s
            LIMIT 1
            """,
            (store_id,)
        )

        if not cursor.fetchone():

            return jsonify({
                "success": False,
                "message":
                    "Store was not found."
            }), 404

        cursor.execute(
            """
            INSERT INTO book_selection_resident
            (
                store_id,
                sku,
                active
            )
            VALUES
            (
                %s,
                %s,
                1
            )
            ON DUPLICATE KEY UPDATE
                active = 1
            """,
            (
                store_id,
                sku
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "message":
                "Resident book saved."
        })

    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        cursor.close()
        db.close()


@resident_books_bp.route(
    "/api/resident-books/<int:resident_id>/active",
    methods=["POST"]
)
@login_required
def resident_books_set_active(
    resident_id
):

    payload = request.get_json(
        silent=True
    ) or {}

    active = payload.get("active")

    if active not in (
        True,
        False,
        1,
        0
    ):

        return jsonify({
            "success": False,
            "message":
                "active must be true or false."
        }), 400

    db = get_db()
    cursor = db.cursor()

    try:

        cursor.execute(
            """
            UPDATE book_selection_resident
            SET active = %s
            WHERE resident_id = %s
            """,
            (
                1 if active else 0,
                resident_id
            )
        )

        if cursor.rowcount == 0:

            db.rollback()

            return jsonify({
                "success": False,
                "message":
                    "Resident book not found."
            }), 404

        db.commit()

        return jsonify({
            "success": True
        })

    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        cursor.close()
        db.close()
