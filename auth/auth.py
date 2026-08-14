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


auth_bp = Blueprint(
    "auth",
    __name__,
    template_folder="templates"
)


login_manager = LoginManager()


class User(UserMixin):

    def __init__(
        self,
        user_id,
        username,
        is_active=True
    ):

        self.id = str(
            user_id
        )

        self.username = username

        self.active = bool(
            is_active
        )

    @property
    def is_active(self):

        return self.active


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
                get_login_redirect()
            )

        error = (
            "Invalid username or password"
        )

    return render_template(
        "login.html",
        error=error
    )


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