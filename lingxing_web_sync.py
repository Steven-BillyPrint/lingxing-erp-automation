"""Import-only compatibility facade for legacy regression tests.

The implementation now lives under :mod:`lingxing_automation`. This module
keeps public imports working for regression tests.  It intentionally no longer
launches the script workflow; the refactored branch has one supported daily
entrypoint: :mod:`desktop_main`.  The runnable script version remains frozen on
the dedicated rollback branch.
"""

from __future__ import annotations

from lingxing_automation.browser.session import (
    get_first_page,
    is_login_page,
    launch_context,
    try_auto_login,
    wait_for_order_page,
)
from lingxing_automation.cli import build_parser, main, print_result, prompt_for_missing_args
from lingxing_automation.config import load_login_config, parse_env_bool, read_lingxing_env
from lingxing_automation.constants import ORDER_MANAGEMENT_URL, PLATFORM_ORDER_RE, SYSTEM_ORDER_RE
from lingxing_automation.flows.contact_sync import (
    build_writeback_success_message,
    build_writeback_without_processed_message,
    compact_batch_result_log,
    compact_batch_scan_log,
    contact_writeback_fields,
    process_batch_order_item,
    run_batch,
    run_batch_round,
    run_once,
    run_retry_order,
    run_retry_order_round,
    save_screenshot,
    write_batch_result,
    write_batch_scan_log,
    write_result,
)
from lingxing_automation.models import (
    BatchOrderItem,
    ContactInfo,
    CustomZipDownloadResult,
    LoginConfig,
    OrderFolderTask,
    SkuDecision,
    SplitDecision,
    SyncResult,
)
from lingxing_automation.pages.order_detail import (
    click_save_button,
    click_system_order,
    close_order_detail_dialog,
    collect_detail_contact_candidates,
    collect_detail_text_candidates,
    extract_contact_from_system_order,
    fill_contact_fields,
    fill_shipping_contact_field,
    find_contact_from_system_orders,
    has_editable_contact_controls,
    try_open_edit_mode,
    update_contact_for_system_orders,
    wait_for_detail,
)
from lingxing_automation.pages.order_management import (
    build_batch_candidates_from_rows,
    click_order_search_button,
    close_search_overlays,
    collect_batch_order_candidates,
    ensure_order_view_mode,
    fill_order_search,
    find_order_search_input_index,
    find_system_order_for_order_no,
    find_system_orders_for_order_no,
    find_visible_system_order_no,
    get_order_search_snapshot,
    select_order_search_type,
    wait_for_order_in_list,
    wait_for_orders_in_list,
)
from lingxing_automation.parsers.contact import (
    contact_choice_identity,
    contact_identity,
    extract_complete_contact_candidates,
    extract_contact_info,
    extract_fixed_contact_info,
    extract_unique_fixed_contact_info,
    missing_contact_fields,
    normalize_fixed_phone_answer,
    normalize_phone,
    normalize_text,
    split_collapsed_fixed_prompts,
)
from lingxing_automation.parsers.orders import (
    guess_search_kind,
    is_single_main_sku_order_text,
    validate_search_snapshot,
)
from lingxing_automation.storage.dedupe import (
    append_contact_writeback_platform_order,
    append_folder_complete_platform_order,
    append_instruction_remark_platform_order,
    append_package_split_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    append_warehouse_logistics_platform_order,
    is_contact_writeback_done,
    is_folder_complete,
    is_instruction_remark_done,
    is_package_split_done,
    is_platform_order_processed,
    is_sku_adjustment_done,
    is_warehouse_logistics_done,
    load_contact_writeback_platform_orders,
    load_folder_complete_platform_orders,
    load_processed_platform_orders,
    migrate_dedupe_file,
)

__all__ = [name for name in globals() if not name.startswith('_')]


if __name__ == "__main__":
    print("旧脚本入口已从重构版本停用；请运行 ERP自动化.exe 或 desktop_main.py。")
    raise SystemExit(2)
