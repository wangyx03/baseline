# -*- coding: utf-8 -*-
"""
Book Selection Allocation

Current business rules:

1. Inventory basis is book_selection_candidates.candidate_stock.
2. Only candidate_stock > min_reserve_qty participates.
3. Across ALL stores, total planned_qty for one SKU must leave at least
   min_reserve_qty units.
4. Resident Books may be shared across stores.
5. VB is selected first and prefers Spice.
6. TU is selected second and prefers Non-Spice.
7. Spice / Non-Spice preferences are SOFT preferences:
   if preferred books cannot support the store's planned_qty target,
   higher-capacity books of the other type may replace them.
8. Regular books avoid cross-store duplicates first.
   Cross-store duplicates are allowed as fallback.
9. No previous-week repeat rule.
10. No resident_ratio.
11. Resident and Regular books use the same planned_qty allocation logic.
12. Each store's book-count target and planned_qty target may be configured
    independently.
13. If every fallback has been used and a store still cannot reach its
    planned_qty target, allocate as much as possible and return a warning.

This module contains only core allocation logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union


@dataclass(frozen=True)
class Book:
    sku: str
    name: str
    spice: str
    candidate_stock: int

    @property
    def is_spice(self) -> bool:
        value = str(self.spice or "").strip().lower()
        return (
            value.startswith("spice")
            and not value.startswith("non")
        )

    def sellable_capacity(
        self,
        min_reserve_qty: int,
    ) -> int:
        return max(
            0,
            int(self.candidate_stock)
            - int(min_reserve_qty),
        )


@dataclass
class SelectedBook:
    store: str
    book: Book
    role: str  # Resident / Regular
    planned_qty: int = 0
    duplicate_across_store: bool = False


# =========================================================
# Candidate loading
# =========================================================

def load_books_from_candidates(
    candidate_rows: Iterable[dict],
    min_reserve_qty: int = 5,
) -> Tuple[List[Book], List[Tuple[str, str, str]]]:

    books: List[Book] = []
    dropped: List[Tuple[str, str, str]] = []
    seen = set()

    for row in candidate_rows or []:

        sku = str(
            row.get("sku")
            or ""
        ).strip()

        name = str(
            row.get("book_title")
            or row.get("name")
            or ""
        ).strip()

        spice = str(
            row.get("spice_type")
            or row.get("spice")
            or ""
        ).strip()

        if not sku:
            continue

        if sku in seen:
            dropped.append(
                (
                    sku,
                    name,
                    "Duplicate SKU",
                )
            )
            continue

        seen.add(sku)

        try:
            candidate_stock = int(
                row.get(
                    "candidate_stock",
                    0,
                )
            )
        except (TypeError, ValueError):
            dropped.append(
                (
                    sku,
                    name,
                    "Invalid candidate_stock",
                )
            )
            continue

        if candidate_stock <= min_reserve_qty:
            dropped.append(
                (
                    sku,
                    name,
                    (
                        f"candidate_stock={candidate_stock} "
                        f"<= reserve={min_reserve_qty}"
                    ),
                )
            )
            continue

        books.append(
            Book(
                sku=sku,
                name=name,
                spice=spice,
                candidate_stock=
                    candidate_stock,
            )
        )

    return books, dropped


# =========================================================
# Normalization
# =========================================================

def _store_priority(
    stores: Sequence[str],
) -> List[str]:

    unique = []
    seen = set()

    for raw_store in stores:
        store = str(
            raw_store
        ).strip()

        if (
            store
            and
            store not in seen
        ):
            seen.add(store)
            unique.append(store)

    result = []

    # Fixed business priority:
    # satisfy VB first, then TU.
    for store in (
        "VB",
        "TU",
    ):
        if store in seen:
            result.append(store)

    result.extend(
        store
        for store in unique
        if store not in {
            "VB",
            "TU",
        }
    )

    return result


def _normalize_store_targets(
    stores: Sequence[str],
    value: Union[
        int,
        Dict[str, int],
    ],
    *,
    field_name: str,
) -> Dict[str, int]:

    result = {}

    if isinstance(
        value,
        dict,
    ):
        for store in stores:
            try:
                number = int(
                    value.get(
                        store,
                        0,
                    )
                )
            except (
                TypeError,
                ValueError,
            ):
                raise ValueError(
                    f"{field_name}.{store} "
                    f"must be an integer."
                )

            if number < 0:
                raise ValueError(
                    f"{field_name}.{store} "
                    f"cannot be negative."
                )

            result[store] = number

        return result

    try:
        number = int(value)
    except (
        TypeError,
        ValueError,
    ):
        raise ValueError(
            f"{field_name} must be "
            f"an integer or dict."
        )

    if number < 0:
        raise ValueError(
            f"{field_name} cannot "
            f"be negative."
        )

    return {
        store: number
        for store in stores
    }


def _normalize_spice_targets(
    stores: Sequence[str],
    value: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """
    Spice target is a soft percentage target for selected book titles.

    Defaults preserve the current business preference:
        VB = 100% Spice
        TU = 0% Spice
    """
    defaults = {
        "VB": 100,
        "TU": 0,
    }

    value = value or {}
    result = {}

    for store in stores:
        raw = value.get(
            store,
            defaults.get(store, 0),
        )

        try:
            percent = int(raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"spice_percent_by_store.{store} must be an integer."
            )

        if percent < 0 or percent > 100:
            raise ValueError(
                f"spice_percent_by_store.{store} must be between 0 and 100."
            )

        result[store] = percent

    return result


def _prefer_spice(
    store: str,
) -> bool:
    # VB -> Spice
    # TU -> Non-Spice
    return (
        str(store)
        .strip()
        .upper()
        ==
        "VB"
    )


# =========================================================
# Selection helpers
# =========================================================

def _capacity(
    book: Book,
    reserve: int,
) -> int:
    return book.sellable_capacity(
        reserve
    )


def _sort_preferred(
    books: Iterable[Book],
    prefer_spice: bool,
    reserve: int,
) -> List[Book]:
    """
    Preferred category first.
    Inside each category, higher capacity first.
    """

    return sorted(
        books,
        key=lambda book: (
            0
            if (
                book.is_spice
                ==
                prefer_spice
            )
            else 1,
            -_capacity(
                book,
                reserve,
            ),
            book.sku,
        ),
    )


def _store_selected_skus(
    selected: Dict[
        str,
        List[SelectedBook],
    ],
    store: str,
) -> set:
    return {
        item.book.sku
        for item in selected.get(
            store,
            []
        )
    }


def _other_store_skus(
    selected: Dict[
        str,
        List[SelectedBook],
    ],
    store: str,
) -> set:
    return {
        item.book.sku
        for other_store, items
        in selected.items()
        if other_store != store
        for item in items
    }


def _store_nominal_capacity(
    items: Sequence[SelectedBook],
    reserve: int,
) -> int:
    """
    Per-store upper-bound capacity.

    Shared SKU competition is handled later by the
    global planned_qty allocator. This value is used
    only to decide whether low-capacity preference
    books should be replaced before allocation.
    """

    return sum(
        _capacity(
            item.book,
            reserve,
        )
        for item in items
    )


def _fill_regular_books(
    *,
    store: str,
    books: Sequence[Book],
    selected: Dict[
        str,
        List[SelectedBook],
    ],
    target_count: int,
    reserve: int,
    allow_cross_store_duplicate: bool,
    spice_target_percent: int,
) -> int:
    """
    Fill Regular Books up to target_count.

    spice_target_percent is a SOFT target for the final title mix.
    Residents are kept as-is. If one category is insufficient, the other
    category fills the remaining title slots.
    """

    need = max(
        0,
        target_count
        - len(
            selected[store]
        ),
    )

    if need <= 0:
        return 0

    local_skus = (
        _store_selected_skus(
            selected,
            store,
        )
    )

    other_skus = (
        _other_store_skus(
            selected,
            store,
        )
    )

    candidates = [
        book
        for book in books
        if (
            book.sku
            not in local_skus
        )
        and (
            allow_cross_store_duplicate
            or
            book.sku
            not in other_skus
        )
    ]

    spice_books = sorted(
        [
            book
            for book in candidates
            if book.is_spice
        ],
        key=lambda book: (
            -_capacity(
                book,
                reserve,
            ),
            book.sku,
        ),
    )

    non_spice_books = sorted(
        [
            book
            for book in candidates
            if not book.is_spice
        ],
        key=lambda book: (
            -_capacity(
                book,
                reserve,
            ),
            book.sku,
        ),
    )

    desired_spice_total = round(
        target_count
        *
        spice_target_percent
        /
        100
    )

    current_spice = sum(
        1
        for item in selected[store]
        if item.book.is_spice
    )

    spice_needed = max(
        0,
        desired_spice_total
        -
        current_spice,
    )

    spice_needed = min(
        need,
        spice_needed,
    )

    non_spice_needed = (
        need
        -
        spice_needed
    )

    picked = []

    picked.extend(
        spice_books[
            :spice_needed
        ]
    )

    picked.extend(
        non_spice_books[
            :non_spice_needed
        ]
    )

    picked_skus = {
        book.sku
        for book in picked
    }

    # If one side cannot meet the requested mix, fill remaining slots
    # from any still-available book, highest capacity first.
    if len(picked) < need:

        fallback = [
            book
            for book in candidates
            if book.sku
            not in picked_skus
        ]

        fallback.sort(
            key=lambda book: (
                -_capacity(
                    book,
                    reserve,
                ),
                0
                if book.is_spice
                else 1,
                book.sku,
            )
        )

        picked.extend(
            fallback[
                :need - len(picked)
            ]
        )

    for book in picked:

        selected[store].append(
            SelectedBook(
                store=store,
                book=book,
                role="Regular",
                duplicate_across_store=(
                    book.sku
                    in other_skus
                ),
            )
        )

    return len(picked)

def _improve_store_capacity(
    *,
    store: str,
    books: Sequence[Book],
    selected: Dict[
        str,
        List[SelectedBook],
    ],
    target_qty: int,
    reserve: int,
    allow_cross_store_duplicate: bool,
) -> int:
    """
    Replace low-capacity REGULAR books with higher-capacity books
    until the selected set can nominally support target_qty,
    or no beneficial replacement remains.

    Type preference is deliberately relaxed here:
    planned_qty capability takes priority over TU's Non-Spice
    preference and VB's Spice preference.

    Resident Books are never removed.
    """

    replacements = 0

    while (
        _store_nominal_capacity(
            selected[store],
            reserve,
        )
        <
        target_qty
    ):

        local_skus = (
            _store_selected_skus(
                selected,
                store,
            )
        )

        other_skus = (
            _other_store_skus(
                selected,
                store,
            )
        )

        replacement_candidates = [
            book
            for book in books
            if (
                book.sku
                not in local_skus
            )
            and (
                allow_cross_store_duplicate
                or
                book.sku
                not in other_skus
            )
        ]

        if not replacement_candidates:
            break

        replacement_candidates.sort(
            key=lambda book: (
                -_capacity(
                    book,
                    reserve,
                ),
                # If capacities tie, keep the store's
                # preferred category.
                0
                if (
                    book.is_spice
                    ==
                    _prefer_spice(
                        store
                    )
                )
                else 1,
                book.sku,
            )
        )

        best = (
            replacement_candidates[0]
        )

        regular_items = [
            item
            for item in selected[store]
            if item.role == "Regular"
        ]

        if not regular_items:
            break

        # Remove the least useful Regular:
        # lowest capacity first.
        # If capacity ties, remove the non-preferred type first.
        worst = min(
            regular_items,
            key=lambda item: (
                _capacity(
                    item.book,
                    reserve,
                ),
                0
                if (
                    item.book.is_spice
                    !=
                    _prefer_spice(
                        store
                    )
                )
                else 1,
                item.book.sku,
            ),
        )

        best_capacity = _capacity(
            best,
            reserve,
        )

        worst_capacity = _capacity(
            worst.book,
            reserve,
        )

        if (
            best_capacity
            <=
            worst_capacity
        ):
            break

        selected[store].remove(
            worst
        )

        selected[store].append(
            SelectedBook(
                store=store,
                book=best,
                role="Regular",
                duplicate_across_store=(
                    best.sku
                    in other_skus
                ),
            )
        )

        replacements += 1

    return replacements


# =========================================================
# Book selection
# =========================================================

def select_books(
    books: Sequence[Book],
    stores: Sequence[str],
    books_per_store: Union[
        int,
        Dict[str, int],
    ],
    per_store_week_qty: Union[
        int,
        Dict[str, int],
    ],
    resident_by_store: Optional[
        Dict[
            str,
            Sequence[str],
        ]
    ] = None,
    min_reserve_qty: int = 5,
    spice_percent_by_store: Optional[
        Dict[str, int]
    ] = None,
):
    """
    Selection phases for each store:

    1. Keep eligible Residents.
    2. Fill Regular Books with preferred type,
       avoiding cross-store duplicates.
    3. If book count is short, allow cross-store duplicates.
    4. If nominal capacity is below planned_qty:
       replace low-capacity Regulars with higher-capacity books
       without enforcing type preference.
    5. First try capacity replacement without cross-store duplicate.
       If still insufficient, allow cross-store duplicate replacement.

    VB is completed before TU.
    """

    resident_by_store = (
        resident_by_store
        or {}
    )

    warnings: List[str] = []

    store_order = (
        _store_priority(
            stores
        )
    )

    book_target = (
        _normalize_store_targets(
            store_order,
            books_per_store,
            field_name=
                "books_per_store",
        )
    )

    qty_target = (
        _normalize_store_targets(
            store_order,
            per_store_week_qty,
            field_name=
                "per_store_week_qty",
        )
    )

    spice_target = (
        _normalize_spice_targets(
            store_order,
            spice_percent_by_store,
        )
    )

    sku_to_book = {
        book.sku: book
        for book in books
    }

    selected: Dict[
        str,
        List[SelectedBook],
    ] = {
        store: []
        for store in store_order
    }

    # -----------------------------------------------------
    # Residents
    # -----------------------------------------------------

    for store in store_order:

        seen_local = set()

        for raw_sku in (
            resident_by_store.get(
                store,
                [],
            )
        ):

            sku = str(
                raw_sku
            ).strip()

            if (
                not sku
                or
                sku in seen_local
            ):
                continue

            seen_local.add(sku)

            book = (
                sku_to_book.get(
                    sku
                )
            )

            if book is None:
                warnings.append(
                    f"{store} Resident SKU "
                    f"{sku} is not eligible."
                )
                continue

            selected[
                store
            ].append(
                SelectedBook(
                    store=store,
                    book=book,
                    role="Resident",
                )
            )

        if (
            len(selected[store])
            >
            book_target[store]
        ):
            warnings.append(
                f"{store} has "
                f"{len(selected[store])} "
                f"eligible Resident Books, "
                f"which exceeds its book "
                f"target {book_target[store]}. "
                f"All Residents are kept."
            )

    # -----------------------------------------------------
    # Process VB completely, then TU.
    # -----------------------------------------------------

    for store in store_order:

        # First: target count, no cross-store duplicates.
        _fill_regular_books(
            store=store,
            books=books,
            selected=selected,
            target_count=
                book_target[store],
            reserve=
                min_reserve_qty,
            allow_cross_store_duplicate=
                False,
            spice_target_percent=
                spice_target[store],
        )

        # If count still short, duplicates are fallback.
        before = len(
            selected[store]
        )

        if (
            before
            <
            book_target[store]
        ):
            added = (
                _fill_regular_books(
                    store=store,
                    books=books,
                    selected=selected,
                    target_count=
                        book_target[
                            store
                        ],
                    reserve=
                        min_reserve_qty,
                    allow_cross_store_duplicate=
                        True,
                    spice_target_percent=
                        spice_target[store],
                )
            )

            if added > 0:
                warnings.append(
                    f"{store} used "
                    f"{added} cross-store "
                    f"duplicate book(s) "
                    f"to reach its book-count target."
                )

        # Then: capacity optimization.
        # First keep cross-store uniqueness if possible.
        replacements_unique = (
            _improve_store_capacity(
                store=store,
                books=books,
                selected=selected,
                target_qty=
                    qty_target[store],
                reserve=
                    min_reserve_qty,
                allow_cross_store_duplicate=
                    False,
            )
        )

        # If capacity is still below target,
        # allow shared SKU as the next fallback.
        if (
            _store_nominal_capacity(
                selected[store],
                min_reserve_qty,
            )
            <
            qty_target[store]
        ):

            replacements_shared = (
                _improve_store_capacity(
                    store=store,
                    books=books,
                    selected=selected,
                    target_qty=
                        qty_target[
                            store
                        ],
                    reserve=
                        min_reserve_qty,
                    allow_cross_store_duplicate=
                        True,
                )
            )

            if replacements_shared > 0:
                warnings.append(
                    f"{store} replaced "
                    f"{replacements_shared} "
                    f"Regular book(s) with "
                    f"higher-capacity cross-store "
                    f"SKU(s) to improve planned_qty capacity."
                )

        if replacements_unique > 0:
            warnings.append(
                f"{store} replaced "
                f"{replacements_unique} "
                f"low-capacity Regular book(s) "
                f"to improve planned_qty capacity."
            )

        if (
            len(selected[store])
            <
            book_target[store]
        ):
            warnings.append(
                f"{store} selected "
                f"{len(selected[store])} "
                f"book(s); target is "
                f"{book_target[store]}."
            )

        nominal_capacity = (
            _store_nominal_capacity(
                selected[store],
                min_reserve_qty,
            )
        )

        if (
            nominal_capacity
            <
            qty_target[store]
        ):
            warnings.append(
                f"{store} selected-book "
                f"capacity is only "
                f"{nominal_capacity}; "
                f"planned_qty target is "
                f"{qty_target[store]}. "
                f"The final allocator will "
                f"use as much as possible."
            )

    # Recalculate duplicate flags after all replacements.
    for store, items in selected.items():

        other_skus = (
            _other_store_skus(
                selected,
                store,
            )
        )

        for item in items:
            item.duplicate_across_store = (
                item.book.sku
                in other_skus
            )

    return (
        selected,
        warnings,
        book_target,
        qty_target,
        spice_target,
    )


# =========================================================
# Global planned quantity allocation
# =========================================================

def allocate_planned_qty(
    selected_by_store: Dict[
        str,
        List[SelectedBook],
    ],
    per_store_week_qty: Union[
        int,
        Dict[str, int],
    ],
    min_reserve_qty: int = 5,
):
    """
    Allocate planned_qty after selection is complete.

    One SKU has exactly one global capacity:

        candidate_stock - min_reserve_qty

    If TU and VB share a SKU, both stores consume that
    same capacity.

    Each store's demand is repeatedly distributed in
    proportion to candidate_stock among selected books
    that still have global capacity.

    If a store cannot reach its target after capacity
    is exhausted, whatever amount is available is kept.
    """

    warnings: List[str] = []

    stores = list(
        selected_by_store.keys()
    )

    target_by_store = (
        _normalize_store_targets(
            stores,
            per_store_week_qty,
            field_name=
                "per_store_week_qty",
        )
    )

    # Reset.
    for items in (
        selected_by_store.values()
    ):
        for item in items:
            item.planned_qty = 0

    candidate_stock_by_sku = {}

    for items in (
        selected_by_store.values()
    ):
        for item in items:

            sku = item.book.sku
            stock = int(
                item.book.candidate_stock
            )

            previous = (
                candidate_stock_by_sku.get(
                    sku
                )
            )

            if (
                previous is not None
                and
                previous != stock
            ):
                raise ValueError(
                    f"Inconsistent "
                    f"candidate_stock for "
                    f"SKU {sku}."
                )

            candidate_stock_by_sku[
                sku
            ] = stock

    global_capacity = {
        sku: max(
            0,
            stock
            -
            min_reserve_qty,
        )
        for sku, stock
        in candidate_stock_by_sku.items()
    }

    remaining_target = dict(
        target_by_store
    )

    item_lookup = {
        (
            store,
            item.book.sku,
        ): item
        for store, items
        in selected_by_store.items()
        for item in items
    }

    guard = 0

    while guard < 2000:

        guard += 1

        unfinished = [
            store
            for store, qty
            in remaining_target.items()
            if qty > 0
        ]

        if not unfinished:
            break

        proposals = {}

        for store in unfinished:

            available = [
                item
                for item
                in selected_by_store[
                    store
                ]
                if (
                    global_capacity.get(
                        item.book.sku,
                        0,
                    )
                    >
                    0
                )
            ]

            if not available:
                continue

            total_weight = sum(
                item.book.candidate_stock
                for item in available
            )

            if total_weight <= 0:
                continue

            demand = (
                remaining_target[
                    store
                ]
            )

            for item in available:
                proposals[
                    (
                        store,
                        item.book.sku,
                    )
                ] = (
                    demand
                    *
                    item.book.candidate_stock
                    /
                    total_weight
                )

        if not proposals:
            break

        # Total proposal for every SKU across stores.
        proposal_total_by_sku = {}

        for (
            _store,
            sku,
        ), amount in proposals.items():

            proposal_total_by_sku[
                sku
            ] = (
                proposal_total_by_sku.get(
                    sku,
                    0.0,
                )
                +
                amount
            )

        # Scale shared SKU demand down to remaining
        # global capacity.
        scaled = {}

        for edge, amount in (
            proposals.items()
        ):

            _store, sku = edge

            total_for_sku = (
                proposal_total_by_sku[
                    sku
                ]
            )

            capacity = (
                global_capacity[
                    sku
                ]
            )

            if (
                total_for_sku
                >
                capacity
                and
                total_for_sku
                >
                0
            ):
                amount = (
                    amount
                    *
                    capacity
                    /
                    total_for_sku
                )

            scaled[edge] = amount

        distributed = 0

        # Floors.
        for (
            store,
            sku,
        ), amount in scaled.items():

            qty = min(
                int(amount),
                remaining_target[
                    store
                ],
                global_capacity[
                    sku
                ],
            )

            if qty <= 0:
                continue

            item_lookup[
                (
                    store,
                    sku,
                )
            ].planned_qty += qty

            remaining_target[
                store
            ] -= qty

            global_capacity[
                sku
            ] -= qty

            distributed += qty

        # Largest remainder.
        edges = sorted(
            scaled.keys(),
            key=lambda edge: (
                scaled[edge]
                -
                int(
                    scaled[edge]
                ),
                edge[0],
                edge[1],
            ),
            reverse=True,
        )

        gave_one = False

        for store, sku in edges:

            if (
                remaining_target[
                    store
                ]
                <=
                0
            ):
                continue

            if (
                global_capacity[
                    sku
                ]
                <=
                0
            ):
                continue

            item_lookup[
                (
                    store,
                    sku,
                )
            ].planned_qty += 1

            remaining_target[
                store
            ] -= 1

            global_capacity[
                sku
            ] -= 1

            gave_one = True

        if (
            distributed == 0
            and
            not gave_one
        ):
            break

    # Shortfall is a warning, not a failure.
    for store, remaining in (
        remaining_target.items()
    ):

        if remaining <= 0:
            continue

        allocated = (
            target_by_store[store]
            -
            remaining
        )

        warnings.append(
            f"{store} target="
            f"{target_by_store[store]}, "
            f"allocated={allocated}. "
            f"All available selected-book "
            f"capacity was used while "
            f"preserving reserve."
        )

    # Hard reserve verification.
    total_planned_by_sku = {}

    for items in (
        selected_by_store.values()
    ):
        for item in items:

            sku = item.book.sku

            total_planned_by_sku[
                sku
            ] = (
                total_planned_by_sku.get(
                    sku,
                    0,
                )
                +
                item.planned_qty
            )

    for sku, planned in (
        total_planned_by_sku.items()
    ):

        final_stock = (
            candidate_stock_by_sku[
                sku
            ]
            -
            planned
        )

        if (
            final_stock
            <
            min_reserve_qty
        ):
            raise AssertionError(
                f"Reserve violation: "
                f"SKU={sku}, "
                f"final_stock="
                f"{final_stock}, "
                f"reserve="
                f"{min_reserve_qty}"
            )

    return (
        selected_by_store,
        warnings,
    )


# =========================================================
# Main entry
# =========================================================

def allocate(
    books: Sequence[Book],
    stores: Sequence[str],
    books_per_store: Union[
        int,
        Dict[str, int],
    ],
    per_store_week_qty: Union[
        int,
        Dict[str, int],
    ],
    resident_by_store: Optional[
        Dict[
            str,
            Sequence[str],
        ]
    ] = None,
    min_reserve_qty: int = 5,
    spice_percent_by_store: Optional[
        Dict[str, int]
    ] = None,
):
    """
    Returns:
        selected_by_store
        detail_rows
        warnings
    """

    (
        selected_by_store,
        warnings,
        book_target,
        qty_target,
        spice_target,
    ) = select_books(
        books=books,
        stores=stores,
        books_per_store=
            books_per_store,
        per_store_week_qty=
            per_store_week_qty,
        resident_by_store=
            resident_by_store,
        min_reserve_qty=
            min_reserve_qty,
        spice_percent_by_store=
            spice_percent_by_store,
    )

    (
        selected_by_store,
        qty_warnings,
    ) = allocate_planned_qty(
        selected_by_store=
            selected_by_store,
        per_store_week_qty=
            qty_target,
        min_reserve_qty=
            min_reserve_qty,
    )

    warnings.extend(
        qty_warnings
    )

    stores_by_sku = {}

    for store, items in (
        selected_by_store.items()
    ):
        for item in items:
            stores_by_sku.setdefault(
                item.book.sku,
                set(),
            ).add(store)

    detail_rows = []

    for store in (
        _store_priority(
            stores
        )
    ):

        for item in (
            selected_by_store.get(
                store,
                [],
            )
        ):

            detail_rows.append({
                "store":
                    store,
                "sku":
                    item.book.sku,
                "book_title":
                    item.book.name,
                "spice_type":
                    item.book.spice,
                "role":
                    item.role,
                "candidate_stock":
                    item.book.candidate_stock,
                "planned_qty":
                    item.planned_qty,
                "shared_across_store":
                    (
                        len(
                            stores_by_sku.get(
                                item.book.sku,
                                set(),
                            )
                        )
                        >
                        1
                    ),
                "fallback_duplicate":
                    item.duplicate_across_store,
                "store_book_target":
                    book_target[
                        store
                    ],
                "store_planned_qty_target":
                    qty_target[
                        store
                    ],
                "store_spice_target_percent":
                    spice_target[
                        store
                    ],
            })

    return (
        selected_by_store,
        detail_rows,
        warnings,
    )


# =========================================================
# Reporting
# =========================================================

def build_summary(
    detail_rows: Sequence[dict],
    stores: Sequence[str],
):

    result = []

    for store in (
        _store_priority(
            stores
        )
    ):

        rows = [
            row
            for row in detail_rows
            if row.get("store")
            ==
            store
        ]

        spice_count = sum(
            1
            for row in rows
            if (
                str(
                    row.get(
                        "spice_type",
                        "",
                    )
                )
                .strip()
                .lower()
                .startswith(
                    "spice"
                )
                and
                not
                str(
                    row.get(
                        "spice_type",
                        "",
                    )
                )
                .strip()
                .lower()
                .startswith(
                    "non"
                )
            )
        )

        planned_qty = sum(
            int(
                row.get(
                    "planned_qty",
                    0,
                )
            )
            for row in rows
        )

        book_target = (
            rows[0].get(
                "store_book_target"
            )
            if rows
            else None
        )

        planned_target = (
            rows[0].get(
                "store_planned_qty_target"
            )
            if rows
            else None
        )

        spice_target_percent = (
            rows[0].get(
                "store_spice_target_percent"
            )
            if rows
            else None
        )

        actual_spice_percent = (
            round(
                spice_count
                * 100
                / len(rows),
                1,
            )
            if rows
            else 0
        )

        result.append({
            "store":
                store,
            "book_count":
                len(rows),
            "book_target":
                book_target,
            "planned_qty":
                planned_qty,
            "planned_qty_target":
                planned_target,
            "resident_count":
                sum(
                    1
                    for row in rows
                    if row.get("role")
                    ==
                    "Resident"
                ),
            "regular_count":
                sum(
                    1
                    for row in rows
                    if row.get("role")
                    ==
                    "Regular"
                ),
            "spice_count":
                spice_count,
            "spice_target_percent":
                spice_target_percent,
            "actual_spice_percent":
                actual_spice_percent,
            "non_spice_count":
                len(rows)
                -
                spice_count,
            "shared_sku_count":
                sum(
                    1
                    for row in rows
                    if row.get(
                        "shared_across_store"
                    )
                ),
        })

    return result


def build_stock_check(
    books: Sequence[Book],
    detail_rows: Sequence[dict],
    min_reserve_qty: int = 5,
):

    planned_by_sku = {}

    for row in detail_rows:

        sku = str(
            row.get("sku")
            or ""
        ).strip()

        planned_by_sku[
            sku
        ] = (
            planned_by_sku.get(
                sku,
                0,
            )
            +
            int(
                row.get(
                    "planned_qty",
                    0,
                )
            )
        )

    result = []

    for book in books:

        planned = (
            planned_by_sku.get(
                book.sku,
                0,
            )
        )

        final_stock = (
            book.candidate_stock
            -
            planned
        )

        result.append({
            "sku":
                book.sku,
            "book_title":
                book.name,
            "candidate_stock":
                book.candidate_stock,
            "planned_qty_total":
                planned,
            "final_stock":
                final_stock,
            "min_reserve_qty":
                min_reserve_qty,
            "reserve_ok":
                (
                    final_stock
                    >=
                    min_reserve_qty
                ),
        })

    return result
