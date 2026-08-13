def get_weekly_item_status(
    cursor,
    week_id,
    store_id,
    sku
):

    cursor.execute(
        """
        SELECT
            wi.planned_qty AS planned,

            (
                COALESCE(
                    locked_data.locked_used,
                    0
                )
                +
                COALESCE(
                    draft_data.draft_used,
                    0
                )
            ) AS used

        FROM weekly_inventory wi

        LEFT JOIN (
            SELECT
                week_id,
                store_id,
                sku,
                SUM(quantity) AS locked_used

            FROM inventory_locked

            GROUP BY
                week_id,
                store_id,
                sku

        ) locked_data

            ON locked_data.week_id = wi.week_id
            AND locked_data.store_id = wi.store_id
            AND locked_data.sku = wi.sku

        LEFT JOIN (
            SELECT
                sr.week_id,
                sr.store_id,
                sr.sku,
                SUM(sr.quantity) AS draft_used

            FROM sku_recording sr

            WHERE NOT EXISTS (
                SELECT 1

                FROM inventory_locked il2

                WHERE il2.week_id = sr.week_id
                  AND il2.store_id = sr.store_id
                  AND il2.live_id = sr.live_id
            )

            GROUP BY
                sr.week_id,
                sr.store_id,
                sr.sku

        ) draft_data

            ON draft_data.week_id = wi.week_id
            AND draft_data.store_id = wi.store_id
            AND draft_data.sku = wi.sku

        WHERE wi.week_id = %s
          AND wi.store_id = %s
          AND wi.sku = %s

        LIMIT 1
        """,
        (
            week_id,
            store_id,
            sku
        )
    )

    row = cursor.fetchone()

    if row is None:

        return {
            "in_weekly_inventory": False,
            "planned": 0,
            "used": 0,
            "remaining": 0
        }

    planned = int(
        row["planned"]
        or 0
    )

    used = int(
        row["used"]
        or 0
    )

    return {
        "in_weekly_inventory": True,
        "planned": planned,
        "used": used,
        "remaining": planned - used
    }
