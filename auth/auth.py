from urllib.parse import urljoin, urlparse

from flask import (
    Blueprint,
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
# Update Last Seen
# =========================

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
        update_last_seen
    )