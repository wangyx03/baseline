from pathlib import Path
import os

from dotenv import load_dotenv

from flask import (
    Flask,
    render_template
)

from flask_login import (
    current_user,
    login_required
)

from auth.auth import (
    auth_bp,
    init_auth
)

from permissions.permissions import (
    MODULE_RECORDING,
    MODULE_WEEKLY_INVENTORY,
    MODULE_AVAILABILITY,
    MODULE_SCHEDULE_MANAGEMENT,
    MODULE_SCHEDULE,
    MODULE_PERMISSION_MANAGEMENT,
    MODULE_BOOK_SELECTION
)

from recording.recording import recording_bp
from recording.weekly_inventory import weekly_bp
from recording.recording_download import recording_download_bp

from staffschedule.availability import availability_bp
from staffschedule.availability_management import (
    availability_management_bp
)
from staffschedule.schedule import schedule_bp

from permissions.permission_management import (
    permission_management_bp
)

from security.security import security_bp

from bookselection.next_week_candidates import (
    next_week_candidates_bp
)

from bookselection.resident_books import resident_books_bp

from bookselection.book_selection_generate import (
    book_selection_generate_bp
)

from db import get_db


BASE_DIR = Path(
    __file__
).resolve().parent


load_dotenv(
    BASE_DIR / ".env"
)


app = Flask(
    __name__
)


app.config[
    "SECRET_KEY"
] = os.getenv(
    "SECRET_KEY"
)


app.config[
    "SESSION_PERMANENT"
] = False


# =========================
# 初始化登录
# =========================

init_auth(
    app
)


# =========================
# 注册功能模块
# =========================

app.register_blueprint(
    auth_bp
)

app.register_blueprint(
    recording_bp
)

app.register_blueprint(
    weekly_bp
)

app.register_blueprint(
    recording_download_bp
)

app.register_blueprint(
    availability_bp
)

app.register_blueprint(
    availability_management_bp
)

app.register_blueprint(
    schedule_bp
)

app.register_blueprint(
    permission_management_bp
)

app.register_blueprint(
    security_bp
)

app.register_blueprint(
    next_week_candidates_bp
)

app.register_blueprint(
    resident_books_bp
)

app.register_blueprint(
    book_selection_generate_bp
)


# =========================
# 系统首页
# =========================

@app.route("/")
@login_required
def index():

    last_schedule_update = None

    schedule_has_update = False

    current_staff_name = None


    # =========================
    # Current Staff
    # =========================

    if current_user.staff_id is not None:

        db = get_db()

        cursor = db.cursor(
            dictionary=True
        )

        try:

            cursor.execute(
                """
                SELECT
                    name

                FROM staff

                WHERE staff_id = %s
                  AND active = TRUE
                """,
                (
                    current_user.staff_id,
                )
            )

            staff_row = cursor.fetchone()

            if staff_row:

                current_staff_name = (
                    staff_row["name"]
                )

        finally:

            cursor.close()
            db.close()


    # =========================
    # Schedule Update
    # =========================

    if current_user.has_access(
        MODULE_SCHEDULE
    ):

        db = get_db()

        cursor = db.cursor(
            dictionary=True
        )

        try:

            # ---------------------------------
            # Latest published schedule
            # ---------------------------------

            cursor.execute(
                """
                SELECT
                    MAX(published_at)
                        AS last_published_at

                FROM staff_schedule

                WHERE status = 'published'
                """
            )

            row = cursor.fetchone()

            last_schedule_update = (
                row["last_published_at"]
                if row
                else None
            )


            # ---------------------------------
            # Current user's last seen time
            # ---------------------------------

            cursor.execute(
                """
                SELECT
                    last_seen_at

                FROM staff_schedule_notify

                WHERE user_id = %s

                LIMIT 1
                """,
                (
                    current_user.id,
                )
            )

            seen_row = cursor.fetchone()

            last_seen_at = (
                seen_row["last_seen_at"]
                if seen_row
                else None
            )


            # ---------------------------------
            # Has unread schedule update?
            # ---------------------------------

            if last_schedule_update is not None:

                if last_seen_at is None:

                    schedule_has_update = True

                elif (
                    last_schedule_update
                    >
                    last_seen_at
                ):

                    schedule_has_update = True


        finally:

            cursor.close()
            db.close()


    # =========================
    # Render Dashboard
    # =========================

    return render_template(
        "index.html",

        show_dashboard=False,

        last_schedule_update=
            last_schedule_update,

        schedule_has_update=
            schedule_has_update,

        current_staff_name=
            current_staff_name,

        MODULE_RECORDING=
            MODULE_RECORDING,

        MODULE_WEEKLY_INVENTORY=
            MODULE_WEEKLY_INVENTORY,

        MODULE_AVAILABILITY=
            MODULE_AVAILABILITY,

        MODULE_SCHEDULE_MANAGEMENT=
            MODULE_SCHEDULE_MANAGEMENT,

        MODULE_SCHEDULE=
            MODULE_SCHEDULE,

        MODULE_PERMISSION_MANAGEMENT=
            MODULE_PERMISSION_MANAGEMENT,

        MODULE_BOOK_SELECTION=
            MODULE_BOOK_SELECTION
    )


# =========================
# 启动
# =========================

if __name__ == "__main__":

    app.run(
        debug=True
    )