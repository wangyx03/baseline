# -*- coding: utf-8 -*-

import json
from typing import Iterable, Optional


def write_recording_log(
    cursor,
    *,
    week_id: str,
    store_id: int,
    live_id: str,
    action_type: str,
    operator_user_id: Optional[int],
    changes: Iterable[dict],
) -> dict:
    """
    Write one operation-level Recording log row.

    Detailed SKU / recording changes are stored in change_data JSON.

    Important:
        - Does NOT open a database connection.
        - Does NOT commit or rollback.
        - The caller controls the transaction.
    """

    week_id = str(week_id or "").strip()
    live_id = str(live_id or "").strip()
    action_type = str(action_type or "").strip().upper()

    if not week_id:
        raise ValueError("week_id is required for recording log.")

    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError("store_id must be an integer for recording log.")

    if not live_id:
        raise ValueError("live_id is required for recording log.")

    if not action_type:
        raise ValueError("action_type is required for recording log.")

    if operator_user_id is not None:
        try:
            operator_user_id = int(operator_user_id)
        except (TypeError, ValueError):
            raise ValueError(
                "operator_user_id must be an integer or None."
            )

    normalized_changes = [
        dict(change)
        for change in (changes or [])
        if isinstance(change, dict)
    ]

    if not normalized_changes:
        return {
            "written": False,
            "log_id": None,
            "change_count": 0,
        }

    payload = {
        "changes": normalized_changes
    }

    cursor.execute(
        """
        INSERT INTO sku_recording_log
        (
            action_type,
            week_id,
            store_id,
            live_id,
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
            %s,
            %s,
            %s
        )
        """,
        (
            action_type,
            week_id,
            store_id,
            live_id,
            operator_user_id,
            len(normalized_changes),
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )

    return {
        "written": True,
        "log_id": cursor.lastrowid,
        "change_count": len(normalized_changes),
    }
