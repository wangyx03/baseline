from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from db import get_db
from permissions.permissions import(
    module_required,
    MODULE_SCHEDULE_MANAGEMENT
)

from .availability_guest import (
    generate_guest_availability_token,
    get_guest_link_expire_hours,
)

from .work_hours import (
    editable_weeks,
    load_week_hours,
    monday_for,
    validate_range,
)

availability_management_bp = Blueprint(
    "availability_management",
    __name__,
    template_folder="templates"
)

@availability_management_bp.before_request
@module_required(MODULE_SCHEDULE_MANAGEMENT)
def require_availability_management_access():
    pass

@availability_management_bp.route(
    "/availability-management"
)
@login_required
def availability_management_page():

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                staff_id,
                name,
                guest

            FROM staff

            WHERE active = TRUE

            ORDER BY name
            """
        )

        staff_list = cursor.fetchall()

        return render_template(
            "availability_management.html",
            staff_list=staff_list
        )

    finally:

        cursor.close()
        db.close()

@availability_management_bp.route(
    "/api/availability-management/staff-guest",
    methods=["POST"]
)
@login_required
def update_staff_guest():

    data = request.get_json(silent=True) or {}

    try:
        staff_id = int(data.get("staff_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Invalid staff_id"
        }), 400

    guest = 1 if bool(data.get("guest")) else 0

    db = get_db()
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            UPDATE staff
            SET guest = %s
            WHERE staff_id = %s
              AND active = TRUE
            """,
            (guest, staff_id)
        )

        if cursor.rowcount == 0:
            return jsonify({
                "success": False,
                "message": "Active staff member not found"
            }), 404

        db.commit()

        return jsonify({
            "success": True,
            "staff_id": staff_id,
            "guest": bool(guest)
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


# =========================
# Work Hours - GET
# =========================

def work_hours_payload(cursor, monday):

    this_monday, next_monday = editable_weeks()

    payload = load_week_hours(cursor, monday)

    payload["editable"] = monday in (this_monday, next_monday)

    return payload


@availability_management_bp.route(
    "/api/availability-management/work-hours",
    methods=["GET"]
)
@login_required
def get_work_hours():

    try:
        monday = monday_for(
            request.args.get("week_start", "")
        )
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Invalid week_start"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        return jsonify({
            "success": True,
            "work_hours": work_hours_payload(cursor, monday)
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        cursor.close()
        db.close()


# =========================
# Work Hours - SAVE
#
# Body: {"week_start": "2026-11-09", "start_hour": 10, "end_hour": 22}
# Only the current week and next week can be changed.
# =========================

@availability_management_bp.route(
    "/api/availability-management/work-hours",
    methods=["POST"]
)
@login_required
def save_work_hours():

    data = request.get_json(silent=True) or {}

    try:
        monday = monday_for(data.get("week_start", ""))
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Invalid week_start"
        }), 400

    if monday not in editable_weeks():
        return jsonify({
            "success": False,
            "message":
                "Work hours can only be changed for "
                "this week or next week."
        }), 400

    try:
        start_hour, end_hour = validate_range(
            data.get("start_hour"),
            data.get("end_hour")
        )
    except ValueError as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            INSERT INTO staff_work_hours
                (week_start, start_hour, end_hour, updated_by)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                start_hour = VALUES(start_hour),
                end_hour   = VALUES(end_hour),
                updated_by = VALUES(updated_by)
            """,
            (
                monday,
                start_hour,
                end_hour,
                getattr(current_user, "id", None)
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "message": "Work hours updated.",
            "work_hours": work_hours_payload(cursor, monday)
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


# =========================
# Generate Guest Link
# =========================

@availability_management_bp.route(
    "/api/availability-management/generate-guest-link",
    methods=["POST"]
)
@login_required
def generate_guest_link():

    data = request.get_json(
        silent=True
    ) or {}

    week_start = str(
        data.get(
            "week_start",
            ""
        )
    ).strip()

    if not week_start:

        return jsonify({
            "success": False,
            "message": "week_start is required"
        }), 400

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                COUNT(*) AS guest_count

            FROM staff

            WHERE active = TRUE
              AND guest = TRUE
            """
        )

        row = cursor.fetchone()

        guest_count = int(
            row["guest_count"]
            if row
            else 0
        )

        if guest_count <= 0:

            return jsonify({
                "success": False,
                "message":
                    "Select at least one Guest Staff "
                    "before generating a link."
            }), 400

        try:

            token = (
                generate_guest_availability_token(
                    week_start
                )
            )

        except ValueError as e:

            return jsonify({
                "success": False,
                "message": str(e)
            }), 400

        guest_url = url_for(
            "availability_guest.guest_availability_page",
            token=token,
            _external=True
        )

        return jsonify({
            "success": True,
            "url": guest_url,
            "week_start": week_start,
            "guest_count": guest_count,
            "expire_hours":
                get_guest_link_expire_hours()
        })

    finally:

        cursor.close()
        db.close()
