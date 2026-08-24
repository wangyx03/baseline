# -*- coding: utf-8 -*-

import json
from typing import Dict, Iterable, List, Optional, Tuple


def _normalize_rows(
    rows: Optional[Iterable[dict]]
) -> Dict[Tuple[int, str], int]:
    """
    Convert weekly inventory rows into:

        {
            (store_id, sku): planned_qty
        }

    Duplicate (store_id, sku) rows are summed defensively.
    """

    result: Dict[Tuple[int, str], int] = {}

    for row in rows or []:

        if not isinstance(row, dict):
            continue

        store_id = row.get("store_id")

        sku = str(
            row.get("sku")
            or ""
        ).strip()

        try:
            store_id = int(store_id)

            planned_qty = int(
                row.get(
                    "planned_qty",
                    0
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if (
            store_id <= 0
            or
            not sku
        ):
            continue

        key = (
            store_id,
            sku
        )

        result[key] = (
            result.get(
                key,
                0
            )
            +
            planned_qty
        )

    return result


def build_weekly_inventory_changes(
    old_rows: Optional[Iterable[dict]],
    new_rows: Optional[Iterable[dict]],
) -> List[dict]:
    """
    Compare old and new weekly_inventory data.

    Returns only actual changes.

    change_type:
        INSERT
        UPDATE
        DELETE
    """

    old_map = _normalize_rows(
        old_rows
    )

    new_map = _normalize_rows(
        new_rows
    )

    all_keys = sorted(
        set(old_map.keys())
        |
        set(new_map.keys())
    )

    changes: List[dict] = []

    for store_id, sku in all_keys:

        key = (
            store_id,
            sku
        )

        old_exists = (
            key in old_map
        )

        new_exists = (
            key in new_map
        )

        old_qty = (
            old_map.get(key)
            if old_exists
            else None
        )

        new_qty = (
            new_map.get(key)
            if new_exists
            else None
        )

        # Existing row changed
        if (
            old_exists
            and
            new_exists
        ):

            if old_qty == new_qty:
                continue

            changes.append({
                "store_id":
                    store_id,
                "sku":
                    sku,
                "change_type":
                    "UPDATE",
                "old_planned_qty":
                    old_qty,
                "new_planned_qty":
                    new_qty,
            })

            continue

        # New row
        if (
            not old_exists
            and
            new_exists
        ):

            changes.append({
                "store_id":
                    store_id,
                "sku":
                    sku,
                "change_type":
                    "INSERT",
                "old_planned_qty":
                    None,
                "new_planned_qty":
                    new_qty,
            })

            continue

        # Deleted row
        if (
            old_exists
            and
            not new_exists
        ):

            changes.append({
                "store_id":
                    store_id,
                "sku":
                    sku,
                "change_type":
                    "DELETE",
                "old_planned_qty":
                    old_qty,
                "new_planned_qty":
                    None,
            })

    return changes


def write_weekly_inventory_log(
    cursor,
    *,
    week_id: str,
    operator_user_id: Optional[int],
    old_rows: Optional[Iterable[dict]],
    new_rows: Optional[Iterable[dict]],
    action: str = "CONFIRM",
) -> dict:
    """
    Write one operation-level weekly_inventory log.

    Important:
        - does NOT open a DB connection
        - does NOT commit
        - does NOT rollback

    The caller controls the transaction.
    """

    week_id = str(
        week_id
        or ""
    ).strip()

    action = str(
        action
        or "CONFIRM"
    ).strip().upper()

    if not week_id:
        raise ValueError(
            "week_id is required."
        )

    if operator_user_id is not None:

        try:
            operator_user_id = int(
                operator_user_id
            )

        except (
            TypeError,
            ValueError,
        ):
            raise ValueError(
                "operator_user_id must be an integer or None."
            )

    changes = (
        build_weekly_inventory_changes(
            old_rows=old_rows,
            new_rows=new_rows,
        )
    )

    change_count = len(
        changes
    )

    # No actual change -> no log
    if change_count == 0:

        return {
            "written":
                False,
            "log_id":
                None,
            "change_count":
                0,
            "changes":
                [],
        }

    change_data = {
        "changes":
            changes
    }

    cursor.execute(
        """
        INSERT INTO weekly_inventory_log
        (
            week_id,
            action,
            operator_user_id,
            change_count,
            change_data
        )
        VALUES
        (
            %s,
            %s,
            %s,
            %s,
            %s
        )
        """,
        (
            week_id,
            action,
            operator_user_id,
            change_count,
            json.dumps(
                change_data,
                ensure_ascii=False,
                separators=(",", ":")
            ),
        )
    )

    return {
        "written":
            True,
        "log_id":
            cursor.lastrowid,
        "change_count":
            change_count,
        "changes":
            changes,
    }