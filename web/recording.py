from flask import Blueprint, Response, jsonify, render_template, request
from flask_login import current_user, login_required
import csv
import io
import math

from db import get_db
from inventory_service import get_weekly_item_status
from utils import format_et

recording_bp = Blueprint("recording", __name__)

def write_recording_log(
    cursor,
    recording_id,
    action_type,
    old_sku,
    new_sku,
    store_id,
    live_id
):

    cursor.execute(
        """
        INSERT INTO sku_recording_log (
            recording_id,
            action_type,
            old_sku,
            new_sku,
            store_id,
            live_id,
            user_id,
            username,
            request_ip
        )

        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        """,
        (
            recording_id,
            action_type,
            old_sku,
            new_sku,
            store_id,
            live_id,
            current_user.id,
            current_user.username,
            request.remote_addr
        )
    )

@recording_bp.route(
    "/recording/<int:store_id>"
)
@login_required
def recording(store_id):

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                store_name

            FROM stores

            WHERE store_id = %s
            """,
            (
                store_id,
            )
        )

        store = cursor.fetchone()

        if store is None:

            return (
                "Store not found",
                404
            )

        return render_template(
            "recording.html",
            store_id=store_id,
            store_name=store["store_name"]
        )

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings/session-info",
    methods=["GET"]
)
@login_required
def recording_session_info():

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

    live_id = str(
        request.args.get(
            "live_id",
            ""
        )
    ).strip()


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


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_items,

                COALESCE(
                    MAX(round_no),
                    1
                ) AS max_round

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                week_id,
                store_id,
                live_id
            )
        )

        row = cursor.fetchone()

        max_round = int(
            row["max_round"]
            or 1
        )

        cursor.execute(
            """
            SELECT
                COALESCE(
                    MAX(seq),
                    0
                ) AS max_seq

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s
            """,
            (
                week_id,
                store_id,
                live_id,
                max_round
            )
        )

        seq_row = cursor.fetchone()

        max_seq = int(
            seq_row["max_seq"]
            or 0
        )

        return jsonify({
            "success": True,

            "total_items":
                int(
                    row["total_items"]
                    or 0
                ),

            "max_round":
                max_round,

            "next_seq":
                max_seq + 1
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

@recording_bp.route(
    "/api/record",
    methods=["POST"]
)
@login_required
def record():

    data = request.get_json() or {}

    week_id = str(
        data.get(
            "week_id",
            ""
        )
    ).strip()

    sku = str(
        data.get(
            "sku",
            ""
        )
    ).strip()

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get(
        "store_id"
    )

    round_no = data.get(
        "round_no"
    )


    if not week_id:

        return jsonify({
            "success": False,
            "message":
                "Week ID is required"
        }), 400


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
        }), 400


    if not sku:

        return jsonify({
            "success": False,
            "message":
                "SKU is empty"
        }), 400


    if store_id is None:

        return jsonify({
            "success": False,
            "message":
                "Store ID is required"
        }), 400


    try:

        round_no = int(
            round_no
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "message":
                "Round number is required"
        }), 400


    if round_no < 1:

        return jsonify({
            "success": False,
            "message":
                "Invalid Round"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT 1

            FROM weekly_inventory

            WHERE week_id = %s
              AND store_id = %s

            LIMIT 1
            """,
            (
                week_id,
                store_id
            )
        )

        if cursor.fetchone() is None:

            return jsonify({
                "success": False,
                "message":
                    "Weekly inventory not found "
                    "for this store and week"
            }), 400


        cursor.execute(
            """
            INSERT IGNORE INTO book_sku (
                isbn
            )

            VALUES (%s)
            """,
            (
                sku,
            )
        )


        cursor.execute(
            """
            SELECT
                COALESCE(
                    MAX(seq),
                    0
                ) AS max_seq

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no
            )
        )

        seq_row = cursor.fetchone()

        next_seq = (
            int(
                seq_row["max_seq"]
                or 0
            )
            + 1
        )


        cursor.execute(
            """
            INSERT INTO sku_recording (
                seq,
                round_no,
                week_id,
                sku,
                store_id,
                quantity,
                live_id
            )

            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                next_seq,
                round_no,
                week_id,
                sku,
                store_id,
                1,
                live_id
            )
        )

        recording_id = (
            cursor.lastrowid
        )


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="CREATE",
            old_sku=None,
            new_sku=sku,
            store_id=store_id,
            live_id=live_id
        )


        cursor.execute(
            """
            SELECT
                sr.recording_id,
                sr.round_no,
                sr.seq,
                sr.week_id,
                sr.sku,
                sr.quantity,
                bs.book_title,
                bs.book_author,
                bs.book_format,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN book_sku bs
                ON bs.isbn = sr.sku

            WHERE sr.recording_id = %s
            """,
            (
                recording_id,
            )
        )

        new_row = cursor.fetchone()


        weekly_status = (
            get_weekly_item_status(
                cursor=cursor,
                week_id=week_id,
                store_id=store_id,
                sku=sku
            )
        )


        db.commit()


        new_row["recorded_at"] = (
            format_et(
                new_row["recorded_at"]
            )
        )


        return jsonify({
            "success": True,

            "recording":
                new_row,

            "book": {
                "book_title":
                    new_row["book_title"],

                "book_author":
                    new_row["book_author"],

                "book_format":
                    new_row["book_format"]
            },

            "weekly_status":
                weekly_status
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings/insert-before",
    methods=["POST"]
)
@login_required
def insert_recording_before():

    data = request.get_json() or {}

    week_id = str(
        data.get(
            "week_id",
            ""
        )
    ).strip()

    sku = str(
        data.get(
            "sku",
            ""
        )
    ).strip()

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get(
        "store_id"
    )

    round_no = data.get(
        "round_no"
    )

    before_seq = data.get(
        "before_seq"
    )


    if not week_id:

        return jsonify({
            "success": False,
            "message":
                "Week ID is required"
        }), 400


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
        }), 400


    if not sku:

        return jsonify({
            "success": False,
            "message":
                "SKU is empty"
        }), 400


    if store_id is None:

        return jsonify({
            "success": False,
            "message":
                "Store ID is required"
        }), 400


    try:

        round_no = int(
            round_no
        )

        before_seq = int(
            before_seq
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid Round or Seq"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                recording_id

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s
              AND seq = %s

            LIMIT 1
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no,
                before_seq
            )
        )

        target = cursor.fetchone()

        if target is None:

            return jsonify({
                "success": False,
                "message":
                    "Insert position not found"
            }), 404


        cursor.execute(
            """
            INSERT IGNORE INTO book_sku (
                isbn
            )

            VALUES (%s)
            """,
            (
                sku,
            )
        )


        cursor.execute(
            """
            UPDATE sku_recording

            SET seq = seq + 1

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s
              AND seq >= %s
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no,
                before_seq
            )
        )


        cursor.execute(
            """
            INSERT INTO sku_recording (
                seq,
                round_no,
                week_id,
                sku,
                store_id,
                quantity,
                live_id
            )

            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                before_seq,
                round_no,
                week_id,
                sku,
                store_id,
                1,
                live_id
            )
        )

        recording_id = (
            cursor.lastrowid
        )


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="CREATE",
            old_sku=None,
            new_sku=sku,
            store_id=store_id,
            live_id=live_id
        )


        cursor.execute(
            """
            SELECT
                book_title,
                book_author,
                book_format

            FROM book_sku

            WHERE isbn = %s
            """,
            (
                sku,
            )
        )

        book = cursor.fetchone()


        weekly_status = (
            get_weekly_item_status(
                cursor=cursor,
                week_id=week_id,
                store_id=store_id,
                sku=sku
            )
        )


        db.commit()


        return jsonify({
            "success": True,
            "recording_id":
                recording_id,
            "round_no":
                round_no,
            "seq":
                before_seq,
            "book":
                book,
            "weekly_status":
                weekly_status
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings",
    methods=["GET"]
)
@login_required
def get_recordings():

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

    live_id = str(
        request.args.get(
            "live_id",
            ""
        )
    ).strip()

    page = request.args.get(
        "page",
        default=1,
        type=int
    )

    page_size = 100


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


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
        }), 400


    if (
        page is None
        or page < 1
    ):

        page = 1


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                week_id,
                store_id,
                live_id
            )
        )

        total_row = cursor.fetchone()

        total_items = int(
            total_row["total"]
            or 0
        )

        total_pages = max(
            1,
            math.ceil(
                total_items
                /
                page_size
            )
        )

        if page > total_pages:

            page = total_pages

        offset = (
            page - 1
        ) * page_size


        cursor.execute(
            """
            SELECT
                sr.recording_id,
                sr.round_no,
                sr.seq,
                sr.week_id,
                sr.sku,
                sr.quantity,
                bs.book_title,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN book_sku bs
                ON bs.isbn = sr.sku

            WHERE sr.week_id = %s
              AND sr.store_id = %s
              AND sr.live_id = %s

            ORDER BY
                sr.round_no ASC,
                sr.seq ASC,
                sr.recording_id ASC

            LIMIT %s
            OFFSET %s
            """,
            (
                week_id,
                store_id,
                live_id,
                page_size,
                offset
            )
        )

        rows = cursor.fetchall()

        for row in rows:

            row["recorded_at"] = (
                format_et(
                    row["recorded_at"]
                )
            )


        return jsonify({
            "success": True,

            "items":
                rows,

            "pagination": {

                "page":
                    page,

                "page_size":
                    page_size,

                "total_items":
                    total_items,

                "total_pages":
                    total_pages,

                "has_previous":
                    page > 1,

                "has_next":
                    page < total_pages,

                "start_item":
                    (
                        offset + 1
                        if total_items > 0
                        else 0
                    ),

                "end_item":
                    min(
                        offset
                        + page_size,
                        total_items
                    )
            }
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

@recording_bp.route(
    "/api/recordings/<int:recording_id>",
    methods=["PUT"]
)
@login_required
def update_recording(recording_id):

    data = request.get_json() or {}

    week_id = str(
        data.get(
            "week_id",
            ""
        )
    ).strip()

    sku = str(
        data.get(
            "sku",
            ""
        )
    ).strip()

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get(
        "store_id"
    )


    if not week_id:

        return jsonify({
            "success": False,
            "message":
                "Week ID is required"
        }), 400


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
        }), 400


    if not sku:

        return jsonify({
            "success": False,
            "message":
                "SKU is empty"
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
                sku

            FROM sku_recording

            WHERE recording_id = %s
              AND week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                recording_id,
                week_id,
                store_id,
                live_id
            )
        )

        old_row = cursor.fetchone()

        if old_row is None:

            return jsonify({
                "success": False,
                "message":
                    "Recording not found "
                    "in current LIVE"
            }), 404


        old_sku = (
            old_row["sku"]
        )


        if old_sku == sku:

            return jsonify({
                "success": True,
                "message":
                    "No changes"
            })


        cursor.execute(
            """
            INSERT IGNORE INTO book_sku (
                isbn
            )

            VALUES (%s)
            """,
            (
                sku,
            )
        )


        cursor.execute(
            """
            UPDATE sku_recording

            SET sku = %s

            WHERE recording_id = %s
              AND week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                sku,
                recording_id,
                week_id,
                store_id,
                live_id
            )
        )


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="UPDATE",
            old_sku=old_sku,
            new_sku=sku,
            store_id=store_id,
            live_id=live_id
        )


        db.commit()


        return jsonify({
            "success": True
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings/<int:recording_id>",
    methods=["DELETE"]
)
@login_required
def delete_recording(recording_id):

    data = request.get_json() or {}

    week_id = str(
        data.get(
            "week_id",
            ""
        )
    ).strip()

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get(
        "store_id"
    )


    if not week_id:

        return jsonify({
            "success": False,
            "message":
                "Week ID is required"
        }), 400


    if not live_id:

        return jsonify({
            "success": False,
            "message":
                "LIVE ID is required"
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
                sku,
                round_no,
                seq

            FROM sku_recording

            WHERE recording_id = %s
              AND week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                recording_id,
                week_id,
                store_id,
                live_id
            )
        )

        old_row = cursor.fetchone()

        if old_row is None:

            return jsonify({
                "success": False,
                "message":
                    "Recording not found "
                    "in current LIVE"
            }), 404


        old_sku = (
            old_row["sku"]
        )

        old_round = (
            old_row["round_no"]
        )

        old_seq = (
            old_row["seq"]
        )


        cursor.execute(
            """
            DELETE FROM sku_recording

            WHERE recording_id = %s
              AND week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                recording_id,
                week_id,
                store_id,
                live_id
            )
        )


        if (
            old_round is not None
            and old_seq is not None
        ):

            cursor.execute(
                """
                UPDATE sku_recording

                SET seq = seq - 1

                WHERE week_id = %s
                  AND store_id = %s
                  AND live_id = %s
                  AND round_no = %s
                  AND seq > %s
                """,
                (
                    week_id,
                    store_id,
                    live_id,
                    old_round,
                    old_seq
                )
            )


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="DELETE",
            old_sku=old_sku,
            new_sku=None,
            store_id=store_id,
            live_id=live_id
        )


        db.commit()


        return jsonify({
            "success": True
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings/download",
    methods=["GET"]
)
@login_required
def download_recordings():

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

    live_id = str(
        request.args.get(
            "live_id",
            ""
        )
    ).strip()


    if not week_id:

        return (
            "Week ID is required",
            400
        )


    if store_id is None:

        return (
            "Store ID is required",
            400
        )


    if not live_id:

        return (
            "LIVE ID is required",
            400
        )


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                store_name

            FROM stores

            WHERE store_id = %s
            """,
            (
                store_id,
            )
        )

        store = cursor.fetchone()

        if store is None:

            return (
                "Store not found",
                404
            )


        cursor.execute(
            """
            SELECT
                sr.round_no,
                sr.seq,
                sr.sku,
                sr.quantity,
                bs.book_title,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN book_sku bs
                ON bs.isbn = sr.sku

            WHERE sr.week_id = %s
              AND sr.store_id = %s
              AND sr.live_id = %s

            ORDER BY
                sr.round_no ASC,
                sr.seq ASC,
                sr.recording_id ASC
            """,
            (
                week_id,
                store_id,
                live_id
            )
        )

        rows = cursor.fetchall()


        output = io.StringIO()

        writer = csv.writer(
            output
        )


        writer.writerow([
            "Round",
            "Seq",
            "Week ID",
            "Store ID",
            "LIVE ID",
            "SKU",
            "Book Title",
            "Quantity",
            "Recorded Time ET"
        ])


        for row in rows:

            writer.writerow([
                row["round_no"],
                row["seq"],
                week_id,
                store_id,
                live_id,
                row["sku"],
                row["book_title"]
                    or "",
                row["quantity"],
                format_et(
                    row["recorded_at"]
                )
            ])


        csv_content = (
            "\ufeff"
            +
            output.getvalue()
        )


        store_name = (
            store["store_name"]
            .replace(
                " ",
                "_"
            )
            .replace(
                "/",
                "_"
            )
            .replace(
                "\\",
                "_"
            )
        )


        safe_live_id = (
            live_id
            .replace(
                " ",
                "_"
            )
            .replace(
                "/",
                "_"
            )
            .replace(
                "\\",
                "_"
            )
        )


        safe_week_id = (
            week_id
            .replace(
                "/",
                "_"
            )
            .replace(
                "\\",
                "_"
            )
        )


        filename = (
            f"{store_name}_"
            f"{safe_week_id}_"
            f"{safe_live_id}_"
            f"recording.csv"
        )


        return Response(
            csv_content,

            mimetype=
                "text/csv; charset=utf-8",

            headers={
                "Content-Disposition":
                    (
                        f'attachment; '
                        f'filename="{filename}"'
                    )
            }
        )


    finally:

        cursor.close()
        db.close()
