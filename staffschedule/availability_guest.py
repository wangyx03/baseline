import os
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
)
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeTimedSerializer,
)

from db import get_db


availability_guest_bp = Blueprint(
    "availability_guest",
    __name__,
    template_folder="templates",
)


GUEST_TOKEN_SALT = (
    "availability-guest-link"
)


# =========================================================
# Configuration
# =========================================================

def get_guest_link_expire_hours():
    """
    Guest link lifetime.

    .env:
        GUEST_LINK_EXPIRE_HOURS=24
    """

    try:

        hours = int(
            os.getenv(
                "GUEST_LINK_EXPIRE_HOURS",
                "24",
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        hours = 24

    return max(
        1,
        hours,
    )


def get_guest_link_max_age():
    """
    itsdangerous max_age uses seconds.
    """

    return (
        get_guest_link_expire_hours()
        *
        60
        *
        60
    )


# =========================================================
# Token
# =========================================================

def get_guest_serializer():

    return URLSafeTimedSerializer(
        current_app.config[
            "SECRET_KEY"
        ],
        salt=GUEST_TOKEN_SALT,
    )


def generate_guest_availability_token(
    week_start,
):
    """
    Generate one signed Guest Availability token.

    The token is bound to one availability week.
    Expiration is enforced when the token is loaded.

    week_start:
        date object or YYYY-MM-DD string
    """

    if hasattr(
        week_start,
        "isoformat",
    ):

        week_start_value = (
            week_start.isoformat()
        )

    else:

        week_start_value = str(
            week_start
            or ""
        ).strip()

    try:

        parsed_week_start = (
            datetime.strptime(
                week_start_value,
                "%Y-%m-%d",
            ).date()
        )

    except ValueError:

        raise ValueError(
            "week_start must use "
            "YYYY-MM-DD format."
        )

    return (
        get_guest_serializer()
        .dumps({
            "purpose":
                "guest_availability",

            "week_start":
                parsed_week_start
                .isoformat(),
        })
    )


def load_guest_availability_token(
    token,
):
    """
    Validate signature and the 24-hour-style max age
    configured through GUEST_LINK_EXPIRE_HOURS.

    Returns:
        {
            "week_start": date
        }

    Raises:
        SignatureExpired
        BadSignature
        ValueError
    """

    data = (
        get_guest_serializer()
        .loads(
            token,
            max_age=
                get_guest_link_max_age(),
        )
    )

    if (
        data.get(
            "purpose"
        )
        !=
        "guest_availability"
    ):

        raise BadSignature(
            "Invalid token purpose."
        )

    week_start_value = str(
        data.get(
            "week_start",
            "",
        )
    ).strip()

    try:

        week_start = (
            datetime.strptime(
                week_start_value,
                "%Y-%m-%d",
            ).date()
        )

    except ValueError:

        raise BadSignature(
            "Invalid week_start."
        )

    return {
        "week_start":
            week_start,
    }


# =========================================================
# Shared Validation
# =========================================================

def get_guest_staff(
    cursor,
    staff_id,
):

    cursor.execute(
        """
        SELECT
            staff_id,
            name

        FROM staff

        WHERE staff_id = %s
          AND active = TRUE
          AND guest = TRUE

        LIMIT 1
        """,
        (
            staff_id,
        )
    )

    return cursor.fetchone()


def get_guest_staff_list(
    cursor,
):

    cursor.execute(
        """
        SELECT
            staff_id,
            name

        FROM staff

        WHERE active = TRUE
          AND guest = TRUE

        ORDER BY
            name
        """
    )

    return cursor.fetchall()


def validate_guest_token_or_response(
    token,
):
    """
    Helper for routes.

    Returns:
        (token_data, None)
    or:
        (None, Flask response tuple)
    """

    try:

        token_data = (
            load_guest_availability_token(
                token
            )
        )

        return (
            token_data,
            None,
        )

    except SignatureExpired:

        return (
            None,
            (
                jsonify({
                    "success": False,
                    "message":
                        "This Guest Availability "
                        "link has expired.",
                    "expired": True,
                }),
                410,
            ),
        )

    except (
        BadSignature,
        ValueError,
    ):

        return (
            None,
            (
                jsonify({
                    "success": False,
                    "message":
                        "Invalid Guest Availability "
                        "link.",
                }),
                403,
            ),
        )


# =========================================================
# Guest Page
# =========================================================

@availability_guest_bp.route(
    "/guest-availability/<token>"
)
def guest_availability_page(
    token,
):

    try:

        token_data = (
            load_guest_availability_token(
                token
            )
        )

    except SignatureExpired:

        return render_template(
            "availability_guest.html",
            link_valid=False,
            link_expired=True,
            error_message=(
                "This Guest Availability "
                "link has expired."
            ),
            token="",
            staff_list=[],
            week_start="",
            expire_hours=
                get_guest_link_expire_hours(),
        ), 410

    except (
        BadSignature,
        ValueError,
    ):

        return render_template(
            "availability_guest.html",
            link_valid=False,
            link_expired=False,
            error_message=(
                "This Guest Availability "
                "link is invalid."
            ),
            token="",
            staff_list=[],
            week_start="",
            expire_hours=
                get_guest_link_expire_hours(),
        ), 403

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        staff_list = (
            get_guest_staff_list(
                cursor
            )
        )

        return render_template(
            "availability_guest.html",

            link_valid=True,
            link_expired=False,
            error_message="",

            token=token,

            staff_list=
                staff_list,

            week_start=(
                token_data[
                    "week_start"
                ].isoformat()
            ),

            expire_hours=
                get_guest_link_expire_hours(),
        )

    finally:

        cursor.close()
        db.close()


# =========================================================
# Guest Availability - GET
# =========================================================

@availability_guest_bp.route(
    "/api/guest-availability/<token>",
    methods=["GET"],
)
def get_guest_availability(
    token,
):

    (
        token_data,
        token_error,
    ) = (
        validate_guest_token_or_response(
            token
        )
    )

    if token_error:
        return token_error

    staff_id = request.args.get(
        "staff_id",
        type=int,
    )

    if not staff_id:

        return jsonify({
            "success": False,
            "message":
                "staff_id is required",
        }), 400

    week_start = (
        token_data[
            "week_start"
        ]
    )

    week_end = (
        week_start
        +
        timedelta(
            days=6
        )
    )

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        staff = (
            get_guest_staff(
                cursor,
                staff_id,
            )
        )

        if not staff:

            return jsonify({
                "success": False,
                "message":
                    "This staff member is not "
                    "currently enabled for "
                    "Guest Availability.",
            }), 403

        cursor.execute(
            """
            SELECT
                availability_id,
                staff_id,
                work_date,
                start_time,
                end_time,
                is_available

            FROM staff_availability

            WHERE staff_id = %s
              AND work_date
                    BETWEEN %s AND %s

            ORDER BY
                work_date,
                start_time
            """,
            (
                staff_id,
                week_start,
                week_end,
            )
        )

        rows = (
            cursor.fetchall()
        )

        cursor.execute(
            """
            SELECT preference
            FROM staff_availability_preference
            WHERE staff_id = %s
              AND week_start = %s
            LIMIT 1
            """,
            (staff_id, week_start)
        )

        preference_row = cursor.fetchone()
        preference = (
            str(preference_row.get("preference") or "")
            if preference_row
            else ""
        )

        availability = []

        for row in rows:

            availability.append({
                "availability_id":
                    row[
                        "availability_id"
                    ],

                "staff_id":
                    row[
                        "staff_id"
                    ],

                "work_date":
                    row[
                        "work_date"
                    ].isoformat(),

                "start_time":
                    str(
                        row[
                            "start_time"
                        ]
                    ),

                "end_time":
                    str(
                        row[
                            "end_time"
                        ]
                    ),

                "is_available":
                    bool(
                        row[
                            "is_available"
                        ]
                    ),
            })

        return jsonify({
            "success": True,

            "staff": {
                "staff_id":
                    staff[
                        "staff_id"
                    ],

                "name":
                    staff[
                        "name"
                    ],
            },

            "week_start":
                week_start
                .isoformat(),

            "week_end":
                week_end
                .isoformat(),

            "availability":
                availability,

            "preference":
                preference,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message":
                str(e),
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# Guest Availability - POST
# =========================================================

@availability_guest_bp.route(
    "/api/guest-availability/<token>",
    methods=["POST"],
)
def save_guest_availability(
    token,
):

    (
        token_data,
        token_error,
    ) = (
        validate_guest_token_or_response(
            token
        )
    )

    if token_error:
        return token_error

    data = request.get_json(
        silent=True
    ) or {}

    try:

        staff_id = int(
            data.get(
                "staff_id"
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid staff_id",
        }), 400

    availability = data.get(
        "availability",
        [],
    )

    preference = str(
        data.get(
            "preference",
            ""
        )
        or ""
    ).strip()

    if len(preference) > 500:

        return jsonify({
            "success": False,
            "message":
                "Preference cannot exceed 500 characters",
        }), 400

    if not isinstance(
        availability,
        list,
    ):

        return jsonify({
            "success": False,
            "message":
                "availability must be a list",
        }), 400

    week_start = (
        token_data[
            "week_start"
        ]
    )

    week_end = (
        week_start
        +
        timedelta(
            days=6
        )
    )

    normalized_items = []

    for item in availability:

        if not isinstance(
            item,
            dict,
        ):
            continue

        work_date_value = str(
            item.get(
                "work_date",
                "",
            )
        ).strip()

        start_time = str(
            item.get(
                "start_time",
                "",
            )
        ).strip()

        end_time = str(
            item.get(
                "end_time",
                "",
            )
        ).strip()

        try:

            work_date = (
                datetime.strptime(
                    work_date_value,
                    "%Y-%m-%d",
                ).date()
            )

        except ValueError:

            return jsonify({
                "success": False,
                "message":
                    "Invalid work_date.",
            }), 400

        if (
            work_date
            <
            week_start
            or
            work_date
            >
            week_end
        ):

            return jsonify({
                "success": False,
                "message":
                    "Availability date is "
                    "outside this Guest "
                    "link's week.",
            }), 400

        try:

            datetime.strptime(
                start_time,
                "%H:%M",
            )

            datetime.strptime(
                end_time,
                "%H:%M",
            )

        except ValueError:

            return jsonify({
                "success": False,
                "message":
                    "Invalid availability time.",
            }), 400

        if start_time >= end_time:

            return jsonify({
                "success": False,
                "message":
                    "start_time must be "
                    "earlier than end_time.",
            }), 400

        normalized_items.append({
            "work_date":
                work_date,

            "start_time":
                start_time,

            "end_time":
                end_time,

            "is_available":
                bool(
                    item.get(
                        "is_available"
                    )
                ),
        })

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        staff = (
            get_guest_staff(
                cursor,
                staff_id,
            )
        )

        if not staff:

            return jsonify({
                "success": False,
                "message":
                    "This staff member is not "
                    "currently enabled for "
                    "Guest Availability.",
            }), 403

        for item in normalized_items:

            cursor.execute(
                """
                INSERT INTO
                    staff_availability
                (
                    staff_id,
                    work_date,
                    start_time,
                    end_time,
                    is_available
                )

                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )

                ON DUPLICATE KEY UPDATE
                    is_available =
                        VALUES(
                            is_available
                        )
                """,
                (
                    staff_id,
                    item[
                        "work_date"
                    ],
                    item[
                        "start_time"
                    ],
                    item[
                        "end_time"
                    ],
                    item[
                        "is_available"
                    ],
                )
            )

        cursor.execute(
            """
            INSERT INTO staff_availability_preference
            (
                staff_id,
                week_start,
                preference
            )
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                preference = VALUES(preference)
            """,
            (
                staff_id,
                week_start,
                preference
            )
        )

        db.commit()

        return jsonify({
            "success": True,

            "message":
                "Availability saved.",

            "staff_id":
                staff_id,

            "staff_name":
                staff[
                    "name"
                ],

            "week_start":
                week_start
                .isoformat(),

            "week_end":
                week_end
                .isoformat(),
        })

    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e),
        }), 500

    finally:

        cursor.close()
        db.close()
