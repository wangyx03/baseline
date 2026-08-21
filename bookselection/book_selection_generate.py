# -*- coding: utf-8 -*-

import re
from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from db import get_db

from permissions.permissions import (
    module_required,
    MODULE_BOOK_SELECTION,
)

from bookselection.book_allocation import (
    allocate,
    build_stock_check,
    build_summary,
    load_books_from_candidates,
)


book_selection_generate_bp = Blueprint(
    "book_selection_generate",
    __name__,
)


@book_selection_generate_bp.before_request
@module_required(
    MODULE_BOOK_SELECTION
)
def require_book_selection_access():
    pass


def _load_candidate_rows(
    cursor,
    batch_id: int,
):
    """
    Read the saved Candidate List for one batch.

    Candidate Stock is the inventory basis used by
    book_allocation.py.
    """

    cursor.execute(
        """
        SELECT
            c.sku,
            COALESCE(
                b.book_title,
                ''
            ) AS book_title,
            COALESCE(
                b.spice_type,
                ''
            ) AS spice_type,
            c.candidate_stock

        FROM book_selection_candidates c

        LEFT JOIN book_sku b
            ON b.isbn = c.sku

        WHERE c.batch_id = %s

        ORDER BY
            c.candidate_stock DESC,
            c.sku ASC
        """,
        (batch_id,),
    )

    return cursor.fetchall()


def _load_resident_by_store(
    cursor,
):
    """
    Return:
        {
            "VB": ["978...", ...],
            "TU": ["978...", ...]
        }

    All Resident Books participate.
    Shared Resident Books are allowed.
    """

    cursor.execute(
        """
        SELECT
            s.short_name AS store_code,
            r.sku

        FROM book_selection_resident r

        INNER JOIN stores s
            ON s.store_id = r.store_id

        ORDER BY
            s.short_name,
            r.resident_id
        """
    )

    rows = cursor.fetchall()

    result = {}

    for row in rows:

        store_code = str(
            row.get("store_code")
            or ""
        ).strip()

        sku = str(
            row.get("sku")
            or ""
        ).strip()

        if not store_code or not sku:
            continue

        result.setdefault(
            store_code,
            []
        ).append(sku)

    return result


def _load_batch(
    cursor,
    batch_id: int,
):
    cursor.execute(
        """
        SELECT
            batch_id,
            week_id,
            include_weekly_remaining,
            generated_at

        FROM book_selection_batches

        WHERE batch_id = %s

        LIMIT 1
        """,
        (batch_id,),
    )

    return cursor.fetchone()


def _validate_week_id(
    week_id: str,
) -> str:

    week_id = str(
        week_id
        or ""
    ).strip()

    match = re.fullmatch(
        r"(\d{4})-W(\d{2})",
        week_id,
    )

    if not match:
        raise ValueError(
            "Target Week must use YYYY-Www format."
        )

    year = int(
        match.group(1)
    )

    week = int(
        match.group(2)
    )

    try:
        date.fromisocalendar(
            year,
            week,
            1,
        )
    except ValueError:
        raise ValueError(
            "Target Week is not a valid ISO week."
        )

    return week_id


def _normalize_percent_dict(
    value,
    *,
    defaults,
):
    if value is None:
        value = {}

    if not isinstance(
        value,
        dict,
    ):
        raise ValueError(
            "spice_percent_by_store must be an object."
        )

    result = {}

    for store in (
        "VB",
        "TU",
    ):
        raw = value.get(
            store,
            defaults[store],
        )

        try:
            raw = int(raw)
        except (
            TypeError,
            ValueError,
        ):
            raise ValueError(
                f"spice_percent_by_store.{store} must be an integer."
            )

        if raw < 0 or raw > 100:
            raise ValueError(
                f"spice_percent_by_store.{store} must be between 0 and 100."
            )

        result[store] = raw

    return result


@book_selection_generate_bp.route(
    "/book-selection"
)
@login_required
def book_selection_page():

    return render_template(
        "book_selection_generate.html",
        extra_nav_links=[
            {
                "label": "Candidate List",
                "url": "/next-week-candidates"
            },
            {
                "label": "Resident Books",
                "url": "/resident-books"
            }
        ]
    )


@book_selection_generate_bp.route(
    "/api/book-selection/generate",
    methods=["POST"],
)
@login_required
def api_generate_book_selection():

    data = request.get_json(
        silent=True
    ) or {}

    batch_id = data.get(
        "batch_id"
    )

    books_per_store = (
        data.get(
            "books_per_store",
            {
                "VB": 100,
                "TU": 100,
            },
        )
    )

    per_store_week_qty = (
        data.get(
            "per_store_week_qty",
            {
                "VB": 5000,
                "TU": 5000,
            },
        )
    )

    min_reserve_qty = (
        data.get(
            "min_reserve_qty",
            5,
        )
    )

    target_week_id = str(
        data.get(
            "target_week_id",
            ""
        )
    ).strip()

    spice_percent_by_store = (
        data.get(
            "spice_percent_by_store",
            {
                "VB": 100,
                "TU": 0,
            },
        )
    )


    try:
        batch_id = int(batch_id)
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message":
                "batch_id is required and must be an integer.",
        }), 400

    try:
        min_reserve_qty = int(
            min_reserve_qty
        )
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message":
                "min_reserve_qty must be an integer.",
        }), 400

    if min_reserve_qty < 0:
        return jsonify({
            "success": False,
            "message":
                "min_reserve_qty cannot be negative.",
        }), 400

    try:
        target_week_id = (
            _validate_week_id(
                target_week_id
            )
        )

        normalized_spice = (
            _normalize_percent_dict(
                spice_percent_by_store,
                defaults={
                    "VB": 100,
                    "TU": 0,
                },
            )
        )

    except ValueError as e:
        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    if not isinstance(
        books_per_store,
        dict,
    ):
        return jsonify({
            "success": False,
            "message":
                "books_per_store must be an object.",
        }), 400

    if not isinstance(
        per_store_week_qty,
        dict,
    ):
        return jsonify({
            "success": False,
            "message":
                "per_store_week_qty must be an object.",
        }), 400

    normalized_books = {}
    normalized_qty = {}

    for store in (
        "VB",
        "TU",
    ):

        raw_books = books_per_store.get(
            store,
            100,
        )

        try:
            raw_books = int(raw_books)
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "message":
                    f"books_per_store.{store} must be an integer.",
            }), 400

        if raw_books < 0:
            return jsonify({
                "success": False,
                "message":
                    f"books_per_store.{store} cannot be negative.",
            }), 400

        normalized_books[
            store
        ] = raw_books

        raw_qty = per_store_week_qty.get(
            store,
            5000,
        )

        try:
            raw_qty = int(raw_qty)
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "message":
                    f"per_store_week_qty.{store} must be an integer.",
            }), 400

        if raw_qty < 0:
            return jsonify({
                "success": False,
                "message":
                    f"per_store_week_qty.{store} cannot be negative.",
            }), 400

        normalized_qty[
            store
        ] = raw_qty

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        batch = _load_batch(
            cursor,
            batch_id,
        )

        if not batch:
            return jsonify({
                "success": False,
                "message":
                    "Candidate Batch not found.",
            }), 404

        candidate_rows = (
            _load_candidate_rows(
                cursor,
                batch_id,
            )
        )

        if not candidate_rows:
            return jsonify({
                "success": False,
                "message":
                    "No Candidate Books found for this batch.",
            }), 400

        resident_by_store = (
            _load_resident_by_store(
                cursor
            )
        )

        books, dropped = (
            load_books_from_candidates(
                candidate_rows=
                    candidate_rows,
                min_reserve_qty=
                    min_reserve_qty,
            )
        )

        if not books:
            return jsonify({
                "success": False,
                "message":
                    "No eligible Candidate Books remain after reserve filtering.",
                "dropped":
                    dropped,
            }), 400

        (
            selected_by_store,
            detail_rows,
            warnings,
        ) = allocate(
            books=books,
            stores=[
                "VB",
                "TU",
            ],
            books_per_store=
                normalized_books,
            per_store_week_qty=
                normalized_qty,
            resident_by_store=
                resident_by_store,
            min_reserve_qty=
                min_reserve_qty,
            spice_percent_by_store=
                normalized_spice,
        )

        summary = build_summary(
            detail_rows=
                detail_rows,
            stores=[
                "VB",
                "TU",
            ],
        )

        stock_check = (
            build_stock_check(
                books=books,
                detail_rows=
                    detail_rows,
                min_reserve_qty=
                    min_reserve_qty,
            )
        )

        reserve_violations = [
            row
            for row in stock_check
            if not row.get(
                "reserve_ok"
            )
        ]

        if reserve_violations:
            return jsonify({
                "success": False,
                "message":
                    "Reserve validation failed.",
                "reserve_violations":
                    reserve_violations,
            }), 500

        store_items = {
            store: [
                row
                for row in detail_rows
                if row.get("store")
                == store
            ]
            for store in (
                "VB",
                "TU",
            )
        }

        return jsonify({
            "success": True,

            "batch": {
                "batch_id":
                    batch["batch_id"],
                "week_id":
                    batch["week_id"],
                "include_weekly_remaining":
                    bool(
                        batch[
                            "include_weekly_remaining"
                        ]
                    ),
            },

            "settings": {
                "books_per_store":
                    normalized_books,
                "per_store_week_qty":
                    normalized_qty,
                "min_reserve_qty":
                    min_reserve_qty,
                "target_week_id":
                    target_week_id,
                "spice_percent_by_store":
                    normalized_spice,
            },

            "target_week_id":
                target_week_id,

            "resident_by_store":
                resident_by_store,

            "eligible_candidate_count":
                len(books),

            "dropped_candidate_count":
                len(dropped),

            "dropped_candidates":
                dropped,

            "summary":
                summary,

            "items":
                detail_rows,

            "items_by_store":
                store_items,

            "stock_check":
                stock_check,

            "warnings":
                warnings,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()



@book_selection_generate_bp.route(
    "/api/book-selection/weekly-inventory",
    methods=["GET"],
)
@login_required
def api_book_selection_weekly_inventory():

    week_id = str(
        request.args.get(
            "week_id",
            ""
        )
    ).strip()

    batch_id = request.args.get(
        "batch_id",
        type=int,
    )

    try:
        week_id = _validate_week_id(
            week_id
        )
    except ValueError as e:
        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                wi.store_id,
                COALESCE(
                    s.short_name,
                    ''
                ) AS store,
                wi.sku,
                wi.planned_qty,
                COALESCE(
                    bs.book_title,
                    ''
                ) AS book_title,
                COALESCE(
                    bs.spice_type,
                    ''
                ) AS spice_type,
                c.candidate_stock,

                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM book_selection_resident r
                        WHERE r.store_id = wi.store_id
                          AND r.sku = wi.sku
                    )
                    THEN 'Resident'
                    ELSE 'Regular'
                END AS role

            FROM weekly_inventory wi

            INNER JOIN stores s
                ON s.store_id =
                   wi.store_id

            LEFT JOIN book_sku bs
                ON bs.isbn =
                   wi.sku

            LEFT JOIN book_selection_candidates c
                ON c.batch_id = %s
               AND c.sku =
                   wi.sku

            WHERE wi.week_id = %s

            ORDER BY
                s.short_name,
                wi.weekly_inventory_id
            """,
            (
                batch_id,
                week_id,
            )
        )

        rows = cursor.fetchall()

        sku_store_count = {}

        for row in rows:
            sku = str(
                row.get("sku")
                or ""
            )

            sku_store_count.setdefault(
                sku,
                set(),
            ).add(
                row.get("store")
            )

        items_by_store = {
            "VB": [],
            "TU": [],
        }

        for row in rows:

            store = str(
                row.get("store")
                or ""
            ).strip()

            row["shared_across_store"] = (
                len(
                    sku_store_count.get(
                        row["sku"],
                        set(),
                    )
                )
                > 1
            )

            row["fallback_duplicate"] = (
                row["shared_across_store"]
            )

            if store in items_by_store:
                items_by_store[
                    store
                ].append(
                    row
                )

        return jsonify({
            "success": True,
            "exists":
                bool(rows),
            "week_id":
                week_id,
            "count":
                len(rows),
            "items_by_store":
                items_by_store,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()


@book_selection_generate_bp.route(
    "/api/book-selection/lookup-sku",
    methods=["GET"],
)
@login_required
def api_book_selection_lookup_sku():

    batch_id = request.args.get(
        "batch_id",
        type=int,
    )

    store_code = str(
        request.args.get(
            "store",
            ""
        )
    ).strip()

    sku = str(
        request.args.get(
            "sku",
            ""
        )
    ).strip()

    if not batch_id:
        return jsonify({
            "success": False,
            "message":
                "Candidate Batch is required.",
        }), 400

    if store_code not in (
        "VB",
        "TU",
    ):
        return jsonify({
            "success": False,
            "message":
                "Store must be VB or TU.",
        }), 400

    if (
        len(sku) != 13
        or
        not sku.isdigit()
    ):
        return jsonify({
            "success": False,
            "message":
                "SKU must be a 13-digit ISBN.",
        }), 400

    db = get_db()

    cursor = db.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                c.sku,
                c.candidate_stock,
                COALESCE(
                    bs.book_title,
                    ''
                ) AS book_title,
                COALESCE(
                    bs.spice_type,
                    ''
                ) AS spice_type,

                CASE
                    WHEN EXISTS (
                        SELECT 1

                        FROM book_selection_resident r

                        INNER JOIN stores rs
                            ON rs.store_id =
                               r.store_id

                        WHERE rs.short_name = %s
                          AND r.sku = c.sku
                    )
                    THEN 'Resident'
                    ELSE 'Regular'
                END AS role

            FROM book_selection_candidates c

            LEFT JOIN book_sku bs
                ON bs.isbn =
                   c.sku

            WHERE c.batch_id = %s
              AND c.sku = %s

            LIMIT 1
            """,
            (
                store_code,
                batch_id,
                sku,
            )
        )

        item = cursor.fetchone()

        if not item:
            return jsonify({
                "success": False,
                "message":
                    (
                        "This SKU is not in the selected "
                        "Candidate Batch."
                    ),
            }), 404

        return jsonify({
            "success": True,
            "item": item,
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()


@book_selection_generate_bp.route(
    "/api/book-selection/save-weekly-inventory",
    methods=["POST"],
)
@login_required
def api_save_weekly_inventory():

    data = request.get_json(
        silent=True
    ) or {}

    batch_id = data.get(
        "batch_id"
    )

    manual_items = data.get(
        "items"
    )

    overwrite = bool(
        data.get(
            "overwrite",
            False,
        )
    )

    try:
        batch_id = int(
            batch_id
        )
    except (
        TypeError,
        ValueError,
    ):
        return jsonify({
            "success": False,
            "message":
                "batch_id is required and must be an integer.",
        }), 400

    try:
        target_week_id = (
            _validate_week_id(
                data.get(
                    "target_week_id"
                )
            )
        )

        min_reserve_qty = int(
            data.get(
                "min_reserve_qty",
                5,
            )
        )

        if min_reserve_qty < 0:
            raise ValueError(
                "min_reserve_qty cannot be negative."
            )

        normalized_spice = (
            _normalize_percent_dict(
                data.get(
                    "spice_percent_by_store"
                ),
                defaults={
                    "VB": 100,
                    "TU": 0,
                },
            )
        )

    except (
        ValueError,
        TypeError,
    ) as e:
        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    books_per_store = (
        data.get(
            "books_per_store",
            {
                "VB": 100,
                "TU": 100,
            },
        )
    )

    per_store_week_qty = (
        data.get(
            "per_store_week_qty",
            {
                "VB": 5000,
                "TU": 5000,
            },
        )
    )

    if not isinstance(
        books_per_store,
        dict,
    ):
        return jsonify({
            "success": False,
            "message":
                "books_per_store must be an object.",
        }), 400

    if not isinstance(
        per_store_week_qty,
        dict,
    ):
        return jsonify({
            "success": False,
            "message":
                "per_store_week_qty must be an object.",
        }), 400

    normalized_books = {}
    normalized_qty = {}

    for store in (
        "VB",
        "TU",
    ):
        try:
            normalized_books[store] = int(
                books_per_store.get(
                    store,
                    100,
                )
            )

            normalized_qty[store] = int(
                per_store_week_qty.get(
                    store,
                    5000,
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            return jsonify({
                "success": False,
                "message":
                    f"Invalid Books or Planned Qty for {store}.",
            }), 400

        if (
            normalized_books[store] < 0
            or
            normalized_qty[store] < 0
        ):
            return jsonify({
                "success": False,
                "message":
                    f"Books and Planned Qty for {store} cannot be negative.",
            }), 400

    db = get_db()
    cursor = db.cursor(
        dictionary=True
    )

    try:

        batch = _load_batch(
            cursor,
            batch_id,
        )

        if not batch:
            return jsonify({
                "success": False,
                "message":
                    "Candidate Batch not found.",
            }), 404

        candidate_rows = (
            _load_candidate_rows(
                cursor,
                batch_id,
            )
        )

        if not candidate_rows:
            return jsonify({
                "success": False,
                "message":
                    "No Candidate Books found for this batch.",
            }), 400

        candidate_by_sku = {
            str(
                row.get("sku")
                or ""
            ).strip(): row
            for row in candidate_rows
        }

        warnings = []

        if manual_items is not None:

            if not isinstance(
                manual_items,
                list,
            ):
                return jsonify({
                    "success": False,
                    "message":
                        "items must be a list.",
                }), 400

            detail_rows = []

            seen_store_sku = set()
            planned_by_sku = {}

            for index, raw in enumerate(
                manual_items,
                start=1,
            ):

                if not isinstance(
                    raw,
                    dict,
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            f"Invalid item at row {index}.",
                    }), 400

                store = str(
                    raw.get("store")
                    or ""
                ).strip()

                sku = str(
                    raw.get("sku")
                    or ""
                ).strip()

                try:
                    planned_qty = int(
                        raw.get(
                            "planned_qty",
                            0,
                        )
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"Invalid Planned Qty for "
                                f"row {index}."
                            ),
                    }), 400

                if store not in (
                    "VB",
                    "TU",
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            f"Invalid store at row {index}.",
                    }), 400

                if (
                    len(sku) != 13
                    or
                    not sku.isdigit()
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"SKU at row {index} must "
                                f"be a 13-digit ISBN."
                            ),
                    }), 400

                if planned_qty <= 0:
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"Planned Qty for {store} "
                                f"{sku} must be greater than 0."
                            ),
                    }), 400

                key = (
                    store,
                    sku,
                )

                if key in seen_store_sku:
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"Duplicate SKU {sku} in "
                                f"{store}."
                            ),
                    }), 400

                seen_store_sku.add(
                    key
                )

                candidate = (
                    candidate_by_sku.get(
                        sku
                    )
                )

                if not candidate:
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"{store} SKU {sku} is not "
                                f"in Candidate Batch #{batch_id}."
                            ),
                    }), 400

                candidate_stock = int(
                    candidate.get(
                        "candidate_stock",
                        0,
                    )
                    or 0
                )

                if (
                    candidate_stock
                    <=
                    min_reserve_qty
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"{store} SKU {sku} has "
                                f"Candidate Stock {candidate_stock}, "
                                f"which is not above reserve "
                                f"{min_reserve_qty}."
                            ),
                    }), 400

                planned_by_sku[
                    sku
                ] = (
                    planned_by_sku.get(
                        sku,
                        0,
                    )
                    +
                    planned_qty
                )

                detail_rows.append({
                    "store":
                        store,
                    "sku":
                        sku,
                    "planned_qty":
                        planned_qty,
                    "candidate_stock":
                        candidate_stock,
                })

            # One SKU has one shared physical inventory pool.
            for sku, total_planned in (
                planned_by_sku.items()
            ):

                candidate_stock = int(
                    candidate_by_sku[
                        sku
                    ].get(
                        "candidate_stock",
                        0,
                    )
                    or 0
                )

                final_stock = (
                    candidate_stock
                    -
                    total_planned
                )

                if (
                    final_stock
                    <
                    min_reserve_qty
                ):
                    return jsonify({
                        "success": False,
                        "message":
                            (
                                f"SKU {sku} would leave "
                                f"{final_stock} book(s). "
                                f"Minimum reserve is "
                                f"{min_reserve_qty}."
                            ),
                    }), 400

        else:

            resident_by_store = (
                _load_resident_by_store(
                    cursor
                )
            )

            books, _dropped = (
                load_books_from_candidates(
                    candidate_rows=
                        candidate_rows,
                    min_reserve_qty=
                        min_reserve_qty,
                )
            )

            (
                _selected_by_store,
                detail_rows,
                warnings,
            ) = allocate(
                books=books,
                stores=[
                    "VB",
                    "TU",
                ],
                books_per_store=
                    normalized_books,
                per_store_week_qty=
                    normalized_qty,
                resident_by_store=
                    resident_by_store,
                min_reserve_qty=
                    min_reserve_qty,
                spice_percent_by_store=
                    normalized_spice,
            )

            stock_check = (
                build_stock_check(
                    books=books,
                    detail_rows=
                        detail_rows,
                    min_reserve_qty=
                        min_reserve_qty,
                )
            )

            if any(
                not row.get(
                    "reserve_ok"
                )
                for row in stock_check
            ):
                return jsonify({
                    "success": False,
                    "message":
                        "Reserve validation failed.",
                }), 500

        cursor.execute(
            """
            SELECT
                store_id,
                short_name
            FROM stores
            WHERE short_name IN ('VB', 'TU')
            """
        )

        store_rows = (
            cursor.fetchall()
        )

        store_id_by_code = {
            str(
                row["short_name"]
            ).strip(): int(
                row["store_id"]
            )
            for row in store_rows
        }

        for required in (
            "VB",
            "TU",
        ):
            if (
                required
                not in store_id_by_code
            ):
                raise ValueError(
                    f"Store {required} was not found."
                )

        # -----------------------------------------------------
        # Existing target week:
        # first Confirm must explicitly approve replacement.
        # -----------------------------------------------------

        cursor.execute(
            """
            SELECT
                COUNT(*) AS row_count
            FROM weekly_inventory
            WHERE week_id = %s
            """,
            (
                target_week_id,
            )
        )

        existing_week = (
            cursor.fetchone()
            or {}
        )

        existing_count = int(
            existing_week.get(
                "row_count",
                0,
            )
            or 0
        )

        if (
            existing_count > 0
            and
            not overwrite
        ):
            db.rollback()

            return jsonify({
                "success": False,
                "requires_confirmation": True,
                "target_week_id":
                    target_week_id,
                "existing_count":
                    existing_count,
                "message":
                    (
                        f"{target_week_id} already has "
                        f"{existing_count} Weekly Inventory row(s). "
                        f"Confirm again to replace this week."
                    ),
            }), 409

        # Replace ONLY the selected target week.
        # Other weeks remain untouched.
        cursor.execute(
            """
            DELETE FROM weekly_inventory
            WHERE week_id = %s
            """,
            (
                target_week_id,
            )
        )

        rows_to_insert = [
            (
                target_week_id,
                store_id_by_code[
                    row["store"]
                ],
                row["sku"],
                int(
                    row.get(
                        "planned_qty",
                        0,
                    )
                ),
            )
            for row in detail_rows
        ]

        if rows_to_insert:
            cursor.executemany(
                """
                INSERT INTO weekly_inventory
                (
                    week_id,
                    store_id,
                    sku,
                    planned_qty
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                rows_to_insert,
            )

        db.commit()

        return jsonify({
            "success": True,
            "target_week_id":
                target_week_id,
            "saved_count":
                len(
                    rows_to_insert
                ),
            "warnings":
                warnings,
            "message":
                f"Saved {len(rows_to_insert)} row(s) to {target_week_id}.",
        })

    except ValueError as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e),
        }), 400

    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e),
        }), 500

    finally:

        cursor.close()
        db.close()
