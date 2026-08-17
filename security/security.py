from flask import (
    Blueprint,
    render_template,
    request
)

from flask_login import (
    current_user,
    login_required
)

from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)

from db import get_db


security_bp = Blueprint(
    "security",
    __name__,
    template_folder="templates"
)


# =========================
# Change Password
# =========================

@security_bp.route(
    "/change-password",
    methods=[
        "GET",
        "POST"
    ]
)
@login_required
def change_password():

    error = None
    success = None

    if request.method == "POST":

        current_password = request.form.get(
            "current_password",
            ""
        )

        new_password = request.form.get(
            "new_password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not current_password:

            error = (
                "Please enter your current password."
            )

        elif not new_password:

            error = (
                "Please enter a new password."
            )

        elif len(new_password) < 8:

            error = (
                "New password must be at least "
                "8 characters."
            )

        elif new_password != confirm_password:

            error = (
                "New passwords do not match."
            )

        if error is None:

            db = get_db()

            cursor = db.cursor(
                dictionary=True
            )

            try:

                cursor.execute(
                    '''
                    SELECT
                        password_hash

                    FROM login_info

                    WHERE user_id = %s
                    ''',
                    (
                        current_user.id,
                    )
                )

                row = cursor.fetchone()

            finally:

                cursor.close()
                db.close()

            if row is None:

                error = (
                    "User account not found."
                )

            elif not check_password_hash(
                row["password_hash"],
                current_password
            ):

                error = (
                    "Current password is incorrect."
                )

        if error is None:

            new_password_hash = (
                generate_password_hash(
                    new_password
                )
            )

            db = get_db()

            cursor = db.cursor()

            try:

                cursor.execute(
                    '''
                    UPDATE login_info

                    SET password_hash = %s

                    WHERE user_id = %s
                    ''',
                    (
                        new_password_hash,
                        current_user.id
                    )
                )

                db.commit()

            finally:

                cursor.close()
                db.close()

            success = (
                "Password changed successfully."
            )

    return render_template(
        "change_password.html",
        error=error,
        success=success
    )
