from datetime import datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
import csv

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
)
from flask_login import login_required

from db import get_db
from permissions.permissions import (
    module_required,
    MODULE_RECORDING,
)
from utils import format_et


recording_download_bp = Blueprint(
    "recording_download",
    __name__,
    template_folder="templates",
)


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@recording_download_bp.before_request
@module_required(MODULE_RECORDING)
def require_recording_download_access():
    pass


# =========================================================
# Helpers
# =========================================================

def parse_date(value, field_name):
    value = str(value or "").strip()

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

    except ValueError:
        raise ValueError(
            f"{field_name} must use YYYY-MM-DD format."
        )


def get_utc_range_from_et_dates(
    start_date,
    end_date,
):
    """
    Convert an ET calendar date range into UTC datetime boundaries.

    Example:
        start_date = 2026-08-20
        end_date   = 2026-08-27

    Result:
        [2026-08-20 00:00 ET, 2026-08-28 00:00 ET)
        converted to UTC.

    Using an exclusive upper bound avoids problems with fractional seconds.
    """

    start_et = datetime.combine(
        start_date,
        time.min,
        tzinfo=ET,
    )

    end_exclusive_et = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=ET,
    )

    return (
        start_et.astimezone(UTC).replace(tzinfo=None),
        end_exclusive_et.astimezone(UTC).replace(tzinfo=None),
    )


def normalize_store_id(value):
    if value in (
        None,
        "",
        "all",
        "ALL",
    ):
        return None

    try:
        store_id = int(value)

    except (TypeError, ValueError):
        raise ValueError(
            "store_id must be an integer or 'all'."
        )

    if store_id < 1:
        raise ValueError(
            "Invalid store_id."
        )

    return store_id


def normalize_selected_lives(raw_items):
    """
    Normalize selected LIVE identifiers from POST JSON.

    Each item must contain:
        week_id
        store_id
        live_id
    """

    if not isinstance(raw_items, list):
        raise ValueError(
            "selected_lives must be a list."
        )

    normalized = []

    seen = set()

    for item in raw_items:

        if not isinstance(item, dict):
            continue

        week_id = str(
            item.get(
                "week_id",
                "",
            )
        ).strip()

        live_id = str(
            item.get(
                "live_id",
                "",
            )
        ).strip()

        try:
            store_id = int(
                item.get(
                    "store_id"
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if (
            not week_id
            or not live_id
            or store_id < 1
        ):
            continue

        key = (
            week_id,
            store_id,
            live_id,
        )

        if key in seen:
            continue

        seen.add(key)

        normalized.append({
            "week_id": week_id,
            "store_id": store_id,
            "live_id": live_id,
        })

    if not normalized:
        raise ValueError(
            "At least one LIVE ID must be selected."
        )

    return normalized


def build_selected_live_where(
    selected_lives,
):
    """
    Build:
        (
            (sr.week_id=%s AND sr.store_id=%s AND sr.live_id=%s)
            OR
            ...
        )

    Returns:
        sql_fragment, params
    """

    clauses = []
    params = []

    for item in selected_lives:

        clauses.append(
            """
            (
                sr.week_id = %s
                AND sr.store_id = %s
                AND sr.live_id = %s
            )
            """
        )

        params.extend([
            item["week_id"],
            item["store_id"],
            item["live_id"],
        ])

    return (
        "("
        + " OR ".join(clauses)
        + ")",
        params,
    )


# =========================================================
# Page
# =========================================================

@recording_download_bp.route(
    "/recording-download"
)
@login_required
def recording_download_page():

    return render_template(
        "recording_download.html"
    )


# =========================================================
# Stores
# =========================================================

@recording_download_bp.route(
    "/api/recording-download/stores",
    methods=["GET"],
)
@login_required
def get_recording_download_stores():

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                store_id,
                store_name,
                short_name

            FROM stores

            ORDER BY
                store_id ASC
            """
        )

        rows = cursor.fetchall()

        return jsonify({
            "success": True,
            "items": rows,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# LIVE IDs by date range
# =========================================================

@recording_download_bp.route(
    "/api/recording-download/lives",
    methods=["GET"],
)
@login_required
def get_recording_download_lives():

    try:

        start_date = parse_date(
            request.args.get(
                "start_date"
            ),
            "start_date",
        )

        end_date = parse_date(
            request.args.get(
                "end_date"
            ),
            "end_date",
        )

        store_id = normalize_store_id(
            request.args.get(
                "store_id"
            )
        )

    except ValueError as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    if start_date is None:

        return jsonify({
            "success": False,
            "message":
                "start_date is required.",
        }), 400

    if end_date is None:

        return jsonify({
            "success": False,
            "message":
                "end_date is required.",
        }), 400

    if end_date < start_date:

        return jsonify({
            "success": False,
            "message":
                "end_date cannot be earlier than start_date.",
        }), 400

    # Prevent accidentally querying an excessively large range.
    if (
        end_date - start_date
    ).days > 92:

        return jsonify({
            "success": False,
            "message":
                "Date range cannot exceed 93 days.",
        }), 400

    utc_start, utc_end = (
        get_utc_range_from_et_dates(
            start_date,
            end_date,
        )
    )

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        sql = """
            SELECT
                sr.week_id,
                sr.store_id,
                s.short_name AS store_code,
                s.store_name,
                sr.live_id,

                COUNT(*) AS total_items,

                MIN(sr.recorded_at)
                    AS started_at,

                MAX(sr.recorded_at)
                    AS ended_at,

                COALESCE(
                    MAX(sr.round_no),
                    1
                ) AS max_round

            FROM sku_recording sr

            LEFT JOIN stores s
                ON s.store_id =
                    sr.store_id

            WHERE sr.recorded_at >= %s
              AND sr.recorded_at < %s
        """

        params = [
            utc_start,
            utc_end,
        ]

        if store_id is not None:

            sql += """
              AND sr.store_id = %s
            """

            params.append(
                store_id
            )

        sql += """
            GROUP BY
                sr.week_id,
                sr.store_id,
                s.short_name,
                s.store_name,
                sr.live_id

            ORDER BY
                started_at DESC,
                sr.store_id ASC,
                sr.live_id ASC
        """

        cursor.execute(
            sql,
            params,
        )

        rows = cursor.fetchall()

        items = []

        for row in rows:

            started_at = row.get(
                "started_at"
            )

            ended_at = row.get(
                "ended_at"
            )

            items.append({
                "week_id":
                    row.get(
                        "week_id"
                    ),

                "store_id":
                    int(
                        row.get(
                            "store_id"
                        )
                    ),

                "store_code":
                    row.get(
                        "store_code"
                    )
                    or
                    str(
                        row.get(
                            "store_id"
                        )
                    ),

                "store_name":
                    row.get(
                        "store_name"
                    )
                    or "",

                "live_id":
                    row.get(
                        "live_id"
                    ),

                "total_items":
                    int(
                        row.get(
                            "total_items"
                        )
                        or 0
                    ),

                "max_round":
                    int(
                        row.get(
                            "max_round"
                        )
                        or 1
                    ),

                "started_at":
                    format_et(
                        started_at
                    )
                    if started_at
                    else "",

                "ended_at":
                    format_et(
                        ended_at
                    )
                    if ended_at
                    else "",
            })

        return jsonify({
            "success": True,
            "start_date":
                start_date.isoformat(),
            "end_date":
                end_date.isoformat(),
            "store_id":
                store_id,
            "count":
                len(items),
            "items":
                items,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()


# =========================================================
# CSV Download
# =========================================================

@recording_download_bp.route(
    "/api/recording-download/csv",
    methods=["POST"],
)
@login_required
def download_recording_csv():

    data = request.get_json() or {}

    try:

        selected_lives = (
            normalize_selected_lives(
                data.get(
                    "selected_lives"
                )
            )
        )

    except ValueError as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    where_sql, where_params = (
        build_selected_live_where(
            selected_lives
        )
    )

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        sql = f"""
            SELECT
                sr.recording_id,
                sr.week_id,
                sr.store_id,
                s.short_name AS store_code,
                sr.live_id,
                sr.round_no,
                sr.seq,
                sr.sku,
                sr.quantity,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN stores s
                ON s.store_id =
                    sr.store_id

            WHERE {where_sql}

            ORDER BY
                sr.week_id ASC,
                sr.store_id ASC,
                sr.live_id ASC,
                sr.round_no ASC,
                sr.seq ASC,
                sr.recording_id ASC
        """

        cursor.execute(
            sql,
            where_params,
        )

        rows = cursor.fetchall()

        output = StringIO(
            newline=""
        )

        # UTF-8 BOM helps Excel open Chinese / UTF-8 text correctly.
        output.write(
            "\ufeff"
        )

        writer = csv.writer(
            output
        )

        writer.writerow([
            "Week",
            "Store",
            "LIVE ID",
            "Round",
            "Seq",
            "SKU",
            "Quantity",
            "Recorded At ET",
        ])

        for row in rows:

            writer.writerow([
                row.get(
                    "week_id"
                )
                or "",

                row.get(
                    "store_code"
                )
                or row.get(
                    "store_id"
                )
                or "",

                row.get(
                    "live_id"
                )
                or "",

                row.get(
                    "round_no"
                )
                or "",

                row.get(
                    "seq"
                )
                or "",

                row.get(
                    "sku"
                )
                or "",

                row.get(
                    "quantity"
                )
                or 0,

                format_et(
                    row.get(
                        "recorded_at"
                    )
                )
                if row.get(
                    "recorded_at"
                )
                else "",
            ])

        csv_text = output.getvalue()

        output.close()

        if len(selected_lives) == 1:

            item = selected_lives[0]

            filename = (
                "recording_"
                f'{item["week_id"]}_'
                f'{item["store_id"]}_'
                f'{item["live_id"]}.csv'
            )

        else:

            filename = (
                "recording_"
                f'{len(selected_lives)}_lives.csv'
            )

        return Response(
            csv_text,
            mimetype=(
                "text/csv; "
                "charset=utf-8"
            ),
            headers={
                "Content-Disposition":
                    f'attachment; filename="{filename}"'
            },
        )

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()
