from flask import Blueprint, render_template


availability_management_bp = Blueprint(
    "availability_management",
    __name__,
    template_folder="templates"
)


@availability_management_bp.route(
    "/availability-management"
)
def availability_management_page():

    return render_template(
        "availability_management.html"
    )