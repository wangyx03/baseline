from flask import Blueprint, render_template
from flask_login import login_required

from db import get_db


availability_management_bp = Blueprint(
    "availability_management",
    __name__,
    template_folder="templates"
)


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
                name

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