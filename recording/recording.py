from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    url_for
)
from permissions.permissions import(
    module_required,
    has_module,
    MODULE_RECORDING,
    MODULE_WEEKLY_INVENTORY
)
from flask_login import current_user, login_required
import csv
import io
import math

from db import get_db
from .inventory_service import (
    get_weekly_item_status,
    get_weekly_item_statuses
)
from utils import format_et

recording_bp = Blueprint(
    "recording",
    __name__,
    template_folder="templates"
)

@recording_bp.before_request
@module_required(MODULE_RECORDING)
def require_recording_access():
    pass

# Maximum number of sequence positions allowed in each round.
MAX_SEQ_PER_ROUND = 300


# =========================================================
# Global Recording sort setting
#
# Shared by every user and every Gunicorn worker because the
# value is stored in MySQL rather than in browser/session memory.
# DESC = newest Round / Seq first.
# ASC  = oldest Round / Seq first.
# =========================================================

RECORDING_SORT_SETTING_KEY = "round_seq_sort_direction"
DEFAULT_RECORDING_SORT_DIRECTION = "DESC"


def ensure_recording_settings_table(cursor):

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS recording_settings (
            setting_key VARCHAR(100) PRIMARY KEY,
            setting_value VARCHAR(50) NOT NULL,
            updated_at TIMESTAMP NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        INSERT IGNORE INTO recording_settings (
            setting_key,
            setting_value
        )
        VALUES (%s, %s)
        """,
        (
            RECORDING_SORT_SETTING_KEY,
            DEFAULT_RECORDING_SORT_DIRECTION
        )
    )


def get_recording_sort_direction(cursor):

    ensure_recording_settings_table(cursor)

    cursor.execute(
        """
        SELECT setting_value

        FROM recording_settings

        WHERE setting_key = %s

        LIMIT 1
        """,
        (
            RECORDING_SORT_SETTING_KEY,
        )
    )

    row = cursor.fetchone()

    if not row:
        return DEFAULT_RECORDING_SORT_DIRECTION

    direction = str(
        row.get("setting_value", "")
    ).upper().strip()

    if direction not in ("ASC", "DESC"):
        return DEFAULT_RECORDING_SORT_DIRECTION

    return direction

def write_recording_log(
    cursor,
    recording_id,
    action_type,
    old_sku,
    new_sku,
    week_id,
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
            week_id,
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
            %s,
            %s
        )
        """,
        (
            recording_id,
            action_type,
            old_sku,
            new_sku,
            week_id,
            store_id,
            live_id,
            current_user.id,
            current_user.username,
            request.remote_addr
        )
    )

def ensure_live_session(cursor, week_id, store_id, live_id):
    """Keep live_sessions derived from sku_recording."""
    cursor.execute(
        """
        INSERT INTO live_sessions (week_id, store_id, live_id, created_at)
        SELECT week_id, store_id, live_id, MIN(recorded_at)
        FROM sku_recording
        WHERE week_id = %s
          AND store_id = %s
          AND live_id = %s
        GROUP BY week_id, store_id, live_id
        ON DUPLICATE KEY UPDATE
            created_at = VALUES(created_at)
        """,
        (week_id, store_id, live_id)
    )


def delete_live_session_if_empty(cursor, week_id, store_id, live_id):
    cursor.execute(
        """
        DELETE FROM live_sessions
        WHERE week_id = %s
          AND store_id = %s
          AND live_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM sku_recording
              WHERE week_id = %s
                AND store_id = %s
                AND live_id = %s
          )
        """,
        (week_id, store_id, live_id, week_id, store_id, live_id)
    )


@recording_bp.route("/recording/<store_code>")
@login_required
def recording(store_code):

    store_code = store_code.upper().strip()

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                store_id,
                store_name,
                short_name

            FROM stores

            WHERE UPPER(short_name) = %s
            """,
            (
                store_code,
            )
        )

        store = cursor.fetchone()

        if not store:

            return "Invalid store", 404

        extra_nav_links = []

        if has_module(MODULE_WEEKLY_INVENTORY):
            extra_nav_links.append({
                "label": "Weekly Inventory",
                "url": url_for(
                    "weekly.weekly_inventory_page"
                )
            })


        return render_template(
            "recording.html",

            store_id=
                store["store_id"],

            store_name=
                store["store_name"],

            store_code=
                store["short_name"],

            extra_nav_links=extra_nav_links
        )

    finally:

        cursor.close()
        db.close()

@recording_bp.route(
    "/api/recordings/version",
    methods=["GET"]
)
@login_required
def recording_version():

    week_id = str(
        request.args.get("week_id", "")
    ).strip()

    store_id = request.args.get(
        "store_id",
        type=int
    )

    live_id = str(
        request.args.get("live_id", "")
    ).strip()

    if not week_id or store_id is None or not live_id:
        return jsonify({
            "success": False,
            "message": "Week ID, Store ID and LIVE ID are required"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                COALESCE(MAX(log_id), 0) AS version

            FROM sku_recording_log

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

        return jsonify({
            "success": True,
            "version": int(row["version"] or 0)
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

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


        # =====================================================
        # Next Seq in the selected Round
        #
        # A Round may contain at most 300 items.
        # Do NOT automatically move to the next Round.
        # The operator must explicitly Start New Round / Set Round.
        # =====================================================

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

        seq_row = cursor.fetchone() or {
            "max_seq": 0
        }

        max_seq = int(
            seq_row["max_seq"]
            or 0
        )

        if max_seq >= MAX_SEQ_PER_ROUND:

            return jsonify({
                "success": False,
                "message":
                    f"Round {round_no} is full (300 items). "
                    "Please start a new round manually."
            }), 409

        next_seq = max_seq + 1


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

        # Save the sku_recording ID immediately.
        # cursor.lastrowid can be changed by later INSERT/UPDATE statements.
        recording_id = (
            cursor.lastrowid
        )


        ensure_live_session(
            cursor,
            week_id,
            store_id,
            live_id
        )


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="CREATE",
            old_sku=None,
            new_sku=sku,
            week_id=week_id,
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

        # Save the sku_recording ID immediately.
        # cursor.lastrowid can be changed by later INSERT/UPDATE statements.
        recording_id = (
            cursor.lastrowid
        )


        ensure_live_session(
            cursor,
            week_id,
            store_id,
            live_id
        )

        # =====================================================
        # Keep every Round at a maximum of 300 Seq
        #
        # Insert Before can temporarily create Seq 301.
        # Move that overflow item to Seq 1 of the next Round,
        # pushing that Round forward. Continue cascading if the
        # next Round is also full.
        # =====================================================

        overflow_round = round_no

        while True:

            cursor.execute(
                """
                SELECT
                    recording_id,
                    seq

                FROM sku_recording

                WHERE week_id = %s
                  AND store_id = %s
                  AND live_id = %s
                  AND round_no = %s
                  AND seq > %s

                ORDER BY
                    seq ASC,
                    recording_id ASC

                LIMIT 1
                """,
                (
                    week_id,
                    store_id,
                    live_id,
                    overflow_round,
                    MAX_SEQ_PER_ROUND
                )
            )

            overflow_row = cursor.fetchone()

            if overflow_row is None:
                break

            next_round = overflow_round + 1

            cursor.execute(
                """
                UPDATE sku_recording

                SET seq = seq + 1

                WHERE week_id = %s
                  AND store_id = %s
                  AND live_id = %s
                  AND round_no = %s
                """,
                (
                    week_id,
                    store_id,
                    live_id,
                    next_round
                )
            )

            cursor.execute(
                """
                UPDATE sku_recording

                SET
                    round_no = %s,
                    seq = 1

                WHERE recording_id = %s
                """,
                (
                    next_round,
                    overflow_row["recording_id"]
                )
            )

            overflow_round = next_round


        write_recording_log(
            cursor=cursor,
            recording_id=recording_id,
            action_type="CREATE",
            old_sku=None,
            new_sku=sku,
            week_id=week_id,
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

# =========================================================
# Merge Current Round Into Previous Round
# =========================================================

@recording_bp.route(
    "/api/recordings/merge-previous-round",
    methods=["POST"]
)
@login_required
def merge_previous_round():

    data = request.get_json() or {}

    week_id = str(
        data.get("week_id", "")
    ).strip()

    store_id = data.get(
        "store_id"
    )

    live_id = str(
        data.get("live_id", "")
    ).strip()

    round_no = data.get(
        "round_no"
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

    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400

    try:
        round_no = int(round_no)
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Invalid Round"
        }), 400

    if round_no <= 1:
        return jsonify({
            "success": False,
            "message": "Round 1 has no previous round."
        }), 400

    previous_round = round_no - 1

    db = get_db()
    cursor = db.cursor(
        dictionary=True
    )

    try:

        # Lock previous round rows.
        cursor.execute(
            """
            SELECT
                recording_id,
                seq,
                sku

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s

            ORDER BY
                seq ASC,
                recording_id ASC

            FOR UPDATE
            """,
            (
                week_id,
                store_id,
                live_id,
                previous_round
            )
        )

        previous_rows = cursor.fetchall()

        # Lock current round rows.
        cursor.execute(
            """
            SELECT
                recording_id,
                seq,
                sku

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s

            ORDER BY
                seq ASC,
                recording_id ASC

            FOR UPDATE
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no
            )
        )

        current_rows = cursor.fetchall()

        if not current_rows:
            return jsonify({
                "success": False,
                "message":
                    f"Round {round_no} has no items."
            }), 404

        previous_count = len(previous_rows)
        current_count = len(current_rows)

        if previous_count >= MAX_SEQ_PER_ROUND:
            return jsonify({
                "success": False,
                "message":
                    f"Round {previous_round} is already full "
                    f"({MAX_SEQ_PER_ROUND} items)."
            }), 409

        available_slots = (
            MAX_SEQ_PER_ROUND
            - previous_count
        )

        move_count = min(
            available_slots,
            current_count
        )

        previous_max_seq = max(
            (
                int(row["seq"] or 0)
                for row in previous_rows
            ),
            default=0
        )

        rows_to_move = current_rows[:move_count]
        rows_to_keep = current_rows[move_count:]

        # Move as many leading items as possible from current round
        # into the end of the previous round.
        for index, row in enumerate(
            rows_to_move,
            start=1
        ):

            cursor.execute(
                """
                UPDATE sku_recording

                SET
                    round_no = %s,
                    seq = %s

                WHERE recording_id = %s
                  AND week_id = %s
                  AND store_id = %s
                  AND live_id = %s
                """,
                (
                    previous_round,
                    previous_max_seq + index,
                    row["recording_id"],
                    week_id,
                    store_id,
                    live_id
                )
            )

        if rows_to_keep:

            # Current round remains.
            # Re-number its remaining rows from Seq 1.
            for new_seq, row in enumerate(
                rows_to_keep,
                start=1
            ):

                cursor.execute(
                    """
                    UPDATE sku_recording

                    SET seq = %s

                    WHERE recording_id = %s
                      AND week_id = %s
                      AND store_id = %s
                      AND live_id = %s
                    """,
                    (
                        new_seq,
                        row["recording_id"],
                        week_id,
                        store_id,
                        live_id
                    )
                )

            db.commit()

            return jsonify({
                "success": True,
                "mode":
                    "filled_previous",
                "moved_items":
                    move_count,
                "result_round":
                    round_no,
                "previous_round":
                    previous_round,
                "previous_items":
                    previous_count + move_count,
                "remaining_items":
                    len(rows_to_keep)
            })

        # The entire current round fit into the previous round.
        # Close the round-number gap by shifting later rounds down.
        cursor.execute(
            """
            UPDATE sku_recording

            SET round_no = round_no - 1

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no > %s

            ORDER BY
                round_no ASC,
                seq ASC,
                recording_id ASC
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "mode":
                "full_merge",
            "merged_from_round":
                round_no,
            "result_round":
                previous_round,
            "previous_round":
                previous_round,
            "moved_items":
                move_count,
            "previous_items":
                previous_count + move_count,
            "remaining_items":
                0
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


# =========================================================
# Split Round
# =========================================================

@recording_bp.route(
    "/api/recordings/split-round",
    methods=["POST"]
)
@login_required
def split_round():

    data = request.get_json() or {}

    week_id = str(
        data.get("week_id", "")
    ).strip()

    store_id = data.get(
        "store_id"
    )

    live_id = str(
        data.get("live_id", "")
    ).strip()

    round_no = data.get(
        "round_no"
    )

    keep_count = data.get(
        "keep_count"
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

    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400

    try:
        round_no = int(round_no)
        keep_count = int(keep_count)
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message":
                "Invalid Round or split position."
        }), 400

    if round_no < 1:
        return jsonify({
            "success": False,
            "message": "Invalid Round"
        }), 400

    db = get_db()
    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                recording_id,
                seq,
                sku

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no = %s

            ORDER BY
                seq ASC,
                recording_id ASC

            FOR UPDATE
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no
            )
        )

        rows = cursor.fetchall()

        total_items = len(rows)

        if total_items < 2:
            return jsonify({
                "success": False,
                "message":
                    f"Round {round_no} does not have enough "
                    "items to split."
            }), 409

        if (
            keep_count < 1
            or keep_count >= total_items
        ):
            return jsonify({
                "success": False,
                "message":
                    f"Enter how many items should remain in "
                    f"Round {round_no}: 1 to "
                    f"{total_items - 1}."
            }), 400

        new_round = round_no + 1
        move_rows = rows[keep_count:]

        # Make room for the new round.
        # DESC order prevents collisions while shifting existing
        # later rounds upward.
        cursor.execute(
            """
            UPDATE sku_recording

            SET round_no = round_no + 1

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
              AND round_no > %s

            ORDER BY
                round_no DESC,
                seq DESC,
                recording_id DESC
            """,
            (
                week_id,
                store_id,
                live_id,
                round_no
            )
        )

        # Move the tail of the selected round into the new round
        # and restart its Seq at 1.
        for new_seq, row in enumerate(
            move_rows,
            start=1
        ):

            cursor.execute(
                """
                UPDATE sku_recording

                SET
                    round_no = %s,
                    seq = %s

                WHERE recording_id = %s
                  AND week_id = %s
                  AND store_id = %s
                  AND live_id = %s
                """,
                (
                    new_round,
                    new_seq,
                    row["recording_id"],
                    week_id,
                    store_id,
                    live_id
                )
            )

        db.commit()

        return jsonify({
            "success": True,
            "original_round":
                round_no,
            "new_round":
                new_round,
            "kept_items":
                keep_count,
            "moved_items":
                len(move_rows)
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


# =========================================================
# Change Recorded LIVE ID
# =========================================================

@recording_bp.route(
    "/api/recordings/change-live-id",
    methods=["POST"]
)
@login_required
def change_recorded_live_id():

    data = request.get_json() or {}

    week_id = str(
        data.get("week_id", "")
    ).strip()

    store_id = data.get(
        "store_id"
    )

    old_live_id = str(
        data.get("old_live_id", "")
    ).strip()

    new_live_id = str(
        data.get("new_live_id", "")
    ).strip()


    # =====================================================
    # Validation
    # =====================================================

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


    if not old_live_id:

        return jsonify({
            "success": False,
            "message": "Current LIVE ID is required"
        }), 400


    if not new_live_id:

        return jsonify({
            "success": False,
            "message": "New LIVE ID is required"
        }), 400


    if old_live_id == new_live_id:

        return jsonify({
            "success": False,
            "message": "New LIVE ID is the same as current LIVE ID"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        # =================================================
        # 1. Make sure current Recording exists
        # =================================================

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
                old_live_id
            )
        )

        row = cursor.fetchone()

        recording_count = int(
            row["total"] or 0
        )


        if recording_count == 0:

            return jsonify({
                "success": False,
                "message":
                    "No recording data found for current LIVE ID"
            }), 404


        # =================================================
        # 2. Current LIVE must NOT already be Locked
        # =================================================

        cursor.execute(
            """
            SELECT 1

            FROM inventory_locked

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s

            LIMIT 1
            """,
            (
                week_id,
                store_id,
                old_live_id
            )
        )


        if cursor.fetchone() is not None:

            return jsonify({
                "success": False,
                "message":
                    "This LIVE already has Locked data. "
                    "Recorded LIVE ID cannot be changed."
            }), 409


        # =================================================
        # 3. New LIVE ID must NOT already have Recording
        # =================================================

        cursor.execute(
            """
            SELECT 1

            FROM sku_recording

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s

            LIMIT 1
            """,
            (
                week_id,
                store_id,
                new_live_id
            )
        )


        if cursor.fetchone() is not None:

            return jsonify({
                "success": False,
                "message":
                    "The new LIVE ID already has Recording data."
            }), 409


        # =================================================
        # 4. New LIVE ID must NOT already have Locked data
        # =================================================

        cursor.execute(
            """
            SELECT 1

            FROM inventory_locked

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s

            LIMIT 1
            """,
            (
                week_id,
                store_id,
                new_live_id
            )
        )


        if cursor.fetchone() is not None:

            return jsonify({
                "success": False,
                "message":
                    "The new LIVE ID already has Locked data."
            }), 409


        # =================================================
        # 5. Change LIVE ID
        # =================================================

        cursor.execute(
            """
            UPDATE sku_recording

            SET live_id = %s

            WHERE week_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                new_live_id,
                week_id,
                store_id,
                old_live_id
            )
        )


        changed_rows = cursor.rowcount

        # Keep live_sessions synchronized with the renamed Recording session.
        ensure_live_session(
            cursor,
            week_id,
            store_id,
            new_live_id
        )
        delete_live_session_if_empty(
            cursor,
            week_id,
            store_id,
            old_live_id
        )

        # One session-level log entry is enough to signal all clients
        # that this Recording session changed.
        write_recording_log(
            cursor=cursor,
            recording_id=None,
            action_type="RENAME_LIVE",
            old_sku=None,
            new_sku=None,
            week_id=week_id,
            store_id=store_id,
            live_id=new_live_id
        )


        db.commit()


        return jsonify({
            "success": True,
            "old_live_id": old_live_id,
            "new_live_id": new_live_id,
            "changed_rows": changed_rows
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


# =========================================================
# Global Round / Seq sort direction
# =========================================================

@recording_bp.route(
    "/api/recordings/sort-direction",
    methods=["POST"]
)
@login_required
def toggle_recording_sort_direction():

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        current_direction = (
            get_recording_sort_direction(cursor)
        )

        new_direction = (
            "ASC"
            if current_direction == "DESC"
            else "DESC"
        )

        cursor.execute(
            """
            INSERT INTO recording_settings (
                setting_key,
                setting_value
            )

            VALUES (%s, %s)

            ON DUPLICATE KEY UPDATE
                setting_value = VALUES(setting_value)
            """,
            (
                RECORDING_SORT_SETTING_KEY,
                new_direction
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "sort_direction": new_direction
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


        sort_direction = (
            get_recording_sort_direction(cursor)
        )

        # sort_direction is restricted to ASC/DESC by the helper above,
        # so it is safe to place into the ORDER BY clause.
        cursor.execute(
            f"""
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
                sr.round_no {sort_direction},
                sr.seq {sort_direction},
                sr.recording_id {sort_direction}

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

        # Fetch weekly status for the whole page in one query.
        # Previously this executed one expensive query per row (N+1).
        weekly_statuses = get_weekly_item_statuses(
            cursor=cursor,
            week_id=week_id,
            store_id=store_id,
            skus=[
                row["sku"]
                for row in rows
            ]
        )

        for row in rows:

            weekly_status = weekly_statuses.get(
                str(row["sku"]).strip()
            )

            if (
                weekly_status
                and weekly_status.get(
                    "in_weekly_inventory"
                )
            ):

                row["remaining"] = (
                    weekly_status.get(
                        "remaining",
                        0
                    )
                )

            else:

                row["remaining"] = None


        return jsonify({
            "success": True,

            "items":
                rows,

            "sort_direction":
                sort_direction,

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
            week_id=week_id,
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
    "/api/recordings/delete-all",
    methods=["DELETE"]
)
@login_required
def delete_all_recordings():

    data = request.get_json() or {}

    week_id = str(
        data.get("week_id", "")
    ).strip()

    live_id = str(
        data.get("live_id", "")
    ).strip()

    store_id = data.get("store_id")

    if not week_id:
        return jsonify({
            "success": False,
            "message": "Week ID is required"
        }), 400

    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400

    if store_id is None:
        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                recording_id,
                sku

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

        rows = cursor.fetchall()

        if not rows:
            return jsonify({
                "success": True,
                "deleted_rows": 0
            })

        for row in rows:
            write_recording_log(
                cursor=cursor,
                recording_id=row["recording_id"],
                action_type="DELETE",
                old_sku=row["sku"],
                new_sku=None,
            week_id=week_id,
                store_id=store_id,
                live_id=live_id
            )

        cursor.execute(
            """
            DELETE FROM sku_recording

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

        deleted_rows = cursor.rowcount

        delete_live_session_if_empty(
            cursor,
            week_id,
            store_id,
            live_id
        )

        db.commit()

        return jsonify({
            "success": True,
            "deleted_rows": deleted_rows
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
            week_id=week_id,
            store_id=store_id,
            live_id=live_id
        )

        delete_live_session_if_empty(
            cursor,
            week_id,
            store_id,
            live_id
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


        sort_direction = (
            get_recording_sort_direction(cursor)
        )

        # Keep CSV in exactly the same Round / Seq direction as the UI.
        cursor.execute(
            f"""
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
                sr.round_no {sort_direction},
                sr.seq {sort_direction},
                sr.recording_id {sort_direction}
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
                f'="{row["sku"]}"',
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
