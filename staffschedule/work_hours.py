"""
Weekly work-hour range.

Every week has one display range [start_hour, end_hour), e.g. 11-21.
All availability / schedule pages show rows only for that range.

- Weeks without a saved range use DEFAULT_START_HOUR / DEFAULT_END_HOUR.
- Only Schedule Management users can change it, and only for the
  current week or next week (see availability_management.py).
- end_hour = 24 means midnight. The 23:00 row is the block 23:00 -> 00:00,
  which schedule.py already treats as crossing midnight.

Table (migrations/001_staff_work_hours.sql):

    staff_work_hours(week_start DATE PK, start_hour, end_hour, ...)
"""

from datetime import date, datetime, time, timedelta


DEFAULT_START_HOUR = 11
DEFAULT_END_HOUR = 21


# =========================================================
# Dates
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


def editable_weeks(today=None):
    """Current week's Monday and next week's Monday."""

    this_monday = monday_for(today or date.today())

    return (
        this_monday,
        this_monday + timedelta(days=7)
    )


# =========================================================
# Range
# =========================================================

def validate_range(start_hour, end_hour):
    """Returns (start, end) as ints or raises ValueError."""

    try:
        start_hour = int(start_hour)
        end_hour = int(end_hour)
    except (TypeError, ValueError):
        raise ValueError("Start and end must be whole hours.")

    if not 0 <= start_hour <= 23:
        raise ValueError("Start hour must be between 0 and 23.")

    if not 1 <= end_hour <= 24:
        raise ValueError("End hour must be between 1 and 24.")

    if end_hour <= start_hour:
        raise ValueError("End hour must be later than start hour.")

    return start_hour, end_hour


def load_week_hours(cursor, week_start):
    """
    {
        "week_start": "2026-11-09",
        "start_hour": 11,
        "end_hour": 21,
        "is_default": True
    }

    cursor must be a dictionary cursor.
    """

    monday = monday_for(week_start)

    cursor.execute(
        """
        SELECT
            start_hour,
            end_hour

        FROM staff_work_hours

        WHERE week_start = %s

        LIMIT 1
        """,
        (monday,)
    )

    row = cursor.fetchone()

    if row:
        return {
            "week_start": monday.isoformat(),
            "start_hour": int(row["start_hour"]),
            "end_hour": int(row["end_hour"]),
            "is_default": False,
        }

    return {
        "week_start": monday.isoformat(),
        "start_hour": DEFAULT_START_HOUR,
        "end_hour": DEFAULT_END_HOUR,
        "is_default": True,
    }


# =========================================================
# Time formatting / validation
# =========================================================

def time_to_hhmm(value):
    """
    MySQL TIME (timedelta), datetime.time or string -> 'HH:MM'.

    str(timedelta(hours=9)) is '9:00:00', which the pages could not
    match against '09:00'. Early-morning hours need zero padding.
    """

    if isinstance(value, timedelta):

        total = int(value.total_seconds()) % 86400

        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"

    if isinstance(value, time):
        return value.strftime("%H:%M")

    parts = str(value).strip().split(":")

    hour = int(parts[0]) % 24
    minute = int(parts[1]) if len(parts) > 1 else 0

    return f"{hour:02d}:{minute:02d}"


def parse_hour_block(start_value, end_value):
    """
    Validate an exact one-hour block ('23:00' -> '00:00' allowed).
    Returns (start 'HH:MM', end 'HH:MM'). Raises ValueError.
    """

    try:
        start = time_to_hhmm(start_value)
        end = time_to_hhmm(end_value)
    except (TypeError, ValueError):
        raise ValueError("Invalid availability time.")

    start_hour, start_minute = (int(x) for x in start.split(":"))
    end_hour, end_minute = (int(x) for x in end.split(":"))

    if (
        start_minute
        or end_minute
        or end_hour != (start_hour + 1) % 24
    ):
        raise ValueError("Availability must use one-hour blocks.")

    return start, end
