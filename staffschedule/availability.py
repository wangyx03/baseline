from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request

from db import get_db


availability_bp = Blueprint(
    "availability",
    __name__,
    template_folder="templates"
)


TIME_BLOCKS = [
    ("11:00", "14:00"),
    ("14:00", "16:00"),
    ("16:00", "17:00"),
    ("17:00", "18:00"),
    ("18:00", "21:00"),
]


@availability_bp.route("/availability/<staff_name>")
def availability_page(staff_name):

    staff_name = staff_name.strip()

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                staff_id,
                name
            FROM staff
            WHERE LOWER(name) = LOWER(%s)
              AND active = TRUE
            """,
            (staff_name,)
        )

        staff = cursor.fetchone()

        if not staff:
            return "Employee not found", 404

        return render_template(
            "availability.html",
            staff_id=staff["staff_id"],
            staff_name=staff["name"]
        )

    finally:
        cursor.close()
        db.close()


@availability_bp.route(
    "/api/staff-list",
    methods=["GET"]
)
def get_staff_list():

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                staff_id,
                name

            FROM staff

            WHERE active = TRUE

            ORDER BY name
            """
        )

        rows = cursor.fetchall()

        return jsonify({
            "success": True,
            "staff": rows
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


@availability_bp.route(
    "/api/staff-availability",
    methods=["GET"]
)
def get_staff_availability():

    staff_id = request.args.get(
        "staff_id",
        type=int
    )

    week_start = request.args.get(
        "week_start",
        ""
    ).strip()

    if not staff_id:

        return jsonify({
            "success": False,
            "message": "staff_id is required"
        }), 400

    if not week_start:

        return jsonify({
            "success": False,
            "message": "week_start is required"
        }), 400


    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

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

              AND work_date BETWEEN
                  %s
                  AND DATE_ADD(
                      %s,
                      INTERVAL 6 DAY
                  )

            ORDER BY
                work_date,
                start_time
            """,
            (
                staff_id,
                week_start,
                week_start
            )
        )

        rows = cursor.fetchall()


        results = []

        for row in rows:

            results.append({
                "availability_id":
                    row["availability_id"],

                "staff_id":
                    row["staff_id"],

                "work_date":
                    row["work_date"].isoformat(),

                "start_time":
                    str(row["start_time"]),

                "end_time":
                    str(row["end_time"]),

                "is_available":
                    bool(row["is_available"])
            })


        return jsonify({
            "success": True,
            "availability": results
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:

        cursor.close()
        db.close()


@availability_bp.route(
    "/api/staff-availability",
    methods=["POST"]
)
def save_staff_availability():

    data = request.get_json(
        silent=True
    ) or {}

    staff_id = data.get(
        "staff_id"
    )

    availability = data.get(
        "availability",
        []
    )


    if not staff_id:

        return jsonify({
            "success": False,
            "message": "staff_id is required"
        }), 400


    db = get_db()
    cursor = db.cursor()

    try:

        for item in availability:

            work_date = item.get(
                "work_date"
            )

            start_time = item.get(
                "start_time"
            )

            end_time = item.get(
                "end_time"
            )

            is_available = bool(
                item.get(
                    "is_available"
                )
            )


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

                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )

                ON DUPLICATE KEY UPDATE

                    is_available =
                        VALUES(is_available)
                """,
                (
                    staff_id,
                    work_date,
                    start_time,
                    end_time,
                    is_available
                )
            )


        db.commit()


        return jsonify({
            "success": True,
            "message":
                "Availability saved"
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