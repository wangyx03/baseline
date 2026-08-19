def get_weekly_item_statuses(
    cursor,
    week_id,
    store_id,
    skus
):
    """
    Return weekly inventory status for many SKUs in one database query.

    Result:
        {
            "sku": {
                "in_weekly_inventory": True,
                "planned": ...,
                "used": ...,
                "remaining": ...
            }
        }

    SKUs that are not in weekly_inventory are returned with
    in_weekly_inventory=False.
    """

    unique_skus = list(dict.fromkeys(
        str(sku).strip()
        for sku in skus
        if sku is not None and str(sku).strip()
    ))

    if not unique_skus:
        return {}

    placeholders = ", ".join(["%s"] * len(unique_skus))

    sql = f"""
        SELECT
            wi.sku,
            wi.planned_qty AS planned,
            (
                COALESCE(locked_data.locked_used, 0)
                +
                COALESCE(draft_data.draft_used, 0)
            ) AS used

        FROM weekly_inventory wi

        LEFT JOIN (
            SELECT
                il.sku,
                SUM(il.quantity) AS locked_used

            FROM inventory_locked il

            WHERE il.week_id = %s
              AND il.store_id = %s
              AND il.sku IN ({placeholders})

            GROUP BY il.sku
        ) locked_data
            ON locked_data.sku = wi.sku

        LEFT JOIN (
            SELECT
                sr.sku,
                SUM(sr.quantity) AS draft_used

            FROM sku_recording sr

            LEFT JOIN (
                SELECT DISTINCT
                    week_id,
                    store_id,
                    live_id

                FROM inventory_locked

                WHERE week_id = %s
                  AND store_id = %s
            ) locked_live
                ON locked_live.week_id = sr.week_id
                AND locked_live.store_id = sr.store_id
                AND locked_live.live_id = sr.live_id

            WHERE sr.week_id = %s
              AND sr.store_id = %s
              AND sr.sku IN ({placeholders})
              AND locked_live.live_id IS NULL

            GROUP BY sr.sku
        ) draft_data
            ON draft_data.sku = wi.sku

        WHERE wi.week_id = %s
          AND wi.store_id = %s
          AND wi.sku IN ({placeholders})
    """

    params = (
        [week_id, store_id]
        + unique_skus
        + [week_id, store_id, week_id, store_id]
        + unique_skus
        + [week_id, store_id]
        + unique_skus
    )

    cursor.execute(sql, params)
    rows = cursor.fetchall()

    statuses = {
        sku: {
            "in_weekly_inventory": False,
            "planned": 0,
            "used": 0,
            "remaining": 0
        }
        for sku in unique_skus
    }

    for row in rows:
        sku = str(row["sku"])
        planned = int(row["planned"] or 0)
        used = int(row["used"] or 0)

        statuses[sku] = {
            "in_weekly_inventory": True,
            "planned": planned,
            "used": used,
            "remaining": planned - used
        }

    return statuses


def get_weekly_item_status(
    cursor,
    week_id,
    store_id,
    sku
):
    """
    Backward-compatible single-SKU helper.
    Existing record/insert APIs can continue using this function.
    """

    statuses = get_weekly_item_statuses(
        cursor=cursor,
        week_id=week_id,
        store_id=store_id,
        skus=[sku]
    )

    return statuses.get(
        str(sku).strip(),
        {
            "in_weekly_inventory": False,
            "planned": 0,
            "used": 0,
            "remaining": 0
        }
    )
