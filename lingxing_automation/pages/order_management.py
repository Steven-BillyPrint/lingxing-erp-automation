"""Facade for order management page operations."""

from __future__ import annotations

from .order_list import (
    build_batch_candidates_from_rows,
    collect_batch_order_candidates,
    collect_visible_batch_order_rows,
    ensure_batch_key_columns_visible,
    ensure_order_view_mode,
    ensure_page_size_1000,
    find_system_order_for_order_no,
    find_system_orders_for_order_no,
    find_visible_system_order_no,
    wait_for_visible_batch_order_rows,
    wait_for_order_in_list,
    wait_for_orders_in_list,
)
from .order_search import (
    click_order_search_button,
    close_search_overlays,
    fill_order_search,
    find_order_search_input_index,
    get_order_search_snapshot,
    select_order_search_type,
)

__all__ = [name for name in globals() if not name.startswith('_')]
