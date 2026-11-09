def get_weekly_item_statuses(
    cursor,
    week_id,
    store_id,
    skus,
    live_id=None
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

    source = "M6"
    if live_id is not None:
        cursor.execute("""
            SELECT inventory_source FROM live_sessions
            WHERE week_id=%s AND store_id=%s AND live_id=%s LIMIT 1
        """, (week_id, store_id, live_id))
        row = cursor.fetchone()
        source = row["inventory_source"] if row else "OFFICE"
    if source == "OFFICE":
        return {str(s): {"in_weekly_inventory": False, "planned": 0,
                         "used": 0, "remaining": 0} for s in skus}

    unique_skus = list(dict.fromkeys(
        str(sku).strip()
        for sku in skus
        if sku is not None and str(sku).strip()
    ))

    if not unique_skus:
        return {}

    if source == "WOOKOO":
        # Same rule as /api/wookoo-inventory:
        # locked LIVE -> inventory_locked, unlocked LIVE -> sku_recording.
        placeholders = ", ".join(["%s"] * len(unique_skus))
        cursor.execute(f"""
            SELECT w.sku, w.planned, COALESCE(u.used, 0) AS used
            FROM (
                SELECT sku, SUM(planned_qty) AS planned
                FROM wookoo_inventory
                WHERE store_id = %s AND sku IN ({placeholders})
                GROUP BY sku
            ) w
            LEFT JOIN (
                SELECT sku, SUM(qty) AS used
                FROM (
                    SELECT il.sku, il.quantity AS qty
                    FROM inventory_locked il
                    INNER JOIN live_sessions ls
                        ON ls.week_id = il.week_id
                       AND ls.store_id = il.store_id
                       AND ls.live_id = il.live_id
                       AND ls.inventory_source = 'WOOKOO'
                    WHERE il.store_id = %s AND il.sku IN ({placeholders})

                    UNION ALL

                    SELECT sr.sku, sr.quantity AS qty
                    FROM sku_recording sr
                    INNER JOIN live_sessions ls
                        ON ls.week_id = sr.week_id
                       AND ls.store_id = sr.store_id
                       AND ls.live_id = sr.live_id
                       AND ls.inventory_source = 'WOOKOO'
                    WHERE sr.store_id = %s AND sr.sku IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM inventory_locked il2
                          WHERE il2.week_id = sr.week_id
                            AND il2.store_id = sr.store_id
                            AND il2.live_id = sr.live_id
                      )
                ) x
                GROUP BY sku
            ) u ON u.sku = w.sku
        """, [store_id] + unique_skus
             + [store_id] + unique_skus
             + [store_id] + unique_skus)
        result = {sku: {"in_weekly_inventory": False, "planned": 0,
                        "used": 0, "remaining": 0} for sku in unique_skus}
        for row in cursor.fetchall():
            planned, used = int(row["planned"] or 0), int(row["used"] or 0)
            result[str(row["sku"])] = {"in_weekly_inventory": True,
                "planned": planned, "used": used, "remaining": planned-used}
        return result

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

            LEFT JOIN live_sessions ls
                ON ls.week_id = il.week_id
                AND ls.store_id = il.store_id
                AND ls.live_id = il.live_id

            WHERE il.week_id = %s
              AND il.store_id = %s
              AND il.sku IN ({placeholders})
              -- Only M6 LIVEs; locked rows without a session are historical M6.
              AND COALESCE(ls.inventory_source, 'M6') = 'M6'

            GROUP BY il.sku
        ) locked_data
            ON locked_data.sku = wi.sku

        LEFT JOIN (
            SELECT
                sr.sku,
                SUM(sr.quantity) AS draft_used

            FROM sku_recording sr

            INNER JOIN live_sessions ls
                ON ls.week_id = sr.week_id
                AND ls.store_id = sr.store_id
                AND ls.live_id = sr.live_id
                AND ls.inventory_source = 'M6'

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
    sku,
    live_id=None
):
    """
    Backward-compatible single-SKU helper.
    Existing record/insert APIs can continue using this function.
    """

    statuses = get_weekly_item_statuses(
        cursor=cursor,
        week_id=week_id,
        store_id=store_id,
        skus=[sku],
        live_id=live_id
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
