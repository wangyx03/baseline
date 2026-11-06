from datetime import date, datetime, time, timedelta

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request
)
from flask_login import (
    current_user,
    login_required
)
from permissions.permissions import(
    module_required,
    MODULE_SCHEDULE,
    MODULE_SCHEDULE_MANAGEMENT
)
from db import get_db

from .work_hours import load_week_hours


schedule_bp = Blueprint(
    "schedule",
    __name__,
    template_folder="templates"
)

@schedule_bp.before_request
@module_required(MODULE_SCHEDULE)
def require_schedule_access():
    pass


# =========================================================
# Schedule Edit Permission
# =========================================================

def can_edit_schedule():

    return current_user.has_access(
        MODULE_SCHEDULE_MANAGEMENT
    )


def require_schedule_edit_access():

    if can_edit_schedule():
        return None

    return jsonify({
        "success": False,
        "message":
            "You do not have permission to modify the schedule."
    }), 403


# =========================================================
# Settings
# =========================================================

VALID_ROLES = {
    "creator",
    "operator"
}

VALID_STATUSES = {
    "draft",
    "published"
}

# Positions per store / hour.
POSITIONS_PER_ROLE = {
    "creator": 1,
    "operator": 2
}

# Max continuous hours in one role before an override is needed.
# None = no limit. Continuous = shifts of that role touching end-to-start,
# any store, any position.
MAX_CONTINUOUS_HOURS = {
    "operator": 3,
    "creator": None
}

OVERRIDE_DAILY_LIMIT = "daily_hour_limit"
OVERRIDE_CONSECUTIVE = "consecutive_operator"   # column: override_consecutive

DAY_MINUTES = 24 * 60


# =========================================================
# Time helpers
#
# Shifts are stored as staff_schedule.start_time / end_time (TIME).
# Internally everything is minutes from midnight of work_date.
# end_time 00:00 means midnight (= 1440).
# =========================================================

def parse_date(value):

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return datetime.strptime(
        str(value).strip(),
        "%Y-%m-%d"
    ).date()


def monday_for(value):

    work_date = parse_date(value)

    return work_date - timedelta(days=work_date.weekday())


def week_bounds(value):

    monday = monday_for(value)

    return (
        monday,
        monday + timedelta(days=6)
    )


def serialize_datetime(value):

    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat(
            sep=" ",
            timespec="seconds"
        )

    return str(value)


def to_minutes(value):
    """MySQL TIME (timedelta) / time / 'HH:MM[:SS]' -> minutes."""

    if isinstance(value, timedelta):
        return int(value.total_seconds()) // 60

    if isinstance(value, time):
        return value.hour * 60 + value.minute

    parts = str(value).strip().split(":")

    if len(parts) < 2:
        raise ValueError("Invalid time.")

    hour = int(parts[0])
    minute = int(parts[1])

    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
        raise ValueError("Invalid time.")

    return hour * 60 + minute


def minutes_range(start_value, end_value):
    """(start, end) in minutes; an end at/before start means next midnight."""

    start = to_minutes(start_value) % DAY_MINUTES
    end = to_minutes(end_value)

    if end <= start:
        end += DAY_MINUTES

    return start, end


def minutes_to_hhmm(minutes):

    minutes = int(minutes) % DAY_MINUTES

    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes_to_sql(minutes):

    return minutes_to_hhmm(minutes) + ":00"


def parse_shift_times(start_value, end_value):
    """Whole hours, inside one day, end may be 00:00 / 24:00 (midnight)."""

    try:
        start, end = minutes_range(start_value, end_value)
    except (TypeError, ValueError):
        raise ValueError("Invalid start_time / end_time.")

    if start % 60 or end % 60:
        raise ValueError("Shifts must start and end on the hour.")

    if end > DAY_MINUTES:
        raise ValueError("A shift cannot go past midnight.")

    return start, end


def ranges_overlap(start_a, end_a, start_b, end_b):

    return start_a < end_b and end_a > start_b


def merge_ranges(ranges):
    """Merge overlapping or touching ranges."""

    merged = []

    for start, end in sorted(ranges):

        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [tuple(item) for item in merged]


def fully_covered(target_start, target_end, available_ranges):

    return any(
        start <= target_start and end >= target_end
        for start, end in merge_ranges(available_ranges)
    )


def continuous_minutes(shift_start, shift_end, other_ranges):
    """Length of the continuous run that contains the new shift."""

    for start, end in merge_ranges(
        list(other_ranges) + [(shift_start, shift_end)]
    ):
        if start <= shift_start and end >= shift_end:
            return end - start

    return shift_end - shift_start


def format_hours(minutes):

    hours = minutes / 60

    return f"{hours:g}"


# =========================================================
# Loaders
# =========================================================

def get_week_status(
    cursor,
    week_start
):

    monday, week_end = week_bounds(
        week_start
    )

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(
                CASE
                    WHEN status = 'draft'
                    THEN 1
                    ELSE 0
                END
            ) AS draft_count,
            MAX(published_at) AS published_at

        FROM staff_schedule

        WHERE work_date
            BETWEEN %s AND %s
        """,
        (
            monday,
            week_end
        )
    )

    row = cursor.fetchone() or {}

    total_count = int(row.get("total_count") or 0)
    draft_count = int(row.get("draft_count") or 0)

    status = (
        "published"
        if total_count > 0 and draft_count == 0
        else "draft"
    )

    return {
        "status": status,
        "total_count": total_count,
        "draft_count": draft_count,
        "published_at": serialize_datetime(row.get("published_at"))
    }


def load_role_staff(cursor, role_type, staff_id=None):
    """Active staff with this live role."""

    sql = """
        SELECT DISTINCT
            st.staff_id,
            st.name,
            st.daily_hour_limit

        FROM staff st

        JOIN staff_live_roles slr
            ON slr.staff_id = st.staff_id

        WHERE st.active = TRUE
          AND slr.role_type = %s
    """

    params = [role_type]

    if staff_id is not None:
        sql += " AND st.staff_id = %s"
        params.append(staff_id)

    sql += " ORDER BY st.staff_id"

    cursor.execute(sql, tuple(params))

    return cursor.fetchall()


def load_available_ranges(cursor, staff_ids, work_date):
    """{staff_id: [(start_min, end_min), ...]}"""

    if not staff_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(staff_ids))

    cursor.execute(
        f"""
        SELECT
            staff_id,
            start_time,
            end_time

        FROM staff_availability

        WHERE work_date = %s
          AND is_available = TRUE
          AND staff_id IN ({placeholders})
        """,
        tuple([work_date] + list(staff_ids))
    )

    result = {}

    for row in cursor.fetchall():
        result.setdefault(row["staff_id"], []).append(
            minutes_range(row["start_time"], row["end_time"])
        )

    return result


def load_day_assignments(
    cursor,
    staff_ids,
    work_date,
    exclude_schedule_id=None
):
    """{staff_id: [assignment dict with start_min / end_min], ...}"""

    if not staff_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(staff_ids))

    sql = f"""
        SELECT
            schedule_id,
            staff_id,
            store_id,
            role_type,
            position_no,
            start_time,
            end_time

        FROM staff_schedule

        WHERE work_date = %s
          AND staff_id IN ({placeholders})
    """

    params = [work_date] + list(staff_ids)

    if exclude_schedule_id is not None:
        sql += " AND schedule_id <> %s"
        params.append(exclude_schedule_id)

    cursor.execute(sql, tuple(params))

    result = {}

    for row in cursor.fetchall():

        row["start_min"], row["end_min"] = minutes_range(
            row["start_time"],
            row["end_time"]
        )

        result.setdefault(row["staff_id"], []).append(row)

    return result


def load_position_rows(cursor, shift, for_update=False):
    """Every shift in this date / store / role / position."""

    sql = """
        SELECT
            ss.schedule_id,
            ss.staff_id,
            st.name AS staff_name,
            ss.start_time,
            ss.end_time,
            ss.override_daily_limit,
            ss.override_consecutive,
            ss.overridden_at

        FROM staff_schedule ss

        JOIN staff st
            ON st.staff_id = ss.staff_id

        WHERE ss.work_date = %s
          AND ss.store_id = %s
          AND ss.role_type = %s
          AND ss.position_no = %s

        ORDER BY ss.start_time
    """

    if for_update:
        sql += " FOR UPDATE"

    cursor.execute(
        sql,
        (
            shift["work_date"],
            shift["store_id"],
            shift["role_type"],
            shift["position_no"]
        )
    )

    rows = cursor.fetchall()

    for row in rows:
        row["start_min"], row["end_min"] = minutes_range(
            row["start_time"],
            row["end_time"]
        )

    return rows


# =========================================================
# Rules
# =========================================================

def evaluate_staff(
    staff,
    role_type,
    shift_start,
    shift_end,
    available_ranges,
    assignments
):
    """
    Hard rules (cannot override):
        - availability covers the whole shift
        - no overlap with the staff member's other shifts (any store/role)

    Soft rules (override):
        - daily hour limit
        - continuous hours in this role (MAX_CONTINUOUS_HOURS)
    """

    if not fully_covered(shift_start, shift_end, available_ranges):
        return {
            "eligible": False,
            "reason": "Availability does not fully cover this time."
        }

    for assignment in assignments:
        if ranges_overlap(
            shift_start,
            shift_end,
            assignment["start_min"],
            assignment["end_min"]
        ):
            return {
                "eligible": False,
                "reason": "Staff already has an overlapping assignment."
            }

    violations = []
    messages = []

    scheduled_minutes = sum(
        a["end_min"] - a["start_min"]
        for a in assignments
    )

    shift_minutes = shift_end - shift_start

    daily_limit = float(staff["daily_hour_limit"])

    total_hours_after = (scheduled_minutes + shift_minutes) / 60

    if total_hours_after > daily_limit + 1e-9:
        violations.append(OVERRIDE_DAILY_LIMIT)
        messages.append(
            f"Daily hour limit would be exceeded "
            f"({total_hours_after:g}h / {daily_limit:g}h)."
        )

    run_minutes = continuous_minutes(
        shift_start,
        shift_end,
        [
            (a["start_min"], a["end_min"])
            for a in assignments
            if a["role_type"] == role_type
        ]
    )

    limit = MAX_CONTINUOUS_HOURS.get(role_type)

    if limit is not None and run_minutes > limit * 60:
        violations.append(OVERRIDE_CONSECUTIVE)
        messages.append(
            f"{role_type.title()} would work "
            f"{format_hours(run_minutes)} hours in a row "
            f"(limit {limit}h)."
        )

    return {
        "eligible": True,
        "requires_override": bool(violations),
        "violations": violations,
        "violation_messages": messages,
        "staff_id": staff["staff_id"],
        "name": staff["name"],
        "daily_hour_limit": daily_limit,
        "scheduled_hours": scheduled_minutes / 60,
        "shift_hours": shift_minutes / 60,
        "total_hours_after": total_hours_after,
        "continuous_hours": run_minutes / 60
    }


# =========================================================
# Request parsing (candidates + save)
# =========================================================

def parse_shift_request(source):
    """
    Returns (shift, None) or (None, error_response).

    shift = {
        work_date, store_id, role_type, position_no,
        start_min, end_min, schedule_id (None for a new shift)
    }
    """

    def error(message):
        return None, (jsonify({"success": False, "message": message}), 400)

    try:
        work_date = parse_date(source.get("work_date", ""))
    except (TypeError, ValueError):
        return error("Invalid work_date")

    try:
        store_id = int(source.get("store_id"))
    except (TypeError, ValueError):
        return error("Invalid store_id")

    role_type = str(source.get("role_type", "")).strip().lower()

    if role_type not in VALID_ROLES:
        return error("Invalid role_type")

    try:
        position_no = int(source.get("position_no", 1))
    except (TypeError, ValueError):
        return error("Invalid position_no")

    if not 1 <= position_no <= POSITIONS_PER_ROLE[role_type]:
        return error("Invalid position_no")

    try:
        start_min, end_min = parse_shift_times(
            source.get("start_time"),
            source.get("end_time")
        )
    except ValueError as e:
        return error(str(e))

    schedule_id = source.get("schedule_id")

    if schedule_id in (None, "", "null"):
        schedule_id = None
    else:
        try:
            schedule_id = int(schedule_id)
        except (TypeError, ValueError):
            return error("Invalid schedule_id")

    return {
        "work_date": work_date,
        "store_id": store_id,
        "role_type": role_type,
        "position_no": position_no,
        "start_min": start_min,
        "end_min": end_min,
        "schedule_id": schedule_id
    }, None


def published_week_response():

    return jsonify({
        "success": False,
        "message":
            "Published schedule must be changed to Modify mode before editing."
    }), 409


def conflict_payload(rows):

    return [
        {
            "schedule_id": row["schedule_id"],
            "staff_id": row["staff_id"],
            "staff_name": row["staff_name"],
            "start_time": minutes_to_hhmm(row["start_min"]),
            "end_time": minutes_to_hhmm(row["end_min"])
        }
        for row in rows
    ]


# =========================================================
# Schedule Page
# =========================================================

@schedule_bp.route(
    "/schedule"
)
@login_required
def schedule_page():

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        # Mark Schedule notification as seen
        cursor.execute(
            """
            INSERT INTO staff_schedule_notify
            (
                user_id,
                last_seen_at
            )

            VALUES
            (
                %s,
                %s
            )

            ON DUPLICATE KEY UPDATE

                last_seen_at =
                    VALUES(last_seen_at)
            """,
            (
                current_user.id,
                datetime.now()
            )
        )

        db.commit()

    except Exception:

        db.rollback()

        raise

    finally:

        cursor.close()
        db.close()


    return render_template(
        "schedule.html",
        can_edit_schedule=can_edit_schedule()
    )


# =========================================================
# Get Week
# =========================================================

@schedule_bp.route(
    "/api/schedule-week",
    methods=["GET"]
)
@login_required
def get_schedule_week():

    try:
        monday, week_end = week_bounds(
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

        cursor.execute(
            """
            SELECT
                store_id,
                store_name,
                short_name

            FROM stores

            ORDER BY store_id
            """
        )

        stores = [
            {
                "store_id": row["store_id"],
                "store_name": row["store_name"],
                "store_code": row["short_name"]
            }
            for row in cursor.fetchall()
        ]


        cursor.execute(
            """
            SELECT
                ss.schedule_id,
                ss.work_date,
                ss.store_id,
                ss.start_time,
                ss.end_time,
                ss.staff_id,
                ss.role_type,
                ss.position_no,
                ss.status,
                ss.override_daily_limit,
                ss.override_consecutive,
                ss.overridden_at,
                ss.published_at,

                st.name AS staff_name

            FROM staff_schedule ss

            JOIN staff st
                ON st.staff_id = ss.staff_id

            WHERE ss.work_date BETWEEN %s AND %s

            ORDER BY
                ss.work_date,
                ss.store_id,
                ss.role_type,
                ss.position_no,
                ss.start_time
            """,
            (
                monday,
                week_end
            )
        )

        assignments_by_date = {}

        for row in cursor.fetchall():

            start_min, end_min = minutes_range(
                row["start_time"],
                row["end_time"]
            )

            assignments_by_date.setdefault(
                row["work_date"].isoformat(),
                []
            ).append({
                "schedule_id": row["schedule_id"],
                "store_id": row["store_id"],
                "staff_id": row["staff_id"],
                "staff_name": row["staff_name"],
                "role_type": row["role_type"],
                "position_no": row["position_no"],
                "start_time": minutes_to_hhmm(start_min),
                "end_time": minutes_to_hhmm(end_min),
                "status": row["status"],
                "override_daily_limit": bool(row["override_daily_limit"]),
                "override_consecutive": bool(row["override_consecutive"]),
                "overridden_at": serialize_datetime(row["overridden_at"]),
                "published_at": serialize_datetime(row["published_at"])
            })


        days = []

        for offset in range(7):

            work_date = monday + timedelta(days=offset)
            key = work_date.isoformat()

            days.append({
                "work_date": key,
                "day_name": work_date.strftime("%A"),
                "assignments": assignments_by_date.get(key, [])
            })


        week_state = get_week_status(cursor, monday)

        return jsonify({
            "success": True,
            "week_start": monday.isoformat(),
            "week_end": week_end.isoformat(),
            "week_status": week_state["status"],
            "published_at": week_state["published_at"],
            "assignment_count": week_state["total_count"],
            "work_hours": load_week_hours(cursor, monday),
            "stores": stores,
            "positions": POSITIONS_PER_ROLE,
            "days": days
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# Get Candidates
#
# GET /api/schedule-candidates
#     ?work_date=2026-11-09&store_id=1&role_type=operator&position_no=1
#     &start_time=11:00&end_time=14:00
#     [&schedule_id=123]   editing an existing shift
# =========================================================

@schedule_bp.route(
    "/api/schedule-candidates",
    methods=["GET"]
)
@login_required
def get_schedule_candidates():

    shift, error = parse_shift_request(request.args)

    if error:
        return error

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        if get_week_status(cursor, shift["work_date"])["status"] == "published":
            return published_week_response()


        position_rows = load_position_rows(cursor, shift)

        current = next(
            (
                row for row in position_rows
                if row["schedule_id"] == shift["schedule_id"]
            ),
            None
        )

        if shift["schedule_id"] is not None and current is None:
            return jsonify({
                "success": False,
                "message": "This shift no longer exists. Reload the page."
            }), 404

        conflicts = [
            row for row in position_rows
            if row["schedule_id"] != shift["schedule_id"]
            and ranges_overlap(
                shift["start_min"], shift["end_min"],
                row["start_min"], row["end_min"]
            )
        ]


        staff_rows = load_role_staff(cursor, shift["role_type"])
        staff_ids = [row["staff_id"] for row in staff_rows]

        available_map = load_available_ranges(
            cursor, staff_ids, shift["work_date"]
        )

        assignment_map = load_day_assignments(
            cursor, staff_ids, shift["work_date"],
            exclude_schedule_id=shift["schedule_id"]
        )

        candidates = []

        for staff in staff_rows:

            result = evaluate_staff(
                staff,
                shift["role_type"],
                shift["start_min"],
                shift["end_min"],
                available_map.get(staff["staff_id"], []),
                assignment_map.get(staff["staff_id"], [])
            )

            if not result["eligible"]:
                continue

            result["is_current"] = bool(
                current and current["staff_id"] == staff["staff_id"]
            )

            candidates.append(result)


        candidates.sort(
            key=lambda item: (
                not item["is_current"],
                item["requires_override"],
                item["scheduled_hours"],
                item["name"].lower()
            )
        )

        return jsonify({
            "success": True,
            "shift_hours": (shift["end_min"] - shift["start_min"]) / 60,
            "position_conflicts": conflict_payload(conflicts),
            "candidates": candidates
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# Save Shift
#
# POST /api/staff-schedule
# {
#   work_date, store_id, role_type, position_no,
#   start_time, end_time, staff_id,
#   schedule_id   (optional: edit this shift)
#   override      (soft rules confirmed)
#   replace       (overlapping shifts in this position may be trimmed)
# }
#
# Afterwards, touching shifts of the same staff / store / role /
# position are merged into one row (14-16 + 16-18 -> 14-18).
# =========================================================

@schedule_bp.route(
    "/api/staff-schedule",
    methods=["POST"]
)
@login_required
def save_staff_schedule():

    permission_error = require_schedule_edit_access()

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    shift, error = parse_shift_request(data)

    if error:
        return error

    try:
        staff_id = int(data.get("staff_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Invalid staff_id"
        }), 400

    override_requested = bool(data.get("override", False))
    replace_requested = bool(data.get("replace", False))

    start_min = shift["start_min"]
    end_min = shift["end_min"]
    schedule_id = shift["schedule_id"]

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        if get_week_status(cursor, shift["work_date"])["status"] == "published":
            return published_week_response()


        staff_rows = load_role_staff(cursor, shift["role_type"], staff_id)

        if not staff_rows:
            return jsonify({
                "success": False,
                "message": "Staff is not active or does not have this role."
            }), 400

        staff = staff_rows[0]


        # Lock this position for the rest of the transaction.
        position_rows = load_position_rows(cursor, shift, for_update=True)

        if schedule_id is not None and not any(
            row["schedule_id"] == schedule_id for row in position_rows
        ):
            db.rollback()
            return jsonify({
                "success": False,
                "message": "This shift no longer exists. Reload the page."
            }), 404


        # ---------------------------------------------
        # Rules
        # ---------------------------------------------

        result = evaluate_staff(
            staff,
            shift["role_type"],
            start_min,
            end_min,
            load_available_ranges(
                cursor, [staff_id], shift["work_date"]
            ).get(staff_id, []),
            load_day_assignments(
                cursor, [staff_id], shift["work_date"],
                exclude_schedule_id=schedule_id
            ).get(staff_id, [])
        )

        if not result["eligible"]:
            db.rollback()
            return jsonify({
                "success": False,
                "message": result["reason"]
            }), 400

        if result["requires_override"] and not override_requested:
            db.rollback()
            return jsonify({
                "success": False,
                "requires_override": True,
                "violations": result["violations"],
                "violation_messages": result["violation_messages"],
                "message": "This assignment requires an override."
            }), 409


        # ---------------------------------------------
        # Other shifts in this position that overlap
        # ---------------------------------------------

        conflicts = [
            row for row in position_rows
            if row["schedule_id"] != schedule_id
            and ranges_overlap(
                start_min, end_min,
                row["start_min"], row["end_min"]
            )
        ]

        if conflicts and not replace_requested:
            db.rollback()
            return jsonify({
                "success": False,
                "requires_replace": True,
                "position_conflicts": conflict_payload(conflicts),
                "message": "This time overlaps other shifts in this position."
            }), 409

        for row in conflicts:

            if row["start_min"] >= start_min and row["end_min"] <= end_min:

                # Fully covered -> remove
                cursor.execute(
                    "DELETE FROM staff_schedule WHERE schedule_id = %s",
                    (row["schedule_id"],)
                )

            elif row["start_min"] < start_min and row["end_min"] > end_min:

                # New shift sits in the middle -> split into two
                cursor.execute(
                    """
                    UPDATE staff_schedule
                    SET end_time = %s, status = 'draft'
                    WHERE schedule_id = %s
                    """,
                    (minutes_to_sql(start_min), row["schedule_id"])
                )

                cursor.execute(
                    """
                    INSERT INTO staff_schedule
                    (
                        work_date, store_id, start_time, end_time,
                        staff_id, role_type, position_no, status,
                        override_daily_limit, override_consecutive, overridden_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s)
                    """,
                    (
                        shift["work_date"], shift["store_id"],
                        minutes_to_sql(end_min), minutes_to_sql(row["end_min"]),
                        row["staff_id"], shift["role_type"], shift["position_no"],
                        int(bool(row["override_daily_limit"])),
                        int(bool(row["override_consecutive"])),
                        row["overridden_at"]
                    )
                )

            elif row["start_min"] < start_min:

                # Overlaps the start -> cut its end
                cursor.execute(
                    """
                    UPDATE staff_schedule
                    SET end_time = %s, status = 'draft'
                    WHERE schedule_id = %s
                    """,
                    (minutes_to_sql(start_min), row["schedule_id"])
                )

            else:

                # Overlaps the end -> move its start
                cursor.execute(
                    """
                    UPDATE staff_schedule
                    SET start_time = %s, status = 'draft'
                    WHERE schedule_id = %s
                    """,
                    (minutes_to_sql(end_min), row["schedule_id"])
                )


        # ---------------------------------------------
        # Insert / update
        # ---------------------------------------------

        override_daily = OVERRIDE_DAILY_LIMIT in result["violations"]
        override_consecutive = OVERRIDE_CONSECUTIVE in result["violations"]
        overridden_at = (
            datetime.now()
            if override_daily or override_consecutive
            else None
        )

        if schedule_id is not None:

            cursor.execute(
                """
                UPDATE staff_schedule
                SET
                    staff_id = %s,
                    start_time = %s,
                    end_time = %s,
                    status = 'draft',
                    override_daily_limit = %s,
                    override_consecutive = %s,
                    overridden_at = %s
                WHERE schedule_id = %s
                """,
                (
                    staff_id,
                    minutes_to_sql(start_min),
                    minutes_to_sql(end_min),
                    int(override_daily),
                    int(override_consecutive),
                    overridden_at,
                    schedule_id
                )
            )

        else:

            cursor.execute(
                """
                INSERT INTO staff_schedule
                (
                    work_date, store_id, start_time, end_time,
                    staff_id, role_type, position_no, status,
                    override_daily_limit, override_consecutive, overridden_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s)
                """,
                (
                    shift["work_date"], shift["store_id"],
                    minutes_to_sql(start_min), minutes_to_sql(end_min),
                    staff_id, shift["role_type"], shift["position_no"],
                    int(override_daily), int(override_consecutive),
                    overridden_at
                )
            )

            schedule_id = cursor.lastrowid


        # ---------------------------------------------
        # Merge touching shifts of the same person
        # ---------------------------------------------

        merged_count = 0

        while True:

            neighbour = next(
                (
                    row for row in load_position_rows(cursor, shift)
                    if row["staff_id"] == staff_id
                    and row["schedule_id"] != schedule_id
                    and (
                        row["end_min"] == start_min
                        or row["start_min"] == end_min
                    )
                ),
                None
            )

            if neighbour is None:
                break

            start_min = min(start_min, neighbour["start_min"])
            end_min = max(end_min, neighbour["end_min"])

            cursor.execute(
                """
                UPDATE staff_schedule
                SET
                    start_time = %s,
                    end_time = %s,
                    override_daily_limit = override_daily_limit OR %s,
                    override_consecutive = override_consecutive OR %s,
                    overridden_at = COALESCE(overridden_at, %s)
                WHERE schedule_id = %s
                """,
                (
                    minutes_to_sql(start_min),
                    minutes_to_sql(end_min),
                    int(bool(neighbour["override_daily_limit"])),
                    int(bool(neighbour["override_consecutive"])),
                    neighbour["overridden_at"],
                    schedule_id
                )
            )

            cursor.execute(
                "DELETE FROM staff_schedule WHERE schedule_id = %s",
                (neighbour["schedule_id"],)
            )

            merged_count += 1


        db.commit()

        has_override = override_daily or override_consecutive

        return jsonify({
            "success": True,
            "message": (
                "Schedule saved with override."
                if has_override
                else "Schedule saved."
            ),
            "schedule_id": schedule_id,
            "start_time": minutes_to_hhmm(start_min),
            "end_time": minutes_to_hhmm(end_min),
            "replaced": len(conflicts),
            "merged": merged_count
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
# Remove Shift
#
# DELETE /api/staff-schedule   {"schedule_id": 123}
# =========================================================

@schedule_bp.route(
    "/api/staff-schedule",
    methods=["DELETE"]
)
@login_required
def delete_staff_schedule():

    permission_error = require_schedule_edit_access()

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    try:
        schedule_id = int(data.get("schedule_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Invalid request"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            "SELECT work_date FROM staff_schedule WHERE schedule_id = %s",
            (schedule_id,)
        )

        row = cursor.fetchone()

        if not row:
            return jsonify({
                "success": False,
                "message": "This shift no longer exists. Reload the page."
            }), 404

        if get_week_status(cursor, row["work_date"])["status"] == "published":
            return published_week_response()

        cursor.execute(
            "DELETE FROM staff_schedule WHERE schedule_id = %s",
            (schedule_id,)
        )

        db.commit()

        return jsonify({
            "success": True,
            "message": "Assignment removed."
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
# Confirm / Publish Week
# =========================================================

@schedule_bp.route(
    "/api/schedule-confirm",
    methods=["POST"]
)
@login_required
def confirm_schedule_week():

    permission_error = require_schedule_edit_access()

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    week_start = str(data.get("week_start", "")).strip()

    if not week_start:
        return jsonify({
            "success": False,
            "message": "week_start is required"
        }), 400

    try:
        monday, week_end = week_bounds(week_start)
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Invalid week_start"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        week_state = get_week_status(cursor, monday)

        if week_state["total_count"] == 0:
            return jsonify({
                "success": False,
                "message": "There is no schedule to publish for this week."
            }), 400

        if week_state["status"] == "published":
            return jsonify({
                "success": True,
                "message": "Schedule is already published.",
                "week_status": "published",
                "published_at": week_state["published_at"]
            })

        published_at = datetime.now()

        cursor.execute(
            """
            UPDATE staff_schedule

            SET
                status = 'published',
                published_at = %s

            WHERE work_date
                BETWEEN %s AND %s
            """,
            (
                published_at,
                monday,
                week_end
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "message": "Schedule published.",
            "week_status": "published",
            "published_at": serialize_datetime(published_at)
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
# Modify Published Week
# =========================================================

@schedule_bp.route(
    "/api/schedule-modify",
    methods=["POST"]
)
@login_required
def modify_schedule_week():

    permission_error = require_schedule_edit_access()

    if permission_error:
        return permission_error

    data = request.get_json(silent=True) or {}

    week_start = str(data.get("week_start", "")).strip()

    if not week_start:
        return jsonify({
            "success": False,
            "message": "week_start is required"
        }), 400

    try:
        monday, week_end = week_bounds(week_start)
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Invalid week_start"
        }), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        week_state = get_week_status(cursor, monday)

        if week_state["total_count"] == 0:
            return jsonify({
                "success": True,
                "message": "Schedule is already in draft mode.",
                "week_status": "draft"
            })

        cursor.execute(
            """
            UPDATE staff_schedule

            SET
                status = 'draft'

            WHERE work_date
                BETWEEN %s AND %s
            """,
            (
                monday,
                week_end
            )
        )

        db.commit()

        return jsonify({
            "success": True,
            "message": "Schedule is now editable.",
            "week_status": "draft"
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
