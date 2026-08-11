from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from erp_automation.persistence.workflow_store import CustomWorkflowStore, ImportResult

from ..constants import PLATFORM_ORDER_RE
from . import dedupe as legacy


_STORE_LOCK = threading.Lock()


@lru_cache(maxsize=32)
def _cached_store(resolved_path: str) -> CustomWorkflowStore:
    return CustomWorkflowStore(resolved_path)


def get_store(path: str | Path) -> CustomWorkflowStore:
    resolved = str(Path(path).expanduser().resolve())
    # Serialize first-use schema/WAL setup even when multiple workers reach a
    # brand-new database at the same instant.
    with _STORE_LOCK:
        store = _cached_store(resolved)
        store.initialize()
        return store


def _validate_platform_order_no(platform_order_no: str) -> None:
    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")


def migrate_dedupe_file(path: str | Path) -> None:
    get_store(path).initialize()


def load_processed_platform_orders(path: str | Path) -> set[str]:
    return get_store(path).processed_platform_orders()


def load_contact_writeback_platform_orders(path: str | Path) -> set[str]:
    # Legacy semantics treat a completed folder as proof that contact writeback
    # already happened, including records imported from early file versions.
    return get_store(path).completed_platform_orders_for_stages("contact", "folder")


def load_folder_complete_platform_orders(path: str | Path) -> set[str]:
    return get_store(path).completed_platform_orders_for_stages("folder")


def is_sku_adjustment_done(path: str | Path, platform_order_no: str) -> bool:
    return get_store(path).is_stage_completed(platform_order_no, "sku")


def is_package_split_done(path: str | Path, platform_order_no: str) -> bool:
    return get_store(path).is_stage_completed(platform_order_no, "package_split")


def is_instruction_remark_done(path: str | Path, platform_order_no: str) -> bool:
    return get_store(path).is_stage_completed(platform_order_no, "instruction_remark")


def is_warehouse_logistics_done(path: str | Path, platform_order_no: str) -> bool:
    return get_store(path).is_stage_completed(platform_order_no, "warehouse_logistics")


def _base(
    old_record: dict[str, Any],
    platform_order_no: str,
    system_order_no: str | None,
) -> dict[str, Any]:
    return {
        **legacy._base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
    }


def _clean_legacy_keys(record: dict[str, Any]) -> dict[str, Any]:
    record.pop(legacy.LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(legacy.LEGACY_FOLDER_DONE_KEY, None)
    return legacy._apply_workflow_status(record)


def append_contact_writeback_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    contact_status: str = "written",
    contact_verified: bool = False,
    contact_verification_method: str | None = None,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            "contact_status": contact_status,
            "contact_completed_at": old_record.get("contact_completed_at") or legacy._now_text(),
            "last_seen_at": legacy._now_text(),
        }
        if contact_verified:
            record["contact_writeback_verified"] = True
            record["contact_verification_method"] = (
                str(contact_verification_method or "").strip()
                or "browser_detail_reopen"
            )
            record["contact_verified_at"] = legacy._now_text()
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="contact_writeback_recorded",
        stage="contact",
    )


def append_folder_complete_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    product_type: str | None = None,
    sku_adjustment_required: bool = False,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            "folder_completed_at": old_record.get("folder_completed_at") or legacy._now_text(),
            "last_seen_at": legacy._now_text(),
        }
        if product_type:
            record[legacy.PRODUCT_TYPE_KEY] = product_type
        if sku_adjustment_required:
            record[legacy.SKU_ADJUSTMENT_REQUIRED_KEY] = True
            record[legacy.PRODUCT_TYPE_KEY] = product_type or legacy.PRODUCT_TYPE_TENT_VALUE
        elif not legacy._normalize_bool(record.get(legacy.SKU_ADJUSTMENT_REQUIRED_KEY)):
            record.pop(legacy.SKU_ADJUSTMENT_REQUIRED_KEY, None)
            record.pop(legacy.SKU_ADJUSTMENT_COMPLETE_KEY, None)
            record.pop(legacy.PACKAGE_SPLIT_REQUIRED_KEY, None)
            record.pop(legacy.PACKAGE_SPLIT_COMPLETE_KEY, None)
            record.pop(legacy.INSTRUCTION_REMARK_REQUIRED_KEY, None)
            record.pop(legacy.INSTRUCTION_REMARK_COMPLETE_KEY, None)
            record.pop(legacy.WAREHOUSE_LOGISTICS_REQUIRED_KEY, None)
            record.pop(legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY, None)
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="folder_complete_recorded",
        stage="folder",
    )


def append_sku_adjustment_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    sku_status: str = "auto",
    instruction_replaced_at: str | None = None,
    instruction_customer_remark: str | None = None,
    workflow_kind: str | None = None,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            legacy.SKU_ADJUSTMENT_REQUIRED_KEY: True,
            legacy.SKU_ADJUSTMENT_COMPLETE_KEY: True,
            legacy.PRODUCT_TYPE_KEY: old_record.get(legacy.PRODUCT_TYPE_KEY)
            or legacy.PRODUCT_TYPE_TENT_VALUE,
            "sku_adjustment_status": sku_status,
            "sku_adjustment_completed_at": old_record.get("sku_adjustment_completed_at")
            or legacy._now_text(),
            "last_seen_at": legacy._now_text(),
        }
        if instruction_replaced_at:
            record["instruction_replaced_at"] = str(instruction_replaced_at)
        if instruction_customer_remark:
            record["instruction_customer_remark"] = str(instruction_customer_remark)
        if workflow_kind:
            record["sku_adjustment_workflow_kind"] = str(workflow_kind)
        if not legacy._normalize_bool(record.get(legacy.PACKAGE_SPLIT_COMPLETE_KEY)):
            record[legacy.PACKAGE_SPLIT_REQUIRED_KEY] = True
            record[legacy.PACKAGE_SPLIT_COMPLETE_KEY] = False
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        else:
            record.pop("processed_at", None)
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="sku_adjustment_recorded",
        stage="sku",
    )


def append_package_split_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    package_status: str,
    package_required: bool,
    system_order_nos: list[str] | None = None,
    instruction_remark_required: bool = False,
    warehouse_plan_input: dict[str, Any] | None = None,
    instruction_system_order_no: str | None = None,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            legacy.SKU_ADJUSTMENT_REQUIRED_KEY: True,
            legacy.SKU_ADJUSTMENT_COMPLETE_KEY: True,
            legacy.PACKAGE_SPLIT_REQUIRED_KEY: bool(package_required),
            legacy.PACKAGE_SPLIT_COMPLETE_KEY: True,
            legacy.PRODUCT_TYPE_KEY: old_record.get(legacy.PRODUCT_TYPE_KEY)
            or legacy.PRODUCT_TYPE_TENT_VALUE,
            "package_split_status": package_status,
            "package_split_completed_at": old_record.get("package_split_completed_at")
            or legacy._now_text(),
            "package_split_system_order_nos": list(system_order_nos or []),
            "package_split_instruction_system_order_no": instruction_system_order_no
            or old_record.get("package_split_instruction_system_order_no"),
            "warehouse_logistics_plan_input": dict(
                warehouse_plan_input or old_record.get("warehouse_logistics_plan_input") or {}
            ),
            legacy.WAREHOUSE_LOGISTICS_REQUIRED_KEY: True,
            legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY: legacy._normalize_bool(
                old_record.get(legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY)
            ),
            "last_seen_at": legacy._now_text(),
        }
        if instruction_remark_required:
            record[legacy.INSTRUCTION_REMARK_REQUIRED_KEY] = True
            if not legacy._normalize_bool(record.get(legacy.INSTRUCTION_REMARK_COMPLETE_KEY)):
                record[legacy.INSTRUCTION_REMARK_COMPLETE_KEY] = False
        elif not legacy._normalize_bool(record.get(legacy.INSTRUCTION_REMARK_REQUIRED_KEY)):
            record.pop(legacy.INSTRUCTION_REMARK_REQUIRED_KEY, None)
            record.pop(legacy.INSTRUCTION_REMARK_COMPLETE_KEY, None)
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        else:
            record.pop("processed_at", None)
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="package_split_recorded",
        stage="package_split",
    )


def append_instruction_remark_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    remark_status: str = "auto",
    target_system_order_no: str | None = None,
    warehouse_plan_input: dict[str, Any] | None = None,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            legacy.SKU_ADJUSTMENT_REQUIRED_KEY: True,
            legacy.SKU_ADJUSTMENT_COMPLETE_KEY: True,
            legacy.PACKAGE_SPLIT_COMPLETE_KEY: True,
            legacy.PRODUCT_TYPE_KEY: old_record.get(legacy.PRODUCT_TYPE_KEY)
            or legacy.PRODUCT_TYPE_TENT_VALUE,
            legacy.INSTRUCTION_REMARK_REQUIRED_KEY: True,
            legacy.INSTRUCTION_REMARK_COMPLETE_KEY: True,
            legacy.WAREHOUSE_LOGISTICS_REQUIRED_KEY: True,
            legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY: legacy._normalize_bool(
                old_record.get(legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY)
            ),
            "instruction_remark_status": remark_status,
            "instruction_remark_completed_at": old_record.get("instruction_remark_completed_at")
            or legacy._now_text(),
            "instruction_remark_target_system_order_no": target_system_order_no
            or old_record.get("instruction_remark_target_system_order_no"),
            "warehouse_logistics_plan_input": dict(
                warehouse_plan_input or old_record.get("warehouse_logistics_plan_input") or {}
            ),
            "last_seen_at": legacy._now_text(),
        }
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        else:
            record.pop("processed_at", None)
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="instruction_remark_recorded",
        stage="instruction_remark",
    )


def append_warehouse_logistics_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    warehouse_status: str,
    decisions: list[dict[str, Any]] | None = None,
    write_results: list[dict[str, Any]] | None = None,
    result_detail: str | None = None,
    warehouse_required: bool = True,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            legacy.SKU_ADJUSTMENT_REQUIRED_KEY: True,
            legacy.SKU_ADJUSTMENT_COMPLETE_KEY: True,
            legacy.PACKAGE_SPLIT_COMPLETE_KEY: True,
            legacy.WAREHOUSE_LOGISTICS_REQUIRED_KEY: bool(warehouse_required),
            legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY: True,
            legacy.PRODUCT_TYPE_KEY: old_record.get(legacy.PRODUCT_TYPE_KEY)
            or legacy.PRODUCT_TYPE_TENT_VALUE,
            "warehouse_logistics_status": warehouse_status,
            "warehouse_logistics_completed_at": old_record.get(
                "warehouse_logistics_completed_at"
            )
            or legacy._now_text(),
            "warehouse_logistics_decisions": list(decisions or []),
            "warehouse_logistics_write_results": list(write_results or []),
            "warehouse_logistics_result_detail": str(result_detail or "").strip() or None,
            "last_seen_at": legacy._now_text(),
        }
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        else:
            record.pop("processed_at", None)
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="warehouse_logistics_recorded",
        stage="warehouse_logistics",
    )


def update_warehouse_logistics_plan_input(
    path: str | Path,
    platform_order_no: str,
    plan_input: dict[str, Any],
) -> None:
    """Refresh warehouse routing input without completing any workflow stage."""

    _validate_platform_order_no(platform_order_no)
    store = get_store(path)
    if store.get_legacy_record(platform_order_no) is None:
        raise KeyError(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        return _clean_legacy_keys(
            {
                **old_record,
                "warehouse_logistics_plan_input": dict(plan_input),
                "last_seen_at": legacy._now_text(),
            }
        )

    store.mutate_legacy_record(
        platform_order_no,
        update,
        event_type="warehouse_logistics_plan_refreshed",
        stage="warehouse_logistics",
        reason="重新读取原始系统单收货信息并刷新仓库物流计划。",
    )


def append_processed_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    product_type: str | None = None,
    sku_adjustment_required: bool = False,
) -> None:
    _validate_platform_order_no(platform_order_no)

    def update(old_record: dict[str, Any]) -> dict[str, Any]:
        record = {
            **_base(old_record, platform_order_no, system_order_no),
            legacy.CONTACT_WRITEBACK_COMPLETE_KEY: True,
            legacy.FOLDER_COMPLETE_KEY: True,
            "contact_completed_at": old_record.get("contact_completed_at") or legacy._now_text(),
            "folder_completed_at": old_record.get("folder_completed_at") or legacy._now_text(),
            "last_seen_at": legacy._now_text(),
        }
        if product_type:
            record[legacy.PRODUCT_TYPE_KEY] = product_type
        if sku_adjustment_required:
            record[legacy.SKU_ADJUSTMENT_REQUIRED_KEY] = True
            record[legacy.PRODUCT_TYPE_KEY] = product_type or legacy.PRODUCT_TYPE_TENT_VALUE
        elif not legacy._normalize_bool(record.get(legacy.SKU_ADJUSTMENT_REQUIRED_KEY)):
            record.pop(legacy.SKU_ADJUSTMENT_REQUIRED_KEY, None)
            record.pop(legacy.SKU_ADJUSTMENT_COMPLETE_KEY, None)
            record.pop(legacy.PACKAGE_SPLIT_REQUIRED_KEY, None)
            record.pop(legacy.PACKAGE_SPLIT_COMPLETE_KEY, None)
            record.pop(legacy.INSTRUCTION_REMARK_REQUIRED_KEY, None)
            record.pop(legacy.INSTRUCTION_REMARK_COMPLETE_KEY, None)
            record.pop(legacy.WAREHOUSE_LOGISTICS_REQUIRED_KEY, None)
            record.pop(legacy.WAREHOUSE_LOGISTICS_COMPLETE_KEY, None)
        if legacy._is_final_complete(record):
            record["processed_at"] = old_record.get("processed_at") or legacy._now_text()
        return _clean_legacy_keys(record)

    get_store(path).mutate_legacy_record(
        platform_order_no,
        update,
        event_type="processed_order_recorded",
    )


def import_dedupe_json_to_sqlite(
    source_json: str | Path,
    sqlite_path: str | Path,
    *,
    create_backup: bool = True,
    overwrite_existing: bool = False,
) -> ImportResult:
    """Explicitly migrate a legacy JSON file into the SQLite workflow store."""

    return get_store(sqlite_path).import_legacy_json(
        source_json,
        create_backup=create_backup,
        overwrite_existing=overwrite_existing,
    )


def export_dedupe_sqlite_to_json(
    sqlite_path: str | Path,
    target_json: str | Path,
) -> Path:
    """Explicitly export SQLite state when an operator chooses script rollback."""

    return get_store(sqlite_path).export_legacy_json(target_json)


__all__ = [
    "append_contact_writeback_platform_order",
    "append_folder_complete_platform_order",
    "append_instruction_remark_platform_order",
    "append_package_split_platform_order",
    "append_processed_platform_order",
    "append_sku_adjustment_platform_order",
    "append_warehouse_logistics_platform_order",
    "export_dedupe_sqlite_to_json",
    "get_store",
    "import_dedupe_json_to_sqlite",
    "is_instruction_remark_done",
    "is_package_split_done",
    "is_sku_adjustment_done",
    "is_warehouse_logistics_done",
    "load_contact_writeback_platform_orders",
    "load_folder_complete_platform_orders",
    "load_processed_platform_orders",
    "migrate_dedupe_file",
]
