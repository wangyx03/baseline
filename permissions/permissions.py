from functools import wraps

from flask import abort

from flask_login import (
    current_user,
    login_required
)

from db import get_db


# =========================
# Module IDs
# =========================

MODULE_RECORDING = 1

MODULE_WEEKLY_INVENTORY = 2

MODULE_AVAILABILITY = 3

MODULE_SCHEDULE_MANAGEMENT = 4

MODULE_SCHEDULE = 5

MODULE_PERMISSION_MANAGEMENT = 6

MODULE_BOOK_SELECTION = 7


# =========================
# Load User Permissions
# =========================

def load_module_permissions(user_id):

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                mp.module_id

            FROM module_permissions mp

            JOIN modules m
                ON m.module_id =
                    mp.module_id

            WHERE mp.user_id = %s
              AND mp.access = TRUE
              AND m.is_active = TRUE
            """,
            (
                user_id,
            )
        )

        rows = cursor.fetchall()

        return {
            row["module_id"]
            for row in rows
        }

    finally:

        cursor.close()
        db.close()


# =========================
# Has Module
# =========================

def has_module(module_id):

    if not current_user.is_authenticated:
        return False

    return current_user.has_access(
        module_id
    )


# =========================
# Module Required
# =========================

def module_required(module_id):

    def decorator(function):

        @wraps(function)
        @login_required
        def wrapped_function(
            *args,
            **kwargs
        ):

            if not current_user.has_access(
                module_id
            ):
                abort(403)

            return function(
                *args,
                **kwargs
            )

        return wrapped_function

    return decorator