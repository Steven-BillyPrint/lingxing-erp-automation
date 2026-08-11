from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..constants import PLATFORM_ORDER_RE
from .dedupe_schema import (
    CONTACT_WRITEBACK_COMPLETE_KEY,
    FOLDER_COMPLETE_KEY,
    INSTRUCTION_REMARK_COMPLETE_KEY,
    INSTRUCTION_REMARK_REQUIRED_KEY,
    LEGACY_CONTACT_ORDERS_KEY,
    LEGACY_CONTACT_WRITEBACK_KEY,
    LEGACY_FOLDER_DONE_KEY,
    ORDERS_KEY,
    PACKAGE_SPLIT_COMPLETE_KEY,
    PACKAGE_SPLIT_REQUIRED_KEY,
    PRODUCT_TYPE_KEY,
    PRODUCT_TYPE_TENT_VALUE,
    SKU_ADJUSTMENT_COMPLETE_KEY,
    SKU_ADJUSTMENT_REQUIRED_KEY,
    WAREHOUSE_LOGISTICS_COMPLETE_KEY,
    WAREHOUSE_LOGISTICS_REQUIRED_KEY,
    normalize_bool as _normalize_bool,
)

SQLITE_DEDUPE_SUFFIXES = frozenset({".sqlite3", ".db"})


def is_sqlite_dedupe_path(path: str | Path) -> bool:
    """Return whether the configured dedupe path selects the SQLite backend."""

    return Path(path).suffix.lower() in SQLITE_DEDUPE_SUFFIXES


def _now_text() -> str:
    """生成去重记录使用的当前时间文本。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty_payload() -> dict[str, Any]:
    """处理空值载荷相关逻辑，并返回后续流程所需结果。"""
    return {"version": 3, "updated_at": None, ORDERS_KEY: {}}


def _base_record(platform_order_no: str, system_order_no: str | None = None) -> dict[str, Any]:
    """构造去重文件中的基础订单记录。"""
    return {
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no,
        CONTACT_WRITEBACK_COMPLETE_KEY: False,
        FOLDER_COMPLETE_KEY: False,
        "workflow_status": "pending",
    }


def _read_bool(value: dict[str, Any], *keys: str) -> bool:
    """读取布尔值。"""
    return any(_normalize_bool(value.get(key)) for key in keys)


def _sku_adjustment_required(record: dict[str, Any]) -> bool:
    """判断订单是否需要第三阶段 SKU 调整。

    只有新流程明确写入 sku_adjustment_required=true 的帐篷订单才需要第三阶段。
    历史 processed 记录没有这个字段，不能因为后来新增了 SKU 阶段而被重新放出来巡检。
    """

    return _normalize_bool(record.get(SKU_ADJUSTMENT_REQUIRED_KEY))


def _package_split_required(record: dict[str, Any]) -> bool:
    """判断订单是否需要帐篷拆分包裹阶段。"""

    return _normalize_bool(record.get(PACKAGE_SPLIT_REQUIRED_KEY))


def _instruction_remark_required(record: dict[str, Any]) -> bool:
    """判断订单是否需要拆包后写说明书客服备注。"""

    return _normalize_bool(record.get(INSTRUCTION_REMARK_REQUIRED_KEY))


def _warehouse_logistics_required(record: dict[str, Any]) -> bool:
    """判断订单是否需要拆单后的仓库物流阶段。"""

    return _normalize_bool(record.get(WAREHOUSE_LOGISTICS_REQUIRED_KEY))


def _is_final_complete(record: dict[str, Any]) -> bool:
    """判断最终完成是否满足业务条件。"""
    if not _normalize_bool(record.get(CONTACT_WRITEBACK_COMPLETE_KEY)):
        return False
    if not _normalize_bool(record.get(FOLDER_COMPLETE_KEY)):
        return False
    if _sku_adjustment_required(record) and not _normalize_bool(record.get(SKU_ADJUSTMENT_COMPLETE_KEY)):
        return False
    if _package_split_required(record) and not _normalize_bool(record.get(PACKAGE_SPLIT_COMPLETE_KEY)):
        return False
    if _instruction_remark_required(record) and not _normalize_bool(record.get(INSTRUCTION_REMARK_COMPLETE_KEY)):
        return False
    if _warehouse_logistics_required(record) and not _normalize_bool(record.get(WAREHOUSE_LOGISTICS_COMPLETE_KEY)):
        return False
    return True


def _apply_workflow_status(record: dict[str, Any]) -> dict[str, Any]:
    """根据各阶段状态刷新订单去重记录。"""
    if _is_final_complete(record):
        record["workflow_status"] = "completed"
    elif (
        _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
        and _sku_adjustment_required(record)
        and not _normalize_bool(record.get(SKU_ADJUSTMENT_COMPLETE_KEY))
    ):
        record["workflow_status"] = "sku_adjustment_pending"
    elif (
        _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
        and _package_split_required(record)
        and not _normalize_bool(record.get(PACKAGE_SPLIT_COMPLETE_KEY))
    ):
        record["workflow_status"] = "package_split_pending"
    elif (
        _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
        and _instruction_remark_required(record)
        and not _normalize_bool(record.get(INSTRUCTION_REMARK_COMPLETE_KEY))
    ):
        record["workflow_status"] = "instruction_remark_pending"
    elif (
        _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
        and _warehouse_logistics_required(record)
        and not _normalize_bool(record.get(WAREHOUSE_LOGISTICS_COMPLETE_KEY))
    ):
        record["workflow_status"] = "warehouse_logistics_pending"
    elif _normalize_bool(record.get(CONTACT_WRITEBACK_COMPLETE_KEY)):
        record["workflow_status"] = "folder_pending"
    else:
        record["workflow_status"] = "pending"
    return record


def _coerce_order_map(raw_orders: Any, *, legacy_final_done: bool) -> dict[str, dict[str, Any]]:
    """处理coerce 订单映射相关逻辑，并返回后续流程所需结果。"""
    orders: dict[str, dict[str, Any]] = {}
    if isinstance(raw_orders, dict):
        iterable = raw_orders.items()
    elif isinstance(raw_orders, list):
        iterable = []
        for item in raw_orders:
            if isinstance(item, str):
                iterable.append((item, {"platform_order_no": item}))
            elif isinstance(item, dict):
                key = item.get("platform_order_no") or item.get("platform_order_id") or ""
                iterable.append((key, item))
    else:
        return orders

    for platform_order_no, record in iterable:
        key = str(platform_order_no).strip()
        if not PLATFORM_ORDER_RE.fullmatch(key):
            continue
        value = dict(record) if isinstance(record, dict) else {}
        system_order_no = value.get("system_order_no")
        normalized = {**_base_record(key, system_order_no), **value, "platform_order_no": key}
        if legacy_final_done:
            # 旧版 processed 文件只有“最终完成”一个概念；迁移时默认联系方式和文件夹都已完成。
            # SKU 阶段是后续新增的，只对新写入的帐篷订单生效，不能让历史订单重新巡检。
            normalized[CONTACT_WRITEBACK_COMPLETE_KEY] = True
            normalized[FOLDER_COMPLETE_KEY] = True
            normalized.pop(SKU_ADJUSTMENT_REQUIRED_KEY, None)
            normalized.pop(SKU_ADJUSTMENT_COMPLETE_KEY, None)
            normalized.pop(PACKAGE_SPLIT_REQUIRED_KEY, None)
            normalized.pop(PACKAGE_SPLIT_COMPLETE_KEY, None)
            normalized["workflow_status"] = "completed"
            normalized["processed_at"] = normalized.get("processed_at")
        else:
            has_explicit_folder_status = FOLDER_COMPLETE_KEY in normalized or LEGACY_FOLDER_DONE_KEY in normalized
            contact_done = _read_bool(
                normalized,
                CONTACT_WRITEBACK_COMPLETE_KEY,
                LEGACY_CONTACT_WRITEBACK_KEY,
            )
            folder_done = _read_bool(normalized, FOLDER_COMPLETE_KEY, LEGACY_FOLDER_DONE_KEY)
            if not has_explicit_folder_status and normalized.get("processed_at"):
                # version=3 早期文件可能还没有 folder_complete 字段；processed_at 只在最终完成时写入。
                folder_done = True
            normalized[CONTACT_WRITEBACK_COMPLETE_KEY] = contact_done or folder_done
            normalized[FOLDER_COMPLETE_KEY] = folder_done
            if normalized.get(PRODUCT_TYPE_KEY) == PRODUCT_TYPE_TENT_VALUE and _normalize_bool(
                normalized.get(SKU_ADJUSTMENT_REQUIRED_KEY)
            ):
                normalized[SKU_ADJUSTMENT_REQUIRED_KEY] = True
            if normalized.get(PRODUCT_TYPE_KEY) == PRODUCT_TYPE_TENT_VALUE and _normalize_bool(
                normalized.get(PACKAGE_SPLIT_REQUIRED_KEY)
            ):
                normalized[PACKAGE_SPLIT_REQUIRED_KEY] = True
            if normalized.get(PRODUCT_TYPE_KEY) == PRODUCT_TYPE_TENT_VALUE and _normalize_bool(
                normalized.get(INSTRUCTION_REMARK_REQUIRED_KEY)
            ):
                normalized[INSTRUCTION_REMARK_REQUIRED_KEY] = True
            normalized = _apply_workflow_status(normalized)
        normalized.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
        normalized.pop(LEGACY_FOLDER_DONE_KEY, None)
        orders[key] = normalized
    return orders


def _load_legacy_txt_payload(text: str) -> dict[str, Any]:
    """加载旧格式txt载荷。"""
    orders: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        platform_order_no = line.split()[0].strip()
        if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
            continue
        parts = line.split()
        orders[platform_order_no] = {
            **_base_record(platform_order_no, parts[1] if len(parts) > 1 else None),
            CONTACT_WRITEBACK_COMPLETE_KEY: True,
            FOLDER_COMPLETE_KEY: True,
            "workflow_status": "completed",
            "processed_at": None,
            "source": "legacy_txt",
        }
    payload = _empty_payload()
    payload[ORDERS_KEY] = orders
    return payload


def _merge_contact_stage_records(
    orders: dict[str, dict[str, Any]],
    contact_orders: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """处理合并 联系方式 阶段记录相关逻辑，并返回后续流程所需结果。"""
    for platform_order_no, contact_record in contact_orders.items():
        old_record = orders.get(platform_order_no, _base_record(platform_order_no, contact_record.get("system_order_no")))
        merged = {
            **old_record,
            "system_order_no": old_record.get("system_order_no") or contact_record.get("system_order_no"),
            CONTACT_WRITEBACK_COMPLETE_KEY: True,
            "contact_status": contact_record.get("contact_status") or old_record.get("contact_status") or "written",
            "contact_completed_at": contact_record.get("contact_completed_at") or old_record.get("contact_completed_at"),
            "last_seen_at": contact_record.get("last_seen_at") or old_record.get("last_seen_at"),
        }
        orders[platform_order_no] = _apply_workflow_status(merged)
    return orders


def _normalize_payload(payload: Any) -> dict[str, Any]:
    """规范化载荷，便于后续匹配和比较。"""
    if isinstance(payload, list):
        normalized = _empty_payload()
        normalized[ORDERS_KEY] = _coerce_order_map(payload, legacy_final_done=True)
        return normalized
    if not isinstance(payload, dict):
        return _empty_payload()

    normalized = _empty_payload()
    raw_orders = payload.get(ORDERS_KEY, {})
    # version < 3 的 orders 是最终完成列表；version 3 起 orders 是带阶段状态的统一记录。
    legacy_final_done = int(payload.get("version") or 1) < 3
    orders = _coerce_order_map(raw_orders, legacy_final_done=legacy_final_done)
    legacy_contact_orders = _coerce_order_map(payload.get(LEGACY_CONTACT_ORDERS_KEY, {}), legacy_final_done=False)
    normalized[ORDERS_KEY] = _merge_contact_stage_records(orders, legacy_contact_orders)
    normalized["updated_at"] = payload.get("updated_at")
    normalized["version"] = 3
    return normalized


def _load_raw_payload(path: Path) -> dict[str, Any]:
    """加载raw 载荷。"""

    if not path.exists():
        return _empty_payload()

    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return _empty_payload()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _load_legacy_txt_payload(text)
    return _normalize_payload(payload)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入，避免脚本被强制关闭时留下半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def migrate_dedupe_file(path: str | Path) -> None:
    """把旧 processed 文件落盘迁移为单记录多状态结构。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import migrate_dedupe_file as migrate_sqlite_dedupe_file

        migrate_sqlite_dedupe_file(path)
        return
    dedupe_path = Path(path)
    if not dedupe_path.exists():
        return
    payload = _load_raw_payload(dedupe_path)
    payload["version"] = 3
    payload["updated_at"] = payload.get("updated_at") or _now_text()
    _atomic_write_json(dedupe_path, payload)


def load_processed_platform_orders(path: str | Path) -> set[str]:
    """读取最终完成订单。

    非帐篷订单只要求联系方式和文件夹完成；帐篷订单如果被标记为需要 SKU 阶段，
    则必须 sku_adjustment_complete=true 后才会在列表页被跳过。
    """

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import load_processed_platform_orders as load_sqlite_orders

        return load_sqlite_orders(path)
    payload = _load_raw_payload(Path(path))
    orders = payload.get(ORDERS_KEY) or {}
    return {
        str(platform_order_no)
        for platform_order_no, record in orders.items()
        if PLATFORM_ORDER_RE.fullmatch(str(platform_order_no)) and isinstance(record, dict) and _is_final_complete(record)
    }


def load_contact_writeback_platform_orders(path: str | Path) -> set[str]:
    """读取联系方式阶段已完成订单；这些订单下轮巡检可以跳过电话邮箱写回。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import (
            load_contact_writeback_platform_orders as load_sqlite_contact_orders,
        )

        return load_sqlite_contact_orders(path)
    payload = _load_raw_payload(Path(path))
    orders = payload.get(ORDERS_KEY) or {}
    return {
        str(platform_order_no)
        for platform_order_no, record in orders.items()
        if PLATFORM_ORDER_RE.fullmatch(str(platform_order_no))
        and isinstance(record, dict)
        and (
            _normalize_bool(record.get(CONTACT_WRITEBACK_COMPLETE_KEY))
            or _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
        )
    }


def load_folder_complete_platform_orders(path: str | Path) -> set[str]:
    """读取文件夹阶段已完成订单。

    帐篷订单可能文件夹已完成但 SKU 未完成，此时不能最终跳过，但下轮不应重复创建文件夹。
    """

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import load_folder_complete_platform_orders as load_sqlite_folder_orders

        return load_sqlite_folder_orders(path)
    payload = _load_raw_payload(Path(path))
    orders = payload.get(ORDERS_KEY) or {}
    return {
        str(platform_order_no)
        for platform_order_no, record in orders.items()
        if PLATFORM_ORDER_RE.fullmatch(str(platform_order_no))
        and isinstance(record, dict)
        and _normalize_bool(record.get(FOLDER_COMPLETE_KEY))
    }


def is_platform_order_processed(path: str | Path, platform_order_no: str) -> bool:
    """进入详情页前调用，确保完整完成的订单不再重复处理。"""

    return platform_order_no in load_processed_platform_orders(path)


def is_contact_writeback_done(path: str | Path, platform_order_no: str) -> bool:
    """判断联系方式阶段是否已完成；完成后下轮巡检可以直接补后续阶段。"""

    return platform_order_no in load_contact_writeback_platform_orders(path)


def is_folder_complete(path: str | Path, platform_order_no: str) -> bool:
    """判断文件夹和 zip 阶段是否已完成，用于帐篷订单只补 SKU。"""

    return platform_order_no in load_folder_complete_platform_orders(path)


def is_sku_adjustment_done(path: str | Path, platform_order_no: str) -> bool:
    """判断帐篷订单 SKU 调整是否已完成。非帐篷订单通常不会写入该字段。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import is_sku_adjustment_done as is_sqlite_sku_done

        return is_sqlite_sku_done(path, platform_order_no)
    payload = _load_raw_payload(Path(path))
    record = (payload.get(ORDERS_KEY) or {}).get(platform_order_no)
    return isinstance(record, dict) and _normalize_bool(record.get(SKU_ADJUSTMENT_COMPLETE_KEY))


def is_package_split_done(path: str | Path, platform_order_no: str) -> bool:
    """判断帐篷订单拆分包裹阶段是否已完成或已明确无需拆包。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import is_package_split_done as is_sqlite_package_done

        return is_sqlite_package_done(path, platform_order_no)
    payload = _load_raw_payload(Path(path))
    record = (payload.get(ORDERS_KEY) or {}).get(platform_order_no)
    return isinstance(record, dict) and _normalize_bool(record.get(PACKAGE_SPLIT_COMPLETE_KEY))


def is_instruction_remark_done(path: str | Path, platform_order_no: str) -> bool:
    """判断帐篷说明书客服备注阶段是否已完成。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import is_instruction_remark_done as is_sqlite_remark_done

        return is_sqlite_remark_done(path, platform_order_no)
    payload = _load_raw_payload(Path(path))
    record = (payload.get(ORDERS_KEY) or {}).get(platform_order_no)
    return isinstance(record, dict) and _normalize_bool(record.get(INSTRUCTION_REMARK_COMPLETE_KEY))


def is_warehouse_logistics_done(path: str | Path, platform_order_no: str) -> bool:
    """判断帐篷仓库物流阶段是否已完成或已明确无需修改。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import is_warehouse_logistics_done as is_sqlite_warehouse_done

        return is_sqlite_warehouse_done(path, platform_order_no)
    payload = _load_raw_payload(Path(path))
    record = (payload.get(ORDERS_KEY) or {}).get(platform_order_no)
    return isinstance(record, dict) and _normalize_bool(record.get(WAREHOUSE_LOGISTICS_COMPLETE_KEY))


def append_contact_writeback_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    contact_status: str = "written",
    contact_verified: bool = False,
    contact_verification_method: str | None = None,
) -> None:
    """记录联系方式阶段已完成，但不代表文件夹也完成。"""

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import append_contact_writeback_platform_order as append_sqlite_contact

        append_sqlite_contact(
            path,
            platform_order_no,
            system_order_no,
            contact_status=contact_status,
            contact_verified=contact_verified,
            contact_verification_method=contact_verification_method,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        "contact_status": contact_status,
        "contact_completed_at": old_record.get("contact_completed_at") or _now_text(),
        "last_seen_at": _now_text(),
    }
    if contact_verified:
        record["contact_writeback_verified"] = True
        record["contact_verification_method"] = (
            str(contact_verification_method or "").strip()
            or "browser_detail_reopen"
        )
        record["contact_verified_at"] = _now_text()
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


def append_folder_complete_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    product_type: str | None = None,
    sku_adjustment_required: bool = False,
) -> None:
    """记录文件夹和 zip 阶段完成。

    帐篷订单在这里会额外标记 sku_adjustment_required=true；
    这样文件夹失败不会重复写回联系方式，SKU 失败也不会重复建文件夹。
    """

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import append_folder_complete_platform_order as append_sqlite_folder

        append_sqlite_folder(
            path,
            platform_order_no,
            system_order_no,
            product_type=product_type,
            sku_adjustment_required=sku_adjustment_required,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        "folder_completed_at": old_record.get("folder_completed_at") or _now_text(),
        "last_seen_at": _now_text(),
    }
    if product_type:
        record[PRODUCT_TYPE_KEY] = product_type
    if sku_adjustment_required:
        record[SKU_ADJUSTMENT_REQUIRED_KEY] = True
        record[PRODUCT_TYPE_KEY] = product_type or PRODUCT_TYPE_TENT_VALUE
    elif not _normalize_bool(record.get(SKU_ADJUSTMENT_REQUIRED_KEY)):
        record.pop(SKU_ADJUSTMENT_REQUIRED_KEY, None)
        record.pop(SKU_ADJUSTMENT_COMPLETE_KEY, None)
        record.pop(PACKAGE_SPLIT_REQUIRED_KEY, None)
        record.pop(PACKAGE_SPLIT_COMPLETE_KEY, None)
        record.pop(INSTRUCTION_REMARK_REQUIRED_KEY, None)
        record.pop(INSTRUCTION_REMARK_COMPLETE_KEY, None)
        record.pop(WAREHOUSE_LOGISTICS_REQUIRED_KEY, None)
        record.pop(WAREHOUSE_LOGISTICS_COMPLETE_KEY, None)
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


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
    """记录帐篷 SKU 调整完成。

    非帐篷订单不会调用这个函数；若美国非本土地区由用户人工处理，也用同一状态标记完成。
    """

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import append_sku_adjustment_platform_order as append_sqlite_sku

        append_sqlite_sku(
            path,
            platform_order_no,
            system_order_no,
            sku_status=sku_status,
            instruction_replaced_at=instruction_replaced_at,
            instruction_customer_remark=instruction_customer_remark,
            workflow_kind=workflow_kind,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        SKU_ADJUSTMENT_REQUIRED_KEY: True,
        SKU_ADJUSTMENT_COMPLETE_KEY: True,
        PRODUCT_TYPE_KEY: old_record.get(PRODUCT_TYPE_KEY) or PRODUCT_TYPE_TENT_VALUE,
        "sku_adjustment_status": sku_status,
        "sku_adjustment_completed_at": old_record.get("sku_adjustment_completed_at") or _now_text(),
        "last_seen_at": _now_text(),
    }
    if instruction_replaced_at:
        record["instruction_replaced_at"] = str(instruction_replaced_at)
    if instruction_customer_remark:
        record["instruction_customer_remark"] = str(instruction_customer_remark)
    if workflow_kind:
        record["sku_adjustment_workflow_kind"] = str(workflow_kind)
    if not _normalize_bool(record.get(PACKAGE_SPLIT_COMPLETE_KEY)):
        record[PACKAGE_SPLIT_REQUIRED_KEY] = True
        record[PACKAGE_SPLIT_COMPLETE_KEY] = False
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    else:
        record.pop("processed_at", None)
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


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
    """记录帐篷拆分包裹阶段完成；无需拆包也会写入完成态。"""

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import append_package_split_platform_order as append_sqlite_package

        append_sqlite_package(
            path,
            platform_order_no,
            system_order_no,
            package_status=package_status,
            package_required=package_required,
            system_order_nos=system_order_nos,
            instruction_remark_required=instruction_remark_required,
            warehouse_plan_input=warehouse_plan_input,
            instruction_system_order_no=instruction_system_order_no,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        SKU_ADJUSTMENT_REQUIRED_KEY: True,
        SKU_ADJUSTMENT_COMPLETE_KEY: True,
        PACKAGE_SPLIT_REQUIRED_KEY: bool(package_required),
        PACKAGE_SPLIT_COMPLETE_KEY: True,
        PRODUCT_TYPE_KEY: old_record.get(PRODUCT_TYPE_KEY) or PRODUCT_TYPE_TENT_VALUE,
        "package_split_status": package_status,
        "package_split_completed_at": old_record.get("package_split_completed_at") or _now_text(),
        "package_split_system_order_nos": list(system_order_nos or []),
        "package_split_instruction_system_order_no": instruction_system_order_no
        or old_record.get("package_split_instruction_system_order_no"),
        "warehouse_logistics_plan_input": dict(
            warehouse_plan_input or old_record.get("warehouse_logistics_plan_input") or {}
        ),
        WAREHOUSE_LOGISTICS_REQUIRED_KEY: True,
        WAREHOUSE_LOGISTICS_COMPLETE_KEY: _normalize_bool(
            old_record.get(WAREHOUSE_LOGISTICS_COMPLETE_KEY)
        ),
        "last_seen_at": _now_text(),
    }
    if instruction_remark_required:
        record[INSTRUCTION_REMARK_REQUIRED_KEY] = True
        if not _normalize_bool(record.get(INSTRUCTION_REMARK_COMPLETE_KEY)):
            record[INSTRUCTION_REMARK_COMPLETE_KEY] = False
    elif not _normalize_bool(record.get(INSTRUCTION_REMARK_REQUIRED_KEY)):
        record.pop(INSTRUCTION_REMARK_REQUIRED_KEY, None)
        record.pop(INSTRUCTION_REMARK_COMPLETE_KEY, None)
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    else:
        record.pop("processed_at", None)
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


def append_instruction_remark_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    remark_status: str = "auto",
    target_system_order_no: str | None = None,
    warehouse_plan_input: dict[str, Any] | None = None,
) -> None:
    """记录帐篷说明书客服备注阶段完成。"""

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import (
            append_instruction_remark_platform_order as append_sqlite_instruction_remark,
        )

        append_sqlite_instruction_remark(
            path,
            platform_order_no,
            system_order_no,
            remark_status=remark_status,
            target_system_order_no=target_system_order_no,
            warehouse_plan_input=warehouse_plan_input,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        SKU_ADJUSTMENT_REQUIRED_KEY: True,
        SKU_ADJUSTMENT_COMPLETE_KEY: True,
        PACKAGE_SPLIT_COMPLETE_KEY: True,
        PRODUCT_TYPE_KEY: old_record.get(PRODUCT_TYPE_KEY) or PRODUCT_TYPE_TENT_VALUE,
        INSTRUCTION_REMARK_REQUIRED_KEY: True,
        INSTRUCTION_REMARK_COMPLETE_KEY: True,
        WAREHOUSE_LOGISTICS_REQUIRED_KEY: True,
        WAREHOUSE_LOGISTICS_COMPLETE_KEY: _normalize_bool(
            old_record.get(WAREHOUSE_LOGISTICS_COMPLETE_KEY)
        ),
        "instruction_remark_status": remark_status,
        "instruction_remark_completed_at": old_record.get("instruction_remark_completed_at") or _now_text(),
        "instruction_remark_target_system_order_no": target_system_order_no or old_record.get("instruction_remark_target_system_order_no"),
        "warehouse_logistics_plan_input": dict(
            warehouse_plan_input or old_record.get("warehouse_logistics_plan_input") or {}
        ),
        "last_seen_at": _now_text(),
    }
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    else:
        record.pop("processed_at", None)
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


def append_processed_platform_order(
    path: str | Path,
    platform_order_no: str,
    system_order_no: str | None = None,
    *,
    product_type: str | None = None,
    sku_adjustment_required: bool = False,
) -> None:
    """记录最终完成订单。

    对普通商品，这会同步标记联系方式和文件夹完成；
    对帐篷商品，如果调用方声明需要 SKU 阶段，则只有 SKU 已完成时才写入最终完成语义。
    """

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import append_processed_platform_order as append_sqlite_processed

        append_sqlite_processed(
            path,
            platform_order_no,
            system_order_no,
            product_type=product_type,
            sku_adjustment_required=sku_adjustment_required,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        "contact_completed_at": old_record.get("contact_completed_at") or _now_text(),
        "folder_completed_at": old_record.get("folder_completed_at") or _now_text(),
        "last_seen_at": _now_text(),
    }
    if product_type:
        record[PRODUCT_TYPE_KEY] = product_type
    if sku_adjustment_required:
        record[SKU_ADJUSTMENT_REQUIRED_KEY] = True
        record[PRODUCT_TYPE_KEY] = product_type or PRODUCT_TYPE_TENT_VALUE
    elif not _normalize_bool(record.get(SKU_ADJUSTMENT_REQUIRED_KEY)):
        record.pop(SKU_ADJUSTMENT_REQUIRED_KEY, None)
        record.pop(SKU_ADJUSTMENT_COMPLETE_KEY, None)
        record.pop(PACKAGE_SPLIT_REQUIRED_KEY, None)
        record.pop(PACKAGE_SPLIT_COMPLETE_KEY, None)
        record.pop(INSTRUCTION_REMARK_REQUIRED_KEY, None)
        record.pop(INSTRUCTION_REMARK_COMPLETE_KEY, None)
        record.pop(WAREHOUSE_LOGISTICS_REQUIRED_KEY, None)
        record.pop(WAREHOUSE_LOGISTICS_COMPLETE_KEY, None)
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


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
    """记录帐篷仓库物流阶段完成；无写入的 KEEP/纯布面也会完成。"""

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import (
            append_warehouse_logistics_platform_order as append_sqlite_warehouse,
        )

        append_sqlite_warehouse(
            path,
            platform_order_no,
            system_order_no,
            warehouse_status=warehouse_status,
            decisions=decisions,
            write_results=write_results,
            result_detail=result_detail,
            warehouse_required=warehouse_required,
        )
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no) if isinstance(orders.get(platform_order_no), dict) else {}
    record = {
        **_base_record(platform_order_no, system_order_no),
        **old_record,
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no or old_record.get("system_order_no"),
        CONTACT_WRITEBACK_COMPLETE_KEY: True,
        FOLDER_COMPLETE_KEY: True,
        SKU_ADJUSTMENT_REQUIRED_KEY: True,
        SKU_ADJUSTMENT_COMPLETE_KEY: True,
        PACKAGE_SPLIT_COMPLETE_KEY: True,
        WAREHOUSE_LOGISTICS_REQUIRED_KEY: bool(warehouse_required),
        WAREHOUSE_LOGISTICS_COMPLETE_KEY: True,
        PRODUCT_TYPE_KEY: old_record.get(PRODUCT_TYPE_KEY) or PRODUCT_TYPE_TENT_VALUE,
        "warehouse_logistics_status": warehouse_status,
        "warehouse_logistics_completed_at": old_record.get("warehouse_logistics_completed_at")
        or _now_text(),
        "warehouse_logistics_decisions": list(decisions or []),
        "warehouse_logistics_write_results": list(write_results or []),
        "warehouse_logistics_result_detail": str(result_detail or "").strip() or None,
        "last_seen_at": _now_text(),
    }
    if _is_final_complete(record):
        record["processed_at"] = old_record.get("processed_at") or _now_text()
    else:
        record.pop("processed_at", None)
    record.pop(LEGACY_CONTACT_WRITEBACK_KEY, None)
    record.pop(LEGACY_FOLDER_DONE_KEY, None)
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


def update_warehouse_logistics_plan_input(
    path: str | Path,
    platform_order_no: str,
    plan_input: dict[str, Any],
) -> None:
    """Refresh the persisted warehouse plan without completing or replaying a stage."""

    if not PLATFORM_ORDER_RE.fullmatch(platform_order_no):
        raise ValueError(f"Invalid platform order number: {platform_order_no}")
    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import (
            update_warehouse_logistics_plan_input as update_sqlite_plan_input,
        )

        update_sqlite_plan_input(path, platform_order_no, plan_input)
        return

    dedupe_path = Path(path)
    payload = _load_raw_payload(dedupe_path)
    orders: dict[str, Any] = dict(payload.get(ORDERS_KEY) or {})
    old_record = orders.get(platform_order_no)
    if not isinstance(old_record, dict):
        raise KeyError(platform_order_no)
    record = {
        **old_record,
        "warehouse_logistics_plan_input": dict(plan_input),
        "last_seen_at": _now_text(),
    }
    orders[platform_order_no] = _apply_workflow_status(record)
    payload["version"] = 3
    payload["updated_at"] = _now_text()
    payload[ORDERS_KEY] = orders
    _atomic_write_json(dedupe_path, payload)


def load_order_workflow_record(
    path: str | Path,
    platform_order_no: str,
) -> dict[str, Any] | None:
    """读取单个平台单号的兼容工作流记录，供拆单后阶段恢复使用。"""

    if is_sqlite_dedupe_path(path):
        from .sqlite_dedupe import get_store

        return get_store(path).get_legacy_record(platform_order_no)
    payload = _load_raw_payload(Path(path))
    record = (payload.get(ORDERS_KEY) or {}).get(platform_order_no)
    return dict(record) if isinstance(record, dict) else None


def import_dedupe_json_to_sqlite(
    source_json: str | Path,
    sqlite_path: str | Path,
    *,
    create_backup: bool = True,
    overwrite_existing: bool = False,
):
    """显式把旧 JSON 状态导入 SQLite；普通运行不会自动改写原文件。"""

    if not is_sqlite_dedupe_path(sqlite_path):
        raise ValueError("SQLite 去重文件必须使用 .sqlite3 或 .db 后缀。")
    from .sqlite_dedupe import import_dedupe_json_to_sqlite as import_sqlite

    return import_sqlite(
        source_json,
        sqlite_path,
        create_backup=create_backup,
        overwrite_existing=overwrite_existing,
    )


def export_dedupe_sqlite_to_json(
    sqlite_path: str | Path,
    target_json: str | Path,
) -> Path:
    """仅在用户选择回退时，把 SQLite 状态显式导出成脚本可读 JSON。"""

    if not is_sqlite_dedupe_path(sqlite_path):
        raise ValueError("SQLite 去重文件必须使用 .sqlite3 或 .db 后缀。")
    from .sqlite_dedupe import export_dedupe_sqlite_to_json as export_sqlite

    return export_sqlite(sqlite_path, target_json)
