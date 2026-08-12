from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from erp_automation.persistence import CustomWorkflowStore
from lingxing_automation.storage.dedupe import (
    append_contact_writeback_platform_order,
    append_folder_complete_platform_order,
    append_instruction_remark_platform_order,
    append_package_split_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    append_warehouse_logistics_platform_order,
    export_dedupe_sqlite_to_json,
    import_dedupe_json_to_sqlite,
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


ORDER_NO = "111-2222222-3333333"
SYSTEM_ORDER_NO = "103700000000000001"


def test_sqlite_path_drives_every_custom_order_stage_and_records_history(tmp_path):
    database = tmp_path / "processed_platform_orders.sqlite3"

    assert load_processed_platform_orders(database) == set()
    migrate_dedupe_file(database)
    append_contact_writeback_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        contact_status="written_by_browser",
    )
    assert is_contact_writeback_done(database, ORDER_NO) is True
    assert is_folder_complete(database, ORDER_NO) is False
    assert load_contact_writeback_platform_orders(database) == {ORDER_NO}

    append_folder_complete_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        product_type="tent",
        sku_adjustment_required=True,
    )
    assert load_folder_complete_platform_orders(database) == {ORDER_NO}
    assert is_platform_order_processed(database, ORDER_NO) is False

    append_sku_adjustment_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        sku_status="api",
    )
    assert is_sku_adjustment_done(database, ORDER_NO) is True
    assert is_package_split_done(database, ORDER_NO) is False

    split_order_no = "103700000000000002"
    append_package_split_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        package_status="api",
        package_required=True,
        system_order_nos=[SYSTEM_ORDER_NO, split_order_no],
        instruction_remark_required=True,
    )
    assert is_package_split_done(database, ORDER_NO) is True
    assert is_instruction_remark_done(database, ORDER_NO) is False

    append_instruction_remark_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        remark_status="api",
        target_system_order_no=split_order_no,
    )
    assert is_instruction_remark_done(database, ORDER_NO) is True
    assert is_warehouse_logistics_done(database, ORDER_NO) is False
    assert load_processed_platform_orders(database) == set()
    append_warehouse_logistics_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        warehouse_status="api",
        decisions=[{"system_order_no": split_order_no, "status": "ready"}],
        write_results=[{"system_order_no": split_order_no, "status": "verified"}],
    )
    assert is_warehouse_logistics_done(database, ORDER_NO) is True
    assert load_processed_platform_orders(database) == {ORDER_NO}

    store = CustomWorkflowStore(database)
    record = store.get_legacy_record(ORDER_NO)
    assert record is not None
    assert record["system_order_no"] == SYSTEM_ORDER_NO
    assert record["product_type"] == "tent"
    assert record["sku_adjustment_status"] == "api"
    assert record["package_split_system_order_nos"] == [SYSTEM_ORDER_NO, split_order_no]
    assert record["instruction_remark_target_system_order_no"] == split_order_no
    assert record["workflow_status"] == "completed"
    assert [event["event_type"] for event in store.history(ORDER_NO)] == [
        "contact_writeback_recorded",
        "folder_complete_recorded",
        "sku_adjustment_recorded",
        "package_split_recorded",
        "instruction_remark_recorded",
        "warehouse_logistics_recorded",
    ]


def test_db_suffix_supports_final_processed_shortcut(tmp_path):
    database = tmp_path / "workflow.db"

    append_processed_platform_order(database, ORDER_NO, SYSTEM_ORDER_NO)

    assert is_contact_writeback_done(database, ORDER_NO) is True
    assert is_folder_complete(database, ORDER_NO) is True
    assert is_platform_order_processed(database, ORDER_NO) is True
    assert CustomWorkflowStore(database).history(ORDER_NO)[0]["event_type"] == "processed_order_recorded"


def test_non_tent_refresh_clears_stale_adjustment_stages_and_completes(tmp_path):
    database = tmp_path / "workflow.sqlite3"
    append_folder_complete_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        product_type="car_magnet",
        sku_adjustment_required=True,
    )
    before = CustomWorkflowStore(database).get_legacy_record(ORDER_NO)
    assert before["workflow_status"] == "sku_adjustment_pending"

    append_processed_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        product_type="car_magnet",
        sku_adjustment_required=False,
    )

    after = CustomWorkflowStore(database).get_legacy_record(ORDER_NO)
    assert after["workflow_status"] == "completed"
    assert "sku_adjustment_required" not in after
    assert "package_split_required" not in after
    assert "warehouse_logistics_required" not in after
    assert load_processed_platform_orders(database) == {ORDER_NO}


def test_json_import_and_rollback_export_are_explicit_and_legacy_readable(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(
        json.dumps(
            {
                "version": 3,
                "orders": {
                    ORDER_NO: {
                        "platform_order_no": ORDER_NO,
                        "system_order_no": SYSTEM_ORDER_NO,
                        "contact_writeback_complete": True,
                        "folder_complete": True,
                        "workflow_status": "completed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "workflow.sqlite3"

    result = import_dedupe_json_to_sqlite(source, database, create_backup=False)
    exported = export_dedupe_sqlite_to_json(database, tmp_path / "rollback.json")

    assert result.imported_count == 1
    assert load_processed_platform_orders(database) == {ORDER_NO}
    assert load_processed_platform_orders(exported) == {ORDER_NO}


def test_concurrent_stage_writes_merge_in_one_transaction_without_lost_fields(tmp_path):
    database = tmp_path / "workflow.sqlite3"
    split_order_no = "103700000000000002"
    append_sku_adjustment_platform_order(database, ORDER_NO, SYSTEM_ORDER_NO, sku_status="api")

    def write_package() -> None:
        append_package_split_platform_order(
            database,
            ORDER_NO,
            SYSTEM_ORDER_NO,
            package_status="api",
            package_required=True,
            system_order_nos=[SYSTEM_ORDER_NO, split_order_no],
            instruction_remark_required=True,
        )

    def write_remark() -> None:
        append_instruction_remark_platform_order(
            database,
            ORDER_NO,
            SYSTEM_ORDER_NO,
            remark_status="api",
            target_system_order_no=split_order_no,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write_package), executor.submit(write_remark)]
        for future in futures:
            future.result()

    record = CustomWorkflowStore(database).get_legacy_record(ORDER_NO)
    assert record is not None
    assert record["package_split_system_order_nos"] == [SYSTEM_ORDER_NO, split_order_no]
    assert record["instruction_remark_target_system_order_no"] == split_order_no
    assert record["package_split_complete"] is True
    assert record["instruction_remark_complete"] is True
    assert record["warehouse_logistics_complete"] is False
    append_warehouse_logistics_platform_order(
        database,
        ORDER_NO,
        SYSTEM_ORDER_NO,
        warehouse_status="api",
    )
    assert is_platform_order_processed(database, ORDER_NO) is True
    assert len(CustomWorkflowStore(database).history(ORDER_NO)) == 4
