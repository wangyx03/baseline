from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    Response,
    redirect,
    url_for,
    session
)

from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user
)

from werkzeug.security import check_password_hash
from dotenv import load_dotenv

import mysql.connector
import os
import csv
import io
import math

from pathlib import Path
from zoneinfo import ZoneInfo


# =========================================================
# Flask
# =========================================================

app = Flask(__name__)


# =========================================================
# Environment
# =========================================================

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(
    BASE_DIR / ".env"
)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY"
)

# Browser-session cookie only.
# Do not create a permanent login session.
app.config["SESSION_PERMANENT"] = False


# =========================================================
# Time Zone
#
# Database = UTC
# Web / CSV = Eastern Time
# =========================================================

UTC_ZONE = ZoneInfo("UTC")
EASTERN_ZONE = ZoneInfo("America/New_York")


def format_et(dt):

    if dt is None:
        return ""

    utc_time = dt.replace(
        tzinfo=UTC_ZONE
    )

    eastern_time = utc_time.astimezone(
        EASTERN_ZONE
    )

    return (
        eastern_time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        + " ET"
    )


# =========================================================
# Flask Login
# =========================================================

login_manager = LoginManager()

login_manager.init_app(app)

login_manager.login_view = "login"

login_manager.login_message = (
    "Please log in first."
)


# =========================================================
# Database
# =========================================================

def get_db():

    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),

        port=int(
            os.getenv(
                "DB_PORT",
                "3306"
            )
        ),

        user=os.getenv("DB_USER"),

        password=os.getenv(
            "DB_PASSWORD"
        ),

        database=os.getenv(
            "DB_NAME"
        )
    )

    cursor = db.cursor()

    cursor.execute(
        "SET time_zone = '+00:00'"
    )

    cursor.close()

    return db


# =========================================================
# User
# =========================================================

class User(UserMixin):

    def __init__(
        self,
        user_id,
        username,
        is_active=True
    ):

        self.id = str(user_id)

        self.username = username

        self.active = bool(
            is_active
        )

    @property
    def is_active(self):

        return self.active


# =========================================================
# User Loader
# =========================================================

@login_manager.user_loader
def load_user(user_id):

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                user_id,
                username,
                is_active

            FROM login_info

            WHERE user_id = %s
            """,
            (
                user_id,
            )
        )

        row = cursor.fetchone()

        if row is None:
            return None

        if not row["is_active"]:
            return None

        return User(
            row["user_id"],
            row["username"],
            row["is_active"]
        )

    finally:

        cursor.close()
        db.close()


# =========================================================
# Recording Audit Log
# =========================================================

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


# =========================================================
# Weekly SKU Status
#
# Locked LIVE:
# use inventory_locked
#
# Unlocked LIVE:
# use sku_recording
# =========================================================

def get_weekly_item_status(
    cursor,
    week_id,
    store_id,
    sku
):

    cursor.execute(
        """
        SELECT
            wi.planned_qty AS planned,

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
            ) AS used

        FROM weekly_inventory wi

        LEFT JOIN (
            SELECT
                week_id,
                store_id,
                sku,
                SUM(quantity) AS locked_used

            FROM inventory_locked

            GROUP BY
                week_id,
                store_id,
                sku

        ) locked_data

            ON locked_data.week_id = wi.week_id
            AND locked_data.store_id = wi.store_id
            AND locked_data.sku = wi.sku

        LEFT JOIN (
            SELECT
                sr.week_id,
                sr.store_id,
                sr.sku,
                SUM(sr.quantity) AS draft_used

            FROM sku_recording sr

            WHERE NOT EXISTS (
                SELECT 1

                FROM inventory_locked il2

                WHERE il2.week_id = sr.week_id
                  AND il2.store_id = sr.store_id
                  AND il2.live_id = sr.live_id
            )

            GROUP BY
                sr.week_id,
                sr.store_id,
                sr.sku

        ) draft_data

            ON draft_data.week_id = wi.week_id
            AND draft_data.store_id = wi.store_id
            AND draft_data.sku = wi.sku

        WHERE wi.week_id = %s
          AND wi.store_id = %s
          AND wi.sku = %s

        LIMIT 1
        """,
        (
            week_id,
            store_id,
            sku
        )
    )

    row = cursor.fetchone()

    if row is None:

        return {
            "in_weekly_inventory": False,
            "planned": 0,
            "used": 0,
            "remaining": 0
        }

    planned = int(
        row["planned"]
        or 0
    )

    used = int(
        row["used"]
        or 0
    )

    return {
        "in_weekly_inventory": True,
        "planned": planned,
        "used": used,
        "remaining": planned - used
    }


# =========================================================
# Last Seen
# =========================================================

@app.before_request
def update_last_seen():

    if not current_user.is_authenticated:
        return

    db = get_db()

    cursor = db.cursor()

    try:

        cursor.execute(
            """
            UPDATE login_info

            SET last_seen_at = NOW()

            WHERE user_id = %s
            """,
            (
                current_user.id,
            )
        )

        db.commit()

    finally:

        cursor.close()
        db.close()


# =========================================================
# Login
# =========================================================

@app.route(
    "/login",
    methods=[
        "GET",
        "POST"
    ]
)
def login():

    if current_user.is_authenticated:

        return redirect(
            url_for(
                "recording",
                store_id=1
            )
        )

    error = None

    if request.method == "POST":

        username = (
            request.form
            .get(
                "username",
                ""
            )
            .strip()
        )

        password = request.form.get(
            "password",
            ""
        )

        db = get_db()

        cursor = db.cursor(
            dictionary=True
        )

        try:

            cursor.execute(
                """
                SELECT
                    user_id,
                    username,
                    password_hash,
                    is_active

                FROM login_info

                WHERE username = %s
                """,
                (
                    username,
                )
            )

            row = cursor.fetchone()

        finally:

            cursor.close()
            db.close()

        if (
            row
            and row["is_active"]
            and check_password_hash(
                row["password_hash"],
                password
            )
        ):

            user = User(
                row["user_id"],
                row["username"],
                row["is_active"]
            )

            login_user(
                user,
                remember=False
            )

            session.permanent = False

            db = get_db()

            cursor = db.cursor()

            try:

                cursor.execute(
                    """
                    UPDATE login_info

                    SET
                        last_login_at = NOW(),
                        last_seen_at = NOW()

                    WHERE user_id = %s
                    """,
                    (
                        row["user_id"],
                    )
                )

                db.commit()

            finally:

                cursor.close()
                db.close()

            return redirect(
                url_for(
                    "recording",
                    store_id=1
                )
            )

        error = (
            "Invalid username or password"
        )

    return render_template(
        "login.html",
        error=error
    )


# =========================================================
# Logout
# =========================================================

@app.route("/logout")
@login_required
def logout():

    logout_user()

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# Recording Page
# =========================================================

@app.route(
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


# =========================================================
# Session Info
# =========================================================

@app.route(
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


# =========================================================
# Add Recording
# =========================================================

@app.route(
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


# =========================================================
# Insert Before
# =========================================================

@app.route(
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


# =========================================================
# Get Recordings
# =========================================================

@app.route(
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


# =========================================================
# Update Recording
# =========================================================

@app.route(
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


# =========================================================
# Delete Recording
# =========================================================

@app.route(
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


# =========================================================
# Download CSV
# =========================================================

@app.route(
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


# =========================================================
# Weekly Inventory Page
# =========================================================

@app.route(
    "/weekly-inventory"
)
@login_required
def weekly_inventory_page():

    return render_template(
        "weekly_inventory.html"
    )


# =========================================================
# Week List
# =========================================================

@app.route(
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


# =========================================================
# Weekly Inventory
# =========================================================

@app.route(
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


# =========================================================
# Start Flask
# =========================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )