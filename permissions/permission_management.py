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

from db import get_db

from permissions.permissions import (
    module_required,
    MODULE_PERMISSION_MANAGEMENT,
    MODULE_WEEKLY_INVENTORY,
    PERMISSION_WEEKLY_ACTUAL_STOCK
)


permission_management_bp = Blueprint(
    "permission_management",
    __name__,
    template_folder="templates"
)


# =========================
# Permission Management Access
# =========================

@permission_management_bp.before_request
@module_required(
    MODULE_PERMISSION_MANAGEMENT
)
def require_permission_management_access():
    pass


# =========================
# Permission Management Page
# =========================

@permission_management_bp.route(
    "/permission-management"
)
@login_required
def permission_management_page():

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        # =========================
        # Users
        # =========================

        cursor.execute(
            """
            SELECT
                li.user_id,
                li.username,
                li.staff_id,
                li.is_active,
                st.name AS staff_name

            FROM login_info li

            LEFT JOIN staff st
                ON st.staff_id = li.staff_id

            ORDER BY
                li.username
            """
        )

        users = cursor.fetchall()


        # =========================
        # Modules
        # =========================

        cursor.execute(
            """
            SELECT
                module_id,
                module_name,
                is_active

            FROM modules

            WHERE is_active = TRUE

            ORDER BY
                module_id
            """
        )

        modules = cursor.fetchall()


        return render_template(
            "permission_management.html",
            users=users,
            modules=modules,
            weekly_inventory_module_id=(
                MODULE_WEEKLY_INVENTORY
            ),
            weekly_actual_stock_permission=(
                PERMISSION_WEEKLY_ACTUAL_STOCK
            )
        )

    finally:

        cursor.close()
        db.close()


# =========================
# Get User Permissions
# =========================

@permission_management_bp.route(
    "/api/permission-management/<int:user_id>",
    methods=["GET"]
)
@login_required
def get_user_permissions(user_id):

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        # =========================
        # Validate User
        # =========================

        cursor.execute(
            """
            SELECT
                user_id,
                username,
                staff_id,
                is_active

            FROM login_info

            WHERE user_id = %s
            """,
            (
                user_id,
            )
        )

        user = cursor.fetchone()

        if user is None:

            return jsonify({
                "success": False,
                "message":
                    "User not found"
            }), 404


        # =========================
        # Module Permissions
        # =========================

        cursor.execute(
            """
            SELECT
                module_id,
                access

            FROM module_permissions

            WHERE user_id = %s

            ORDER BY
                module_id
            """,
            (
                user_id,
            )
        )

        rows = cursor.fetchall()


        permissions = {
            row["module_id"]:
                bool(
                    row["access"]
                )
            for row in rows
        }


        # =========================
        # Feature Permissions
        # =========================

        cursor.execute(
            """
            SELECT
                permission_key,
                access

            FROM permission_features

            WHERE user_id = %s
            """,
            (
                user_id,
            )
        )

        feature_rows = cursor.fetchall()


        feature_permissions = {
            row["permission_key"]:
                bool(
                    row["access"]
                )
            for row in feature_rows
        }


        return jsonify({
            "success": True,

            "user":
                user,

            "permissions":
                permissions,

            "feature_permissions":
                feature_permissions,

            "current_user_id":
                int(
                    current_user.id
                ),

            "permission_management_module_id":
                MODULE_PERMISSION_MANAGEMENT,

            "weekly_inventory_module_id":
                MODULE_WEEKLY_INVENTORY,

            "weekly_actual_stock_permission":
                PERMISSION_WEEKLY_ACTUAL_STOCK
        })

    finally:

        cursor.close()
        db.close()


# =========================
# Save User Permissions
# =========================

@permission_management_bp.route(
    "/api/permission-management/<int:user_id>",
    methods=["POST"]
)
@login_required
def save_user_permissions(user_id):

    data = request.get_json(
        silent=True
    ) or {}


    module_ids = data.get(
        "module_ids",
        []
    )


    feature_permissions = data.get(
        "feature_permissions",
        {}
    )


    # =========================
    # Validate module_ids
    # =========================

    if not isinstance(
        module_ids,
        list
    ):

        return jsonify({
            "success": False,
            "message":
                "module_ids must be a list"
        }), 400


    # =========================
    # Validate feature_permissions
    # =========================

    if not isinstance(
        feature_permissions,
        dict
    ):

        return jsonify({
            "success": False,
            "message":
                "feature_permissions must be an object"
        }), 400


    allowed_feature_permissions = {
        PERMISSION_WEEKLY_ACTUAL_STOCK
    }


    unknown_feature_permissions = (
        set(
            feature_permissions.keys()
        )
        -
        allowed_feature_permissions
    )


    if unknown_feature_permissions:

        return jsonify({
            "success": False,
            "message":
                "One or more feature permissions are invalid"
        }), 400


    cleaned_module_ids = []


    for module_id in module_ids:

        try:

            module_id = int(
                module_id
            )

        except (
            TypeError,
            ValueError
        ):

            return jsonify({
                "success": False,
                "message":
                    "Invalid module_id"
            }), 400


        if (
            module_id
            not in
            cleaned_module_ids
        ):

            cleaned_module_ids.append(
                module_id
            )


    # =========================
    # Normalize Feature Access
    # =========================

    actual_stock_access = (
        feature_permissions.get(
            PERMISSION_WEEKLY_ACTUAL_STOCK,
            False
        )
        is True
    )


    # Feature permission cannot exist without Module 2.
    if (
        MODULE_WEEKLY_INVENTORY
        not in
        cleaned_module_ids
    ):

        actual_stock_access = False


    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        # =========================
        # Validate User
        # =========================

        cursor.execute(
            """
            SELECT
                user_id

            FROM login_info

            WHERE user_id = %s
            """,
            (
                user_id,
            )
        )

        if cursor.fetchone() is None:

            return jsonify({
                "success": False,
                "message":
                    "User not found"
            }), 404


        # =========================
        # Protect Existing
        # Permission Administrator
        # =========================

        cursor.execute(
            """
            SELECT
                access

            FROM module_permissions

            WHERE user_id = %s
              AND module_id = %s
              AND access = TRUE
            """,
            (
                user_id,
                MODULE_PERMISSION_MANAGEMENT
            )
        )

        existing_admin_permission = (
            cursor.fetchone()
        )


        if (
            existing_admin_permission
            and
            MODULE_PERMISSION_MANAGEMENT
            not in
            cleaned_module_ids
        ):

            cleaned_module_ids.append(
                MODULE_PERMISSION_MANAGEMENT
            )


        # =========================
        # Validate Modules
        # =========================

        if cleaned_module_ids:

            placeholders = ", ".join(
                ["%s"]
                *
                len(
                    cleaned_module_ids
                )
            )

            cursor.execute(
                f"""
                SELECT
                    module_id

                FROM modules

                WHERE is_active = TRUE
                  AND module_id IN (
                      {placeholders}
                  )
                """,
                tuple(
                    cleaned_module_ids
                )
            )

            valid_rows = (
                cursor.fetchall()
            )


            valid_module_ids = {
                row["module_id"]
                for row in valid_rows
            }


            requested_module_ids = set(
                cleaned_module_ids
            )


            if (
                valid_module_ids
                !=
                requested_module_ids
            ):

                return jsonify({
                    "success": False,
                    "message":
                        "One or more modules are invalid"
                }), 400


        # =========================
        # Replace Module Permissions
        # =========================

        cursor.execute(
            """
            DELETE FROM module_permissions

            WHERE user_id = %s
            """,
            (
                user_id,
            )
        )


        for module_id in cleaned_module_ids:

            cursor.execute(
                """
                INSERT INTO module_permissions
                (
                    user_id,
                    module_id,
                    access
                )

                VALUES
                (
                    %s,
                    %s,
                    TRUE
                )
                """,
                (
                    user_id,
                    module_id
                )
            )


        # =========================
        # Save Weekly Inventory
        # Feature Permission
        # =========================

        cursor.execute(
            """
            INSERT INTO permission_features
            (
                user_id,
                permission_key,
                access
            )

            VALUES
            (
                %s,
                %s,
                %s
            )

            ON DUPLICATE KEY UPDATE
                access = VALUES(access)
            """,
            (
                user_id,
                PERMISSION_WEEKLY_ACTUAL_STOCK,
                1
                if actual_stock_access
                else 0
            )
        )


        db.commit()


        return jsonify({
            "success": True,

            "message":
                "Permissions updated",

            "user_id":
                user_id,

            "module_ids":
                cleaned_module_ids,

            "feature_permissions": {
                PERMISSION_WEEKLY_ACTUAL_STOCK:
                    actual_stock_access
            }
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message":
                str(e)
        }), 500


    finally:

        cursor.close()
        db.close()
