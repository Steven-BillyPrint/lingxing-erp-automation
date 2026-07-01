"""Facade for order detail page operations."""

from __future__ import annotations

from .order_detail_extraction import (
    collect_order_folder_dom_context,
    collect_detail_contact_candidates,
    collect_detail_customization_items,
    collect_detail_text_candidates,
    extract_contact_from_system_order,
    find_contact_from_system_orders,
    read_detail_product_quantity,
    read_detail_recipient_name,
)
from .order_detail_navigation import (
    assert_current_detail_order,
    close_order_detail_dialog,
    click_system_order,
    get_current_detail_identity,
    wait_for_detail,
)
from .order_detail_writeback import (
    click_save_button,
    fill_contact_fields,
    fill_shipping_contact_field,
    has_editable_contact_controls,
    try_open_edit_mode,
    update_current_detail_contact,
    update_contact_for_system_orders,
)

__all__ = [name for name in globals() if not name.startswith('_')]
