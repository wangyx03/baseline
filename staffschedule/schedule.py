from datetime import date, datetime, time, timedelta

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request
)
from flask_login import login_required

from db import get_db


schedule_bp = Blueprint(
    "schedule",
    __name__,
    template_folder="templates"
)


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

OVERRIDE_DAILY_LIMIT = "daily_hour_limit"
OVERRIDE_CONSECUTIVE = "consecutive_operator"

OVERRIDE_MESSAGES = {
    OVERRIDE_DAILY_LIMIT:
        "Daily hour limit would be exceeded.",
    OVERRIDE_CONSECUTIVE:
        "Operator would work consecutive slots."
}


# =========================================================
# Helpers
# =========================================================

def parse_date(value):

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return datetime.strptime(
        str(value),
        "%Y-%m-%d"
    ).date()


def normalize_time(value):

    if isinstance(value, time):
        return value

    if isinstance(value, timedelta):

        total_seconds = int(
            value.total_seconds()
        )

        hours = (
            total_seconds // 3600
        )

        minutes = (
            total_seconds % 3600
        ) // 60

        seconds = (
            total_seconds % 60
        )

        return time(
            hour=hours,
            minute=minutes,
            second=seconds
        )

    value = str(value)

    if "." in value:
        value = value.split(".")[0]

    return datetime.strptime(
        value,
        "%H:%M:%S"
    ).time()


def local_datetime(
    work_date,
    local_time
):

    return datetime.combine(
        parse_date(work_date),
        normalize_time(local_time)
    )


def get_slot_range(
    work_date,
    slot
):

    start_dt = local_datetime(
        work_date,
        slot["start_time"]
    )

    end_dt = local_datetime(
        work_date,
        slot["end_time"]
    )

    # Future support for overnight slots.
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    return (
        start_dt,
        end_dt
    )


def ranges_overlap(
    start_a,
    end_a,
    start_b,
    end_b
):

    return (
        start_a < end_b
        and
        end_a > start_b
    )


def merge_ranges(ranges):

    if not ranges:
        return []

    ranges = sorted(
        ranges,
        key=lambda item: item[0]
    )

    merged = [
        list(ranges[0])
    ]

    for start, end in ranges[1:]:

        previous = merged[-1]

        if start <= previous[1]:

            if end > previous[1]:
                previous[1] = end

        else:

            merged.append([
                start,
                end
            ])

    return [
        tuple(item)
        for item in merged
    ]


def fully_covered(
    target_start,
    target_end,
    available_ranges
):

    for start, end in merge_ranges(
        available_ranges
    ):

        if (
            start <= target_start
            and
            end >= target_end
        ):
            return True

    return False


def monday_for(value):

    work_date = parse_date(
        value
    )

    return (
        work_date
        -
        timedelta(
            days=work_date.weekday()
        )
    )


def week_bounds(value):

    monday = monday_for(
        value
    )

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


def load_slot(
    cursor,
    slot_id
):

    cursor.execute(
        """
        SELECT
            sa.slot_id,
            sa.store_id,
            sa.slot_no,
            sa.start_time,
            sa.end_time,
            sa.timezone,
            sa.active,

            s.store_name,
            s.short_name

        FROM slots_arrangement sa

        JOIN stores s
            ON s.store_id = sa.store_id

        WHERE sa.slot_id = %s
        """,
        (
            slot_id,
        )
    )

    return cursor.fetchone()


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

    row = (
        cursor.fetchone()
        or
        {}
    )

    total_count = int(
        row.get("total_count")
        or
        0
    )

    draft_count = int(
        row.get("draft_count")
        or
        0
    )

    if (
        total_count > 0
        and
        draft_count == 0
    ):

        status = "published"

    else:

        status = "draft"

    return {
        "status":
            status,

        "total_count":
            total_count,

        "draft_count":
            draft_count,

        "published_at":
            serialize_datetime(
                row.get(
                    "published_at"
                )
            )
    }


def get_staff_available_ranges(
    cursor,
    staff_id,
    work_date
):

    cursor.execute(
        """
        SELECT
            work_date,
            start_time,
            end_time

        FROM staff_availability

        WHERE staff_id = %s
          AND work_date = %s
          AND is_available = TRUE

        ORDER BY
            start_time,
            end_time
        """,
        (
            staff_id,
            work_date
        )
    )

    rows = cursor.fetchall()

    ranges = []

    for row in rows:

        start_dt = local_datetime(
            row["work_date"],
            row["start_time"]
        )

        end_dt = local_datetime(
            row["work_date"],
            row["end_time"]
        )

        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        ranges.append(
            (
                start_dt,
                end_dt
            )
        )

    return ranges


def get_staff_assignments_for_day(
    cursor,
    staff_id,
    work_date,
    exclude_slot_id=None
):

    sql = """
        SELECT
            ss.schedule_id,
            ss.slot_id,
            ss.role_type,
            ss.position_no,

            sa.start_time,
            sa.end_time,
            sa.store_id

        FROM staff_schedule ss

        JOIN slots_arrangement sa
            ON sa.slot_id = ss.slot_id

        WHERE ss.staff_id = %s
          AND ss.work_date = %s
    """

    params = [
        staff_id,
        work_date
    ]

    if exclude_slot_id is not None:

        sql += """
            AND ss.slot_id <> %s
        """

        params.append(
            exclude_slot_id
        )

    cursor.execute(
        sql,
        tuple(params)
    )

    return cursor.fetchall()


def get_scheduled_hours(
    work_date,
    assignments
):

    total_seconds = 0

    for assignment in assignments:

        start_dt = local_datetime(
            work_date,
            assignment["start_time"]
        )

        end_dt = local_datetime(
            work_date,
            assignment["end_time"]
        )

        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        total_seconds += (
            end_dt
            -
            start_dt
        ).total_seconds()

    return (
        total_seconds
        /
        3600
    )


def check_staff_eligibility(
    cursor,
    staff_id,
    work_date,
    slot,
    role_type
):

    # -----------------------------------------------------
    # Staff / role
    # Hard rule: cannot override.
    # -----------------------------------------------------

    cursor.execute(
        """
        SELECT
            st.staff_id,
            st.name,
            st.daily_hour_limit

        FROM staff st

        JOIN staff_live_roles slr
            ON slr.staff_id = st.staff_id

        WHERE st.staff_id = %s
          AND st.active = TRUE
          AND slr.role_type = %s
        """,
        (
            staff_id,
            role_type
        )
    )

    staff = cursor.fetchone()

    if not staff:

        return {
            "eligible": False,
            "requires_override": False,
            "violations": [],
            "violation_messages": [],
            "reason":
                "Staff is not active or does not have this role."
        }


    # -----------------------------------------------------
    # Target slot
    # -----------------------------------------------------

    slot_start_dt, slot_end_dt = (
        get_slot_range(
            work_date,
            slot
        )
    )


    # -----------------------------------------------------
    # Availability
    # Hard rule: cannot override.
    # -----------------------------------------------------

    available_ranges = (
        get_staff_available_ranges(
            cursor,
            staff_id,
            work_date
        )
    )

    if not fully_covered(
        slot_start_dt,
        slot_end_dt,
        available_ranges
    ):

        return {
            "eligible": False,
            "requires_override": False,
            "violations": [],
            "violation_messages": [],
            "reason":
                "Availability does not fully cover this slot."
        }


    # -----------------------------------------------------
    # Existing assignments today
    #
    # Exclude the target slot so replacing somebody in the
    # same slot does not count that slot twice.
    # -----------------------------------------------------

    assignments = (
        get_staff_assignments_for_day(
            cursor,
            staff_id,
            work_date,
            exclude_slot_id=
                slot["slot_id"]
        )
    )


    violations = []


    # -----------------------------------------------------
    # Assignment conflicts
    # -----------------------------------------------------

    for assignment in assignments:

        assignment_start_dt = local_datetime(
            work_date,
            assignment["start_time"]
        )

        assignment_end_dt = local_datetime(
            work_date,
            assignment["end_time"]
        )

        if (
            assignment_end_dt
            <=
            assignment_start_dt
        ):

            assignment_end_dt += timedelta(
                days=1
            )


        # -------------------------------------------------
        # Hard rule:
        # No overlapping assignments.
        # -------------------------------------------------

        if ranges_overlap(
            slot_start_dt,
            slot_end_dt,
            assignment_start_dt,
            assignment_end_dt
        ):

            return {
                "eligible": False,
                "requires_override": False,
                "violations": [],
                "violation_messages": [],
                "reason":
                    "Staff already has an overlapping assignment."
            }


        # -------------------------------------------------
        # Soft rule:
        # Operator cannot work consecutive slots.
        #
        # Store does not matter.
        # This rule may be overridden.
        # -------------------------------------------------

        if (
            role_type == "operator"
            and
            assignment["role_type"]
            == "operator"
        ):

            consecutive = (
                assignment_end_dt
                ==
                slot_start_dt
                or
                assignment_start_dt
                ==
                slot_end_dt
            )

            if (
                consecutive
                and
                OVERRIDE_CONSECUTIVE
                not in
                violations
            ):

                violations.append(
                    OVERRIDE_CONSECUTIVE
                )


    # -----------------------------------------------------
    # Soft rule:
    # Daily hour limit.
    # This rule may be overridden.
    # -----------------------------------------------------

    scheduled_hours = (
        get_scheduled_hours(
            work_date,
            assignments
        )
    )

    slot_hours = (
        (
            slot_end_dt
            -
            slot_start_dt
        ).total_seconds()
        /
        3600
    )

    daily_limit = float(
        staff["daily_hour_limit"]
    )

    total_hours_after = (
        scheduled_hours
        +
        slot_hours
    )

    if (
        total_hours_after
        >
        daily_limit
    ):

        violations.append(
            OVERRIDE_DAILY_LIMIT
        )


    violation_messages = [
        OVERRIDE_MESSAGES[
            violation
        ]
        for violation
        in violations
    ]


    return {
        "eligible": True,

        "requires_override":
            bool(
                violations
            ),

        "violations":
            violations,

        "violation_messages":
            violation_messages,

        "staff_id":
            staff["staff_id"],

        "name":
            staff["name"],

        "daily_hour_limit":
            daily_limit,

        "scheduled_hours":
            scheduled_hours,

        "slot_hours":
            slot_hours,

        "total_hours_after":
            total_hours_after
    }


# =========================================================
# Schedule Page
# =========================================================

@schedule_bp.route(
    "/schedule"
)
@login_required
def schedule_page():

    return render_template(
        "schedule.html"
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

    week_start = (
        request.args.get(
            "week_start",
            ""
        )
        .strip()
    )

    if not week_start:

        return jsonify({
            "success": False,
            "message":
                "week_start is required"
        }), 400


    try:

        monday = monday_for(
            week_start
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message":
                "Invalid week_start"
        }), 400


    week_end = (
        monday
        +
        timedelta(days=6)
    )


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        # -------------------------------------------------
        # Slots
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                sa.slot_id,
                sa.store_id,
                sa.slot_no,
                sa.start_time,
                sa.end_time,
                sa.timezone,

                s.store_name,
                s.short_name

            FROM slots_arrangement sa

            JOIN stores s
                ON s.store_id = sa.store_id

            WHERE sa.active = TRUE

            ORDER BY
                sa.store_id,
                sa.slot_no
            """
        )

        slots = cursor.fetchall()


        # -------------------------------------------------
        # Existing assignments
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                ss.schedule_id,
                ss.work_date,
                ss.slot_id,
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

            WHERE ss.work_date
                BETWEEN %s AND %s

            ORDER BY
                ss.work_date,
                ss.slot_id,
                ss.role_type,
                ss.position_no
            """,
            (
                monday,
                week_end
            )
        )

        rows = cursor.fetchall()


        assignment_map = {}


        for row in rows:

            key = (
                row[
                    "work_date"
                ].isoformat(),
                row[
                    "slot_id"
                ]
            )

            assignment_map.setdefault(
                key,
                []
            )

            assignment_map[
                key
            ].append({

                "schedule_id":
                    row[
                        "schedule_id"
                    ],

                "staff_id":
                    row[
                        "staff_id"
                    ],

                "staff_name":
                    row[
                        "staff_name"
                    ],

                "role_type":
                    row[
                        "role_type"
                    ],

                "position_no":
                    row[
                        "position_no"
                    ],

                "status":
                    row[
                        "status"
                    ],

                "override_daily_limit":
                    bool(
                        row[
                            "override_daily_limit"
                        ]
                    ),

                "override_consecutive":
                    bool(
                        row[
                            "override_consecutive"
                        ]
                    ),

                "overridden_at":
                    serialize_datetime(
                        row[
                            "overridden_at"
                        ]
                    ),

                "published_at":
                    serialize_datetime(
                        row[
                            "published_at"
                        ]
                    )
            })


        # -------------------------------------------------
        # Build seven days
        # -------------------------------------------------

        days = []


        for day_offset in range(7):

            work_date = (
                monday
                +
                timedelta(
                    days=day_offset
                )
            )

            date_string = (
                work_date.isoformat()
            )

            day_slots = []


            for slot in slots:

                key = (
                    date_string,
                    slot[
                        "slot_id"
                    ]
                )

                day_slots.append({

                    "slot_id":
                        slot[
                            "slot_id"
                        ],

                    "store_id":
                        slot[
                            "store_id"
                        ],

                    "store_name":
                        slot[
                            "store_name"
                        ],

                    "store_code":
                        slot[
                            "short_name"
                        ],

                    "slot_no":
                        slot[
                            "slot_no"
                        ],

                    "start_time":
                        str(
                            slot[
                                "start_time"
                            ]
                        ),

                    "end_time":
                        str(
                            slot[
                                "end_time"
                            ]
                        ),

                    "timezone":
                        slot[
                            "timezone"
                        ],

                    "assignments":
                        assignment_map.get(
                            key,
                            []
                        )
                })


            days.append({

                "work_date":
                    date_string,

                "day_name":
                    work_date.strftime(
                        "%A"
                    ),

                "slots":
                    day_slots
            })


        week_state = get_week_status(
            cursor,
            monday
        )


        return jsonify({

            "success": True,

            "week_start":
                monday.isoformat(),

            "week_end":
                week_end.isoformat(),

            "week_status":
                week_state[
                    "status"
                ],

            "published_at":
                week_state[
                    "published_at"
                ],

            "assignment_count":
                week_state[
                    "total_count"
                ],

            "days":
                days
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
# =========================================================

@schedule_bp.route(
    "/api/schedule-candidates",
    methods=["GET"]
)
@login_required
def get_schedule_candidates():

    work_date = (
        request.args.get(
            "work_date",
            ""
        )
        .strip()
    )

    slot_id = request.args.get(
        "slot_id",
        type=int
    )

    role_type = (
        request.args.get(
            "role_type",
            ""
        )
        .strip()
        .lower()
    )


    if not work_date:

        return jsonify({
            "success": False,
            "message":
                "work_date is required"
        }), 400


    if slot_id is None:

        return jsonify({
            "success": False,
            "message":
                "slot_id is required"
        }), 400


    if role_type not in VALID_ROLES:

        return jsonify({
            "success": False,
            "message":
                "Invalid role_type"
        }), 400


    try:

        work_date_obj = parse_date(
            work_date
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message":
                "Invalid work_date"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        week_state = get_week_status(
            cursor,
            work_date
        )

        if (
            week_state[
                "status"
            ]
            ==
            "published"
        ):

            return jsonify({
                "success": False,
                "message":
                    "Published schedule must be changed to Modify mode before editing."
            }), 409


        slot = load_slot(
            cursor,
            slot_id
        )

        if not slot:

            return jsonify({
                "success": False,
                "message":
                    "Slot not found"
            }), 404


        if not slot["active"]:

            return jsonify({
                "success": False,
                "message":
                    "Slot is inactive"
            }), 400


        # -------------------------------------------------
        # 1) Load every active staff member for this role.
        #    One query for the whole candidate pool.
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT DISTINCT
                st.staff_id,
                st.name,
                st.daily_hour_limit

            FROM staff st

            JOIN staff_live_roles slr
                ON slr.staff_id = st.staff_id

            WHERE st.active = TRUE
              AND slr.role_type = %s

            ORDER BY
                st.staff_id
            """,
            (
                role_type,
            )
        )

        staff_rows = cursor.fetchall()

        if not staff_rows:

            return jsonify({
                "success": True,
                "work_date": work_date,
                "slot_id": slot_id,
                "role_type": role_type,
                "candidates": []
            })


        staff_ids = [
            row["staff_id"]
            for row in staff_rows
        ]

        placeholders = ", ".join(
            ["%s"] * len(staff_ids)
        )


        # -------------------------------------------------
        # 2) Load availability for every candidate at once.
        # -------------------------------------------------

        cursor.execute(
            f"""
            SELECT
                staff_id,
                work_date,
                start_time,
                end_time

            FROM staff_availability

            WHERE work_date = %s
              AND is_available = TRUE
              AND staff_id IN ({placeholders})

            ORDER BY
                staff_id,
                start_time,
                end_time
            """,
            tuple(
                [work_date_obj]
                + staff_ids
            )
        )

        availability_rows = cursor.fetchall()

        availability_map = {}

        for row in availability_rows:

            start_dt = local_datetime(
                row["work_date"],
                row["start_time"]
            )

            end_dt = local_datetime(
                row["work_date"],
                row["end_time"]
            )

            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            availability_map.setdefault(
                row["staff_id"],
                []
            ).append((
                start_dt,
                end_dt
            ))


        # -------------------------------------------------
        # 3) Load today's assignments for every candidate
        #    at once. The target slot is excluded later in
        #    Python, matching check_staff_eligibility().
        # -------------------------------------------------

        cursor.execute(
            f"""
            SELECT
                ss.staff_id,
                ss.schedule_id,
                ss.slot_id,
                ss.role_type,
                ss.position_no,

                sa.start_time,
                sa.end_time,
                sa.store_id

            FROM staff_schedule ss

            JOIN slots_arrangement sa
                ON sa.slot_id = ss.slot_id

            WHERE ss.work_date = %s
              AND ss.staff_id IN ({placeholders})

            ORDER BY
                ss.staff_id,
                sa.start_time,
                sa.end_time
            """,
            tuple(
                [work_date_obj]
                + staff_ids
            )
        )

        assignment_rows = cursor.fetchall()

        assignment_map = {}

        for row in assignment_rows:
            assignment_map.setdefault(
                row["staff_id"],
                []
            ).append(row)


        # -------------------------------------------------
        # Evaluate candidates entirely in Python.
        # No per-staff SQL queries below this point.
        # -------------------------------------------------

        slot_start_dt, slot_end_dt = get_slot_range(
            work_date_obj,
            slot
        )

        slot_hours = (
            (
                slot_end_dt
                -
                slot_start_dt
            ).total_seconds()
            /
            3600
        )

        candidates = []


        for staff in staff_rows:

            staff_id = staff["staff_id"]

            available_ranges = availability_map.get(
                staff_id,
                []
            )

            # Hard rule: availability must fully cover slot.
            if not fully_covered(
                slot_start_dt,
                slot_end_dt,
                available_ranges
            ):
                continue


            assignments = [
                assignment
                for assignment in assignment_map.get(
                    staff_id,
                    []
                )
                if assignment["slot_id"] != slot_id
            ]

            violations = []
            hard_conflict = False


            for assignment in assignments:

                assignment_start_dt = local_datetime(
                    work_date_obj,
                    assignment["start_time"]
                )

                assignment_end_dt = local_datetime(
                    work_date_obj,
                    assignment["end_time"]
                )

                if assignment_end_dt <= assignment_start_dt:
                    assignment_end_dt += timedelta(days=1)


                # Hard rule: no overlapping assignments.
                if ranges_overlap(
                    slot_start_dt,
                    slot_end_dt,
                    assignment_start_dt,
                    assignment_end_dt
                ):
                    hard_conflict = True
                    break


                # Soft rule: operator cannot work
                # consecutive operator slots.
                if (
                    role_type == "operator"
                    and
                    assignment["role_type"] == "operator"
                ):

                    consecutive = (
                        assignment_end_dt == slot_start_dt
                        or
                        assignment_start_dt == slot_end_dt
                    )

                    if (
                        consecutive
                        and
                        OVERRIDE_CONSECUTIVE not in violations
                    ):
                        violations.append(
                            OVERRIDE_CONSECUTIVE
                        )


            if hard_conflict:
                continue


            scheduled_hours = get_scheduled_hours(
                work_date_obj,
                assignments
            )

            daily_limit = float(
                staff["daily_hour_limit"]
            )

            total_hours_after = (
                scheduled_hours
                +
                slot_hours
            )

            if total_hours_after > daily_limit:
                violations.append(
                    OVERRIDE_DAILY_LIMIT
                )


            violation_messages = [
                OVERRIDE_MESSAGES[violation]
                for violation in violations
            ]


            candidates.append({
                "eligible": True,
                "requires_override": bool(violations),
                "violations": violations,
                "violation_messages": violation_messages,
                "staff_id": staff_id,
                "name": staff["name"],
                "daily_hour_limit": daily_limit,
                "scheduled_hours": scheduled_hours,
                "slot_hours": slot_hours,
                "total_hours_after": total_hours_after
            })


        candidates.sort(
            key=lambda item: (
                item["requires_override"],
                item["scheduled_hours"],
                item["name"].lower()
            )
        )


        return jsonify({
            "success": True,
            "work_date": work_date,
            "slot_id": slot_id,
            "role_type": role_type,
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
# Save Assignment
# =========================================================

@schedule_bp.route(
    "/api/staff-schedule",
    methods=["POST"]
)
@login_required
def save_staff_schedule():

    data = (
        request.get_json(
            silent=True
        )
        or
        {}
    )


    work_date = (
        str(
            data.get(
                "work_date",
                ""
            )
        )
        .strip()
    )

    slot_id = data.get(
        "slot_id"
    )

    staff_id = data.get(
        "staff_id"
    )

    role_type = (
        str(
            data.get(
                "role_type",
                ""
            )
        )
        .strip()
        .lower()
    )

    position_no = data.get(
        "position_no",
        1
    )

    override_requested = bool(
        data.get(
            "override",
            False
        )
    )


    if not work_date:

        return jsonify({
            "success": False,
            "message":
                "work_date is required"
        }), 400


    try:

        parse_date(
            work_date
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message":
                "Invalid work_date"
        }), 400


    if not slot_id:

        return jsonify({
            "success": False,
            "message":
                "slot_id is required"
        }), 400


    if not staff_id:

        return jsonify({
            "success": False,
            "message":
                "staff_id is required"
        }), 400


    if role_type not in VALID_ROLES:

        return jsonify({
            "success": False,
            "message":
                "Invalid role_type"
        }), 400


    try:

        position_no = int(
            position_no
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid position_no"
        }), 400


    if position_no < 1:

        return jsonify({
            "success": False,
            "message":
                "position_no must be at least 1"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        # -------------------------------------------------
        # Published week is locked.
        # -------------------------------------------------

        week_state = get_week_status(
            cursor,
            work_date
        )

        if (
            week_state[
                "status"
            ]
            ==
            "published"
        ):

            return jsonify({
                "success": False,
                "message":
                    "Published schedule must be changed to Modify mode before editing."
            }), 409


        slot = load_slot(
            cursor,
            slot_id
        )

        if not slot:

            return jsonify({
                "success": False,
                "message":
                    "Slot not found"
            }), 404


        if not slot["active"]:

            return jsonify({
                "success": False,
                "message":
                    "Slot is inactive"
            }), 400


        # -------------------------------------------------
        # Validate employee.
        # -------------------------------------------------

        eligibility = (
            check_staff_eligibility(
                cursor,
                staff_id,
                work_date,
                slot,
                role_type
            )
        )

        if not eligibility[
            "eligible"
        ]:

            return jsonify({
                "success": False,
                "message":
                    eligibility[
                        "reason"
                    ]
            }), 400


        violations = eligibility[
            "violations"
        ]


        # -------------------------------------------------
        # Soft rules require explicit override.
        # -------------------------------------------------

        if (
            eligibility[
                "requires_override"
            ]
            and
            not override_requested
        ):

            return jsonify({

                "success": False,

                "requires_override":
                    True,

                "violations":
                    violations,

                "violation_messages":
                    eligibility[
                        "violation_messages"
                    ],

                "message":
                    "This assignment requires an override."
            }), 409


        # -------------------------------------------------
        # Same employee cannot occupy another role/position
        # in the same slot.
        # Hard rule: cannot override.
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                schedule_id,
                role_type,
                position_no

            FROM staff_schedule

            WHERE work_date = %s
              AND slot_id = %s
              AND staff_id = %s
            """,
            (
                work_date,
                slot_id,
                staff_id
            )
        )

        existing_staff = (
            cursor.fetchone()
        )


        if existing_staff:

            same_position = (
                existing_staff[
                    "role_type"
                ]
                ==
                role_type

                and

                existing_staff[
                    "position_no"
                ]
                ==
                position_no
            )

            if not same_position:

                return jsonify({
                    "success": False,
                    "message":
                        "This employee is already assigned "
                        "to another role in this slot."
                }), 400


        override_daily_limit = (
            OVERRIDE_DAILY_LIMIT
            in
            violations
        )

        override_consecutive = (
            OVERRIDE_CONSECUTIVE
            in
            violations
        )

        has_override = (
            override_daily_limit
            or
            override_consecutive
        )


        # -------------------------------------------------
        # Insert / replace requested position.
        #
        # UNIQUE expected:
        # (
        #   work_date,
        #   slot_id,
        #   role_type,
        #   position_no
        # )
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO staff_schedule
            (
                work_date,
                slot_id,
                staff_id,
                role_type,
                position_no,
                status,

                override_daily_limit,
                override_consecutive,
                overridden_at
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                'draft',

                %s,
                %s,
                %s
            )

            ON DUPLICATE KEY UPDATE

                staff_id =
                    VALUES(staff_id),

                status =
                    'draft',

                override_daily_limit =
                    VALUES(
                        override_daily_limit
                    ),

                override_consecutive =
                    VALUES(
                        override_consecutive
                    ),

                overridden_at =
                    VALUES(
                        overridden_at
                    )
            """,
            (
                work_date,
                slot_id,
                staff_id,
                role_type,
                position_no,

                int(
                    override_daily_limit
                ),

                int(
                    override_consecutive
                ),

                (
                    datetime.now()
                    if has_override
                    else None
                )
            )
        )


        db.commit()


        return jsonify({

            "success": True,

            "message":
                (
                    "Schedule saved with override."
                    if has_override
                    else
                    "Schedule saved."
                ),

            "assignment": {

                "work_date":
                    work_date,

                "slot_id":
                    slot_id,

                "staff_id":
                    staff_id,

                "staff_name":
                    eligibility[
                        "name"
                    ],

                "role_type":
                    role_type,

                "position_no":
                    position_no,

                "status":
                    "draft",

                "override_daily_limit":
                    override_daily_limit,

                "override_consecutive":
                    override_consecutive,
            }
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
# Remove Assignment
# =========================================================

@schedule_bp.route(
    "/api/staff-schedule",
    methods=["DELETE"]
)
@login_required
def delete_staff_schedule():

    data = (
        request.get_json(
            silent=True
        )
        or
        {}
    )


    work_date = (
        str(
            data.get(
                "work_date",
                ""
            )
        )
        .strip()
    )

    slot_id = data.get(
        "slot_id"
    )

    role_type = (
        str(
            data.get(
                "role_type",
                ""
            )
        )
        .strip()
        .lower()
    )

    position_no = data.get(
        "position_no"
    )


    if (
        not work_date
        or
        not slot_id
        or
        role_type
        not in
        VALID_ROLES
        or
        position_no is None
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid request"
        }), 400


    try:

        parse_date(
            work_date
        )

        position_no = int(
            position_no
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid request"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        week_state = get_week_status(
            cursor,
            work_date
        )

        if (
            week_state[
                "status"
            ]
            ==
            "published"
        ):

            return jsonify({
                "success": False,
                "message":
                    "Published schedule must be changed to Modify mode before editing."
            }), 409


        cursor.execute(
            """
            DELETE FROM staff_schedule

            WHERE work_date = %s
              AND slot_id = %s
              AND role_type = %s
              AND position_no = %s
            """,
            (
                work_date,
                slot_id,
                role_type,
                position_no
            )
        )

        db.commit()


        return jsonify({

            "success": True,

            "message":
                "Assignment removed."
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

    data = (
        request.get_json(
            silent=True
        )
        or
        {}
    )

    week_start = (
        str(
            data.get(
                "week_start",
                ""
            )
        )
        .strip()
    )


    if not week_start:

        return jsonify({
            "success": False,
            "message":
                "week_start is required"
        }), 400


    try:

        monday, week_end = week_bounds(
            week_start
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message":
                "Invalid week_start"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        week_state = get_week_status(
            cursor,
            monday
        )

        if (
            week_state[
                "total_count"
            ]
            ==
            0
        ):

            return jsonify({
                "success": False,
                "message":
                    "There is no schedule to publish for this week."
            }), 400


        if (
            week_state[
                "status"
            ]
            ==
            "published"
        ):

            return jsonify({
                "success": True,
                "message":
                    "Schedule is already published.",
                "week_status":
                    "published",
                "published_at":
                    week_state[
                        "published_at"
                    ]
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

            "message":
                "Schedule published.",

            "week_status":
                "published",

            "published_at":
                serialize_datetime(
                    published_at
                )
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

    data = (
        request.get_json(
            silent=True
        )
        or
        {}
    )

    week_start = (
        str(
            data.get(
                "week_start",
                ""
            )
        )
        .strip()
    )


    if not week_start:

        return jsonify({
            "success": False,
            "message":
                "week_start is required"
        }), 400


    try:

        monday, week_end = week_bounds(
            week_start
        )

    except ValueError:

        return jsonify({
            "success": False,
            "message":
                "Invalid week_start"
        }), 400


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )


    try:

        week_state = get_week_status(
            cursor,
            monday
        )

        if (
            week_state[
                "total_count"
            ]
            ==
            0
        ):

            return jsonify({
                "success": True,
                "message":
                    "Schedule is already in draft mode.",
                "week_status":
                    "draft"
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

            "message":
                "Schedule is now editable.",

            "week_status":
                "draft"
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
