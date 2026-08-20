from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for
)

from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user
)

from werkzeug.security import check_password_hash

from db import get_db

from permissions.permissions import(
    load_module_permissions
)


auth_bp = Blueprint(
    "auth",
    __name__,
    template_folder="templates"
)


login_manager = LoginManager()


# =========================
# User
# =========================

class User(UserMixin):

    def __init__(
        self,
        user_id,
        username,
        staff_id=None,
        is_active=True,
        module_permissions=None
    ):

        self.id = str(
            user_id
        )

        self.username = username

        self.staff_id = staff_id

        self.active = bool(
            is_active
        )

        self.module_permissions = set(
            module_permissions or []
        )

    @property
    def is_active(self):

        return self.active

    def has_access(
        self,
        module_id
    ):

        return (
            module_id
            in self.module_permissions
        )


# =========================
# Load User
# =========================

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
                staff_id,
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

    finally:

        cursor.close()
        db.close()


    module_permissions = (
        load_module_permissions(
            row["user_id"]
        )
    )


    return User(
        row["user_id"],
        row["username"],
        row["staff_id"],
        row["is_active"],
        module_permissions
    )



# =========================
# Session Policy
# =========================

SESSION_IDLE_DAYS = 7

SESSION_ABSOLUTE_DAYS = 30

SESSION_TIMEZONE = ZoneInfo(
    "America/New_York"
)

ABSOLUTE_TIMEOUT_EXEMPT_PATHS = {
    "/api/recordings/version",
}


def get_client_ip():

    cloudflare_ip = (
        request.headers
        .get(
            "CF-Connecting-IP",
            ""
        )
        .strip()
    )

    if cloudflare_ip:
        return cloudflare_ip[:45]

    forwarded_for = (
        request.headers
        .get(
            "X-Forwarded-For",
            ""
        )
        .split(",")[0]
        .strip()
    )

    if forwarded_for:
        return forwarded_for[:45]

    return (
        request.remote_addr
        or ""
    )[:45]


def enforce_absolute_session_timeout():

    if not current_user.is_authenticated:
        return None

    if request.path in ABSOLUTE_TIMEOUT_EXEMPT_PATHS:
        return None

    login_started_date = session.get(
        "login_started_date"
    )

    if not login_started_date:

        # Existing sessions created before this feature was added:
        # start their 30-day window from the first non-exempt request.
        session["login_started_date"] = (
            datetime.now(
                SESSION_TIMEZONE
            )
            .date()
            .isoformat()
        )

        return None

    try:

        started_date = (
            datetime.fromisoformat(
                login_started_date
            )
            .date()
        )

    except (
        TypeError,
        ValueError
    ):

        logout_user()
        session.clear()

        return redirect(
            url_for(
                "auth.login"
            )
        )

    today = (
        datetime.now(
            SESSION_TIMEZONE
        )
        .date()
    )

    if (
        today
        <
        started_date
        +
        timedelta(
            days=SESSION_ABSOLUTE_DAYS
        )
    ):

        return None

    logout_user()
    session.clear()

    if request.path.startswith(
        "/api/"
    ):

        return jsonify({
            "success": False,
            "message":
                "Login expired. Please log in again."
        }), 401

    return redirect(
        url_for(
            "auth.login"
        )
    )


# =========================
# Safe URL Check
# =========================

def is_safe_url(target):

    if not target:
        return False

    reference_url = urlparse(
        request.host_url
    )

    test_url = urlparse(
        urljoin(
            request.host_url,
            target
        )
    )

    return (
        test_url.scheme
        in (
            "http",
            "https"
        )
        and
        reference_url.netloc
        ==
        test_url.netloc
    )


# =========================
# Login Redirect
# =========================

def get_login_redirect():

    next_url = request.args.get(
        "next"
    )

    if (
        next_url
        and
        is_safe_url(
            next_url
        )
    ):

        return next_url

    return url_for(
        "index"
    )


# =========================
# Login
# =========================

@auth_bp.route(
    "/login",
    methods=[
        "GET",
        "POST"
    ]
)
def login():

    if current_user.is_authenticated:

        return redirect(
            get_login_redirect()
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
                    staff_id,
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

            module_permissions = (
                load_module_permissions(
                    row["user_id"]
                )
            )


            user = User(
                row["user_id"],
                row["username"],
                row["staff_id"],
                row["is_active"],
                module_permissions
            )


            login_user(
                user,
                remember=False
            )

            session.permanent = True

            session["login_started_date"] = (
                datetime.now(
                    SESSION_TIMEZONE
                )
                .date()
                .isoformat()
            )


            db = get_db()

            cursor = db.cursor()

            try:

                cursor.execute(
                    """
                    UPDATE login_info

                    SET
                        last_login_at = NOW(),
                        last_login_ip = %s

                    WHERE user_id = %s
                    """,
                    (
                        get_client_ip(),
                        row["user_id"],
                    )
                )

                db.commit()

            finally:

                cursor.close()
                db.close()


            return redirect(
                get_login_redirect()
            )


        error = (
            "Invalid username or password"
        )


    return render_template(
        "login.html",
        error=error
    )


# =========================
# Logout
# =========================

@auth_bp.route(
    "/logout"
)
@login_required
def logout():

    logout_user()

    session.clear()

    return redirect(
        url_for(
            "auth.login"
        )
    )


# =========================
# Init Auth
# =========================

def init_auth(app):

    app.config[
        "PERMANENT_SESSION_LIFETIME"
    ] = timedelta(
        days=SESSION_IDLE_DAYS
    )

    app.config[
        "SESSION_REFRESH_EACH_REQUEST"
    ] = True

    login_manager.init_app(
        app
    )

    login_manager.login_view = (
        "auth.login"
    )

    login_manager.login_message = (
        "Please log in first."
    )

    login_manager.user_loader(
        load_user
    )

    app.before_request(
        enforce_absolute_session_timeout
    )
