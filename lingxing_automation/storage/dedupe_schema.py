from __future__ import annotations

from typing import Any


ORDERS_KEY = "orders"
LEGACY_CONTACT_ORDERS_KEY = "contact_writeback_orders"
CONTACT_WRITEBACK_COMPLETE_KEY = "contact_writeback_complete"
FOLDER_COMPLETE_KEY = "folder_complete"
SKU_ADJUSTMENT_COMPLETE_KEY = "sku_adjustment_complete"
SKU_ADJUSTMENT_REQUIRED_KEY = "sku_adjustment_required"
PACKAGE_SPLIT_COMPLETE_KEY = "package_split_complete"
PACKAGE_SPLIT_REQUIRED_KEY = "package_split_required"
INSTRUCTION_REMARK_COMPLETE_KEY = "instruction_remark_complete"
INSTRUCTION_REMARK_REQUIRED_KEY = "instruction_remark_required"
PRODUCT_TYPE_KEY = "product_type"
PRODUCT_TYPE_TENT_VALUE = "tent"
LEGACY_CONTACT_WRITEBACK_KEY = "contact_writeback_done"
LEGACY_FOLDER_DONE_KEY = "folder_done"


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "done", "completed"}
    return bool(value)


__all__ = [
    "CONTACT_WRITEBACK_COMPLETE_KEY",
    "FOLDER_COMPLETE_KEY",
    "INSTRUCTION_REMARK_COMPLETE_KEY",
    "INSTRUCTION_REMARK_REQUIRED_KEY",
    "LEGACY_CONTACT_ORDERS_KEY",
    "LEGACY_CONTACT_WRITEBACK_KEY",
    "LEGACY_FOLDER_DONE_KEY",
    "ORDERS_KEY",
    "PACKAGE_SPLIT_COMPLETE_KEY",
    "PACKAGE_SPLIT_REQUIRED_KEY",
    "PRODUCT_TYPE_KEY",
    "PRODUCT_TYPE_TENT_VALUE",
    "SKU_ADJUSTMENT_COMPLETE_KEY",
    "SKU_ADJUSTMENT_REQUIRED_KEY",
    "normalize_bool",
]
