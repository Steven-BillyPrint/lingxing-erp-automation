"""Lingxing OpenAPI implementation for customization-order mutations.

Only operations documented by Lingxing are implemented here.  In particular,
buyer e-mail and the unmasked shipping address intentionally remain outside of
this adapter and continue to use the retained browser steps.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from lingxing_automation.models import CustomZipFile, OrderCustomZipBundle, OrderFolderLine
from lingxing_automation.services.custom_order_api import (
    ApiWriteOutcome,
    InstructionRemarkOutcome,
    OrderProcessingStatus,
    WarehouseLogisticsOutcome,
)
from lingxing_automation.services.custom_attachment_downloader import unique_zip_target_path
from lingxing_automation.services.custom_zip_downloader import (
    CUSTOM_ZIP_DOWNLOADED,
    CUSTOM_ZIP_DOWNLOAD_ERROR,
    CUSTOM_ZIP_NOT_FOUND,
)
from lingxing_automation.services.tent_package_split_adjuster import TentPackageSplitResult
from lingxing_automation.services.tent_package_split_planner import TentPackageSplitPlan
from lingxing_automation.services.tent_sku_adjuster import (
    TentSkuAdjustmentResult,
    _merge_instruction_customer_remark,
)
from lingxing_automation.services.tent_sku_planner import TentSkuAdjustmentPlan
from lingxing_automation.services.tent_sku_rules import INSTRUCTION_SKU
from lingxing_automation.services.tent_warehouse_routing import (
    TentRoutingItem,
    TentRoutingPackage,
    TentWarehouseRoutingDecision,
    build_tent_warehouse_routing_plan,
)

from .capabilities import (
    CapabilityUnavailable,
    ManualReviewRequired,
    MutationResult,
    MutationState,
)
from .lingxing_gateway import (
    AttachmentData,
    LingxingGateway,
    MutationVerification,
    OrderRecord,
    VerificationOutcome,
)
from .order_status import BUYER_CANCEL_REQUEST_TEXT, has_buyer_cancel_request
from .readback import SleepFunc, iter_readback_attempts, normalize_readback_delays


MAX_CUSTOM_ZIP_BYTES = 256 * 1024 * 1024
MAX_CUSTOM_ZIP_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_CUSTOM_ZIP_ENTRIES = 10_000
MAX_CUSTOM_JSON_BYTES = 16 * 1024 * 1024
_SAFE_ORDER_DIRECTORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
DEFAULT_WAREHOUSE_PROJECTION_DELAYS_SECONDS: tuple[float, ...] = (
    0.0,
    *(3.0 for _ in range(10)),
    *(5.0 for _ in range(12)),
    *(15.0 for _ in range(14)),
)


class CustomOrderApiPlanError(RuntimeError):
    """The API payload cannot be proven to target the intended order rows."""


@dataclass(frozen=True)
class _ApiOrderItem:
    item_id: str | None
    order_item_no: str | None
    msku: str | None
    local_sku: str | None
    quantity: int
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class _ApiOrderSnapshot:
    global_order_no: str
    platform_order_nos: tuple[str, ...]
    shipping_deadline: str | None
    remark: str
    items: tuple[_ApiOrderItem, ...]
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class _CustomZipCandidate:
    row_index: int
    platform_order_no: str
    order_item_id: str
    sku: str | None
    msku: str | None
    file_id: str
    file_name: str


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _sku_key(value: object) -> str:
    return "".join(str(value or "").split()).upper()


def _positive_quantity(value: object, *, label: str) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise CustomOrderApiPlanError(f"{label}缺少有效数量。") from exc
    if quantity <= 0:
        raise CustomOrderApiPlanError(f"{label}数量必须大于 0。")
    return quantity


def _deadline_text(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _first(mapping: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _safe_order_directory(value: object, *, label: str) -> str:
    normalized = _text(value)
    if normalized is None or not _SAFE_ORDER_DIRECTORY_RE.fullmatch(normalized):
        raise CustomOrderApiPlanError(f"{label}包含不安全字符，拒绝创建 staging 路径。")
    if normalized in {".", ".."}:
        raise CustomOrderApiPlanError(f"{label}不能是相对路径片段。")
    return normalized


def _safe_zip_filename(value: object, *, label: str) -> str:
    raw = _text(value)
    if raw is None:
        raise CustomOrderApiPlanError(f"{label}缺少文件名。")
    if raw != str(value) or len(raw) > 240:
        raise CustomOrderApiPlanError(f"{label}文件名格式不安全。")
    if Path(raw).is_absolute() or Path(raw).name != raw or _WINDOWS_INVALID_FILENAME_RE.search(raw):
        raise CustomOrderApiPlanError(f"{label}文件名包含路径或 Windows 非法字符。")
    if raw.endswith((" ", ".")):
        raise CustomOrderApiPlanError(f"{label}文件名不能以空格或句点结尾。")
    reserved_stem = raw.split(".", 1)[0].upper()
    if reserved_stem in _WINDOWS_RESERVED_NAMES:
        raise CustomOrderApiPlanError(f"{label}使用了 Windows 保留文件名。")
    if not raw.lower().endswith(".zip"):
        raise CustomOrderApiPlanError(f"{label}不是 ZIP 文件名。")
    return raw


def _safe_zip_member_name(value: str) -> None:
    normalized = str(value).replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise CustomOrderApiPlanError(f"ZIP 内部包含不安全路径：{value[:160]}")


def _validate_custom_zip_content(
    content: bytes,
    *,
    platform_order_no: str,
    order_item_id: str,
) -> None:
    size = len(content)
    if size < 4 or size > MAX_CUSTOM_ZIP_BYTES:
        raise CustomOrderApiPlanError(
            f"订单行 {order_item_id} 的 ZIP 大小不在安全范围内：{size} bytes。"
        )
    if not content.startswith(_ZIP_MAGIC_PREFIXES):
        raise CustomOrderApiPlanError(f"订单行 {order_item_id} 的附件缺少 ZIP 魔数。")
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_CUSTOM_ZIP_ENTRIES:
                raise CustomOrderApiPlanError(
                    f"订单行 {order_item_id} 的 ZIP 文件条目数量不在安全范围内。"
                )
            uncompressed_size = sum(int(entry.file_size) for entry in entries)
            if uncompressed_size > MAX_CUSTOM_ZIP_UNCOMPRESSED_BYTES:
                raise CustomOrderApiPlanError(
                    f"订单行 {order_item_id} 的 ZIP 解压后体积超过安全上限。"
                )
            for entry in entries:
                _safe_zip_member_name(entry.filename)
                if entry.flag_bits & 0x1:
                    raise CustomOrderApiPlanError(
                        f"订单行 {order_item_id} 的 ZIP 含加密条目，无法安全校验。"
                    )
            damaged = archive.testzip()
            if damaged:
                raise CustomOrderApiPlanError(
                    f"订单行 {order_item_id} 的 ZIP 校验失败：{damaged[:160]}"
                )

            observed_item_ids: set[str] = set()
            observed_order_ids: set[str] = set()
            for entry in entries:
                if entry.is_dir() or not entry.filename.lower().endswith(".json"):
                    continue
                if entry.file_size > MAX_CUSTOM_JSON_BYTES:
                    raise CustomOrderApiPlanError(
                        f"订单行 {order_item_id} 的定制 JSON 超过安全上限。"
                    )
                try:
                    payload = json.loads(archive.read(entry).decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CustomOrderApiPlanError(
                        f"订单行 {order_item_id} 的定制 JSON 无法解析。"
                    ) from exc
                if not isinstance(payload, Mapping):
                    continue
                item_value = _text(_first(payload, "orderItemId", "OrderItemId", "order_item_id"))
                order_value = _text(_first(payload, "orderId", "OrderId", "order_id"))
                if item_value:
                    observed_item_ids.add(item_value)
                if order_value:
                    observed_order_ids.add(order_value)
            if observed_item_ids != {order_item_id}:
                raise CustomOrderApiPlanError(
                    f"订单行 {order_item_id} 的 ZIP 内 orderItemId 不唯一或不匹配。"
                )
            if observed_order_ids != {platform_order_no}:
                raise CustomOrderApiPlanError(
                    f"订单行 {order_item_id} 的 ZIP 内 orderId 与平台单号不匹配。"
                )
    except zipfile.BadZipFile as exc:
        raise CustomOrderApiPlanError(f"订单行 {order_item_id} 的附件不是有效 ZIP。") from exc


def _atomic_write_custom_zip(staging_dir: Path, filename: str, content: bytes) -> Path:
    target = unique_zip_target_path(staging_dir, filename)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".lingxing-api-",
            suffix=".tmp",
            dir=staging_dir,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _custom_zip_candidates_from_detail(
    payload: Mapping[str, Any],
    *,
    platform_order_no: str,
    system_order_no: str,
    expected_zip_count: int | None,
    expected_order_item_ids: set[str] | None,
) -> tuple[list[_CustomZipCandidate], list[str]]:
    detail_order_no = _text(_first(payload, "order_number", "orderNumber", "system_order_no"))
    if detail_order_no != system_order_no:
        raise CustomOrderApiPlanError(
            f"订单详情系统单号不匹配：期望 {system_order_no}，实际 {detail_order_no or '-'}。"
        )
    raw_items = _sequence(_first(payload, "order_item", "orderItem", "order_items"))
    if not raw_items:
        raise CustomOrderApiPlanError(f"系统订单 {system_order_no} 的详情缺少商品行。")

    all_candidates: list[_CustomZipCandidate] = []
    seen_file_ids: set[str] = set()
    for row_index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise CustomOrderApiPlanError(f"系统订单 {system_order_no} 的商品行格式无效。")
        raw_attachments = _sequence(
            _first(raw_item, "newAttachments", "new_attachments", "newattachments")
        )
        zip_attachments: list[Mapping[str, Any]] = []
        for raw_attachment in raw_attachments:
            if not isinstance(raw_attachment, Mapping):
                raise CustomOrderApiPlanError(f"系统订单 {system_order_no} 的新附件格式无效。")
            try:
                file_type = int(_first(raw_attachment, "file_type", "fileType"))
            except (TypeError, ValueError):
                continue
            if file_type == 2:
                zip_attachments.append(raw_attachment)
        if not zip_attachments:
            continue

        row_platform_no = _text(
            _first(raw_item, "platform_order_id", "platformOrderId", "platform_order_no")
        )
        if row_platform_no != platform_order_no:
            raise CustomOrderApiPlanError(
                f"第 {row_index} 行 ZIP 的平台单号不匹配：{row_platform_no or '-'}。"
            )
        order_item_id = _text(
            _first(raw_item, "order_item_no", "orderItemNo", "order_item_id", "orderItemId")
        )
        if order_item_id is None:
            raise CustomOrderApiPlanError(f"第 {row_index} 行 ZIP 缺少 order_item_id。")
        for raw_attachment in zip_attachments:
            file_id = _text(_first(raw_attachment, "file_id", "fileId", "id"))
            if file_id is None:
                raise CustomOrderApiPlanError(f"订单行 {order_item_id} 的 ZIP 缺少 file_id。")
            if file_id in seen_file_ids:
                raise CustomOrderApiPlanError(f"订单详情重复返回 ZIP file_id：{file_id}。")
            seen_file_ids.add(file_id)
            file_name = _safe_zip_filename(
                _first(raw_attachment, "file_name", "fileName", "name"),
                label=f"订单行 {order_item_id}",
            )
            all_candidates.append(
                _CustomZipCandidate(
                    row_index=row_index,
                    platform_order_no=row_platform_no,
                    order_item_id=order_item_id,
                    sku=_text(_first(raw_item, "sku", "SKU")),
                    msku=_text(_first(raw_item, "MSKU", "msku")),
                    file_id=file_id,
                    file_name=file_name,
                )
            )

    warnings: list[str] = []
    if expected_order_item_ids is not None:
        selected: list[_CustomZipCandidate] = []
        for order_item_id in sorted(expected_order_item_ids):
            matches = [row for row in all_candidates if row.order_item_id == order_item_id]
            if len(matches) != 1:
                raise CustomOrderApiPlanError(
                    f"期望订单行 {order_item_id} 必须且只能匹配一个 ZIP，实际 {len(matches)} 个。"
                )
            selected.extend(matches)
        for candidate in all_candidates:
            if candidate.order_item_id not in expected_order_item_ids:
                warnings.append(
                    f"ignored_unexpected_custom_zip_order_item:{candidate.order_item_id}"
                )
        selected.sort(key=lambda row: row.row_index)
    else:
        selected = list(all_candidates)
        counts = Counter(row.order_item_id for row in selected)
        duplicate_item_ids = sorted(key for key, count in counts.items() if count != 1)
        if duplicate_item_ids:
            raise CustomOrderApiPlanError(
                "订单详情中的同一 order_item_id 匹配到多个 ZIP："
                + ", ".join(duplicate_item_ids)
            )

    if expected_zip_count is not None and len(selected) != expected_zip_count:
        raise CustomOrderApiPlanError(
            f"定制 ZIP 数量不匹配：期望 {expected_zip_count} 个，API 详情为 {len(selected)} 个。"
        )
    if not selected and expected_zip_count != 0:
        raise CustomOrderApiPlanError("领星订单详情中没有匹配的定制 ZIP 附件。")
    return selected, warnings


def _snapshot(record: OrderRecord) -> _ApiOrderSnapshot:
    payload = record.payload
    global_order_no = _text(record.global_order_no) or _text(
        _first(payload, "global_order_no", "globalOrderNo", "system_order_no")
    )
    if global_order_no is None:
        raise CustomOrderApiPlanError("领星订单列表缺少全局系统单号。")

    platform_order_nos: list[str] = []
    if record.order_number:
        platform_order_nos.append(record.order_number)
    for raw in _sequence(_first(payload, "platform_info", "platformInfo")):
        if not isinstance(raw, Mapping):
            continue
        for key in ("platform_order_no", "platform_order_name", "order_number"):
            value = _text(raw.get(key))
            if value and value not in platform_order_nos:
                platform_order_nos.append(value)

    raw_items = _sequence(
        _first(payload, "item_info", "itemInfo", "order_item_list", "orderItemList")
    )
    items: list[_ApiOrderItem] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        platform_no = _text(_first(raw, "platform_order_no", "platformOrderNo"))
        if platform_no and platform_no not in platform_order_nos:
            platform_order_nos.append(platform_no)
        quantity = _positive_quantity(
            _first(raw, "quantity", "qty", "quantity_ordered"),
            label=f"系统订单 {global_order_no} 商品行",
        )
        items.append(
            _ApiOrderItem(
                item_id=_text(_first(raw, "id", "item_id", "itemId")),
                order_item_no=_text(
                    _first(raw, "order_item_no", "orderItemNo", "order_item_id", "orderItemId")
                ),
                msku=_text(_first(raw, "msku", "seller_sku", "sellerSku")),
                local_sku=_text(_first(raw, "local_sku", "localSku", "sku")),
                quantity=quantity,
                payload=dict(raw),
            )
        )

    return _ApiOrderSnapshot(
        global_order_no=global_order_no,
        platform_order_nos=tuple(platform_order_nos),
        shipping_deadline=_deadline_text(
            _first(payload, "global_latest_ship_time", "globalLatestShipTime", "latest_ship_time")
        ),
        remark=_text(_first(payload, "remark", "customer_service_remark", "customerServiceRemark")) or "",
        items=tuple(items),
        payload=dict(payload),
    )


def _snapshot_summary(snapshot: _ApiOrderSnapshot) -> dict[str, Any]:
    return {
        "global_order_no": snapshot.global_order_no,
        "platform_order_nos": list(snapshot.platform_order_nos),
        "remark": snapshot.remark,
        "items": [
            {
                "id": item.item_id,
                "order_item_no": item.order_item_no,
                "msku": item.msku,
                "local_sku": item.local_sku,
                "quantity": item.quantity,
            }
            for item in snapshot.items
        ],
    }


def _snapshot_receiver_phone(snapshot: _ApiOrderSnapshot) -> str | None:
    containers: list[Mapping[str, Any]] = [snapshot.payload]
    for key in ("address_info", "addressInfo", "shipping_address", "shippingAddress"):
        value = snapshot.payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for raw in _sequence(_first(snapshot.payload, "platform_info", "platformInfo")):
        if isinstance(raw, Mapping):
            containers.append(raw)
            address = _first(raw, "address_info", "addressInfo")
            if isinstance(address, Mapping):
                containers.append(address)
    for container in containers:
        value = _text(
            _first(
                container,
                "receiver_tel",
                "receiverTel",
                "receiver_phone",
                "receiverPhone",
                "phone",
            )
        )
        if value:
            return value
    return None


def _snapshot_shipping_route(snapshot: _ApiOrderSnapshot) -> tuple[str | None, str | None]:
    """读取订单当前仓库和物流 ID；不从商品行中做模糊递归搜索。"""

    containers: list[Mapping[str, Any]] = [snapshot.payload]
    for key in (
        "logistics",
        "logistics_info",
        "logisticsInfo",
        "warehouse_info",
        "warehouseInfo",
        "shipping_info",
        "shippingInfo",
    ):
        value = snapshot.payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    warehouse_id = None
    logistics_type_id = None
    for container in containers:
        if warehouse_id is None:
            warehouse_id = _positive_identifier(
                _first(container, "sys_wid", "sysWid", "warehouse_id", "warehouseId", "wid")
            )
        if logistics_type_id is None:
            logistics_type_id = _positive_identifier(
                _first(
                    container,
                    "logistics_type_id",
                    "logisticsTypeId",
                    "logistics_id",
                    "logisticsId",
                )
            )
    return warehouse_id, logistics_type_id


def _positive_identifier(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return str(int(text)) if int(text) > 0 else None
    except (TypeError, ValueError):
        return text if text not in {"0", "-"} else None


def _wire_identifier(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _lookup_payload_value(payload: Mapping[str, Any], *keys: str) -> str | None:
    return _text(_first(payload, *keys))


def _phone_identity(value: object) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _mutation_outcome(result: MutationResult) -> ApiWriteOutcome:
    if result.state is MutationState.SUCCEEDED:
        status = "succeeded"
    elif result.state in {MutationState.UNKNOWN, MutationState.MANUAL_REVIEW}:
        status = "manual_review"
    else:
        status = "failed"
    return ApiWriteOutcome(
        status=status,
        message=result.message,
        request_id=result.request_id,
        details={
            **dict(result.details),
            "definitely_not_executed": bool(result.definitely_not_executed),
            "source": result.source,
        },
    )


def _readback_details(
    attempt: Any,
    *,
    transient_error_count: int = 0,
    last_error: Exception | None = None,
) -> dict[str, Any]:
    """Return safe readback diagnostics without logging API response contents."""

    details = dict(attempt.details()) if attempt is not None else {}
    if transient_error_count:
        details["readback_transient_error_count"] = transient_error_count
    if last_error is not None:
        details["readback_last_error_type"] = type(last_error).__name__
    return details


class LingxingCustomOrderApiOperations:
    """API-first custom-order operations backed by :class:`LingxingGateway`.

    This adapter never performs a browser action itself.  It preserves the
    gateway's definitive-not-executed marker so the orchestration layer may
    ask the operator before invoking the retained browser implementation.
    Ambiguous writes remain ``manual_review`` and are never replayed.
    """

    def __init__(
        self,
        gateway: LingxingGateway,
        *,
        verification_attempts: int = 4,
        verification_delay_seconds: float = 0.25,
        verification_delays_seconds: Sequence[float] | None = None,
        warehouse_projection_delays_seconds: Sequence[float] | None = None,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> None:
        if verification_attempts <= 0:
            raise ValueError("verification_attempts must be positive")
        if verification_delay_seconds < 0:
            raise ValueError("verification_delay_seconds cannot be negative")
        self.gateway = gateway
        self.verification_attempts = verification_attempts
        self.verification_delay_seconds = verification_delay_seconds
        self.verification_delays_seconds = normalize_readback_delays(
            verification_delays_seconds
            if verification_delays_seconds is not None
            else (
                0.0,
                *(verification_delay_seconds for _ in range(verification_attempts - 1)),
            )
        )
        self.warehouse_projection_delays_seconds = normalize_readback_delays(
            warehouse_projection_delays_seconds
            if warehouse_projection_delays_seconds is not None
            else verification_delays_seconds
            if verification_delays_seconds is not None
            else DEFAULT_WAREHOUSE_PROJECTION_DELAYS_SECONDS
        )
        self.sleeper = sleeper

    async def download_custom_zip_bundle(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
        staging_root: str | Path,
        expected_zip_count: int | None,
        expected_order_item_ids: set[str] | None,
    ) -> OrderCustomZipBundle:
        """Download every expected customization ZIP through documented APIs.

        The method deliberately has no browser callback.  It validates the
        order-detail identity and every ZIP in memory before any production
        file is committed to staging.  Each final file is then written through
        a same-directory temporary file and ``os.replace``.
        """

        platform_text = str(platform_order_no or "").strip()
        created_paths: list[Path] = []
        try:
            platform_text = _safe_order_directory(platform_order_no, label="平台单号")
            system_text = _text(system_order_no)
            if system_text is None:
                raise CustomOrderApiPlanError("系统单号不能为空。")
            if expected_zip_count is not None:
                if isinstance(expected_zip_count, bool) or expected_zip_count < 0:
                    raise CustomOrderApiPlanError("期望 ZIP 数量必须是非负整数。")
                expected_zip_count = int(expected_zip_count)
            normalized_expected_ids: set[str] | None = None
            if expected_order_item_ids is not None:
                normalized_expected_ids = {
                    str(value).strip() for value in expected_order_item_ids if str(value).strip()
                }
                if len(normalized_expected_ids) != len(expected_order_item_ids):
                    raise CustomOrderApiPlanError("期望 order_item_id 包含空值或重复值。")
                if (
                    expected_zip_count is not None
                    and expected_zip_count != len(normalized_expected_ids)
                ):
                    raise CustomOrderApiPlanError(
                        "期望 ZIP 数量与期望 order_item_id 数量不一致。"
                    )

            detail = await self.gateway.get_order_detail(system_text)
            candidates, warnings = _custom_zip_candidates_from_detail(
                detail.payload,
                platform_order_no=platform_text,
                system_order_no=system_text,
                expected_zip_count=expected_zip_count,
                expected_order_item_ids=normalized_expected_ids,
            )

            downloads: list[tuple[_CustomZipCandidate, AttachmentData]] = []
            for candidate in candidates:
                # ``newAttachments.file_id`` belongs to the FBM order-item
                # attachment service.  The similarly named customization-file
                # endpoint is reserved for custom reports/files and rejects
                # these order attachment identifiers.
                attachment = await self.gateway.download_order_attachment(candidate.file_id)
                response_filename = _safe_zip_filename(
                    attachment.filename,
                    label=f"file_id {candidate.file_id} 下载响应",
                )
                if response_filename.casefold() != candidate.file_name.casefold():
                    raise CustomOrderApiPlanError(
                        f"file_id {candidate.file_id} 的详情文件名与下载响应不一致。"
                    )
                _validate_custom_zip_content(
                    attachment.content,
                    platform_order_no=platform_text,
                    order_item_id=candidate.order_item_id,
                )
                downloads.append((candidate, attachment))

            staging_root_path = Path(staging_root).expanduser().resolve()
            staging_dir = (staging_root_path / platform_text).resolve()
            if staging_root_path not in staging_dir.parents:
                raise CustomOrderApiPlanError("定制 ZIP staging 路径越界。")
            staging_dir.mkdir(parents=True, exist_ok=True)
            zip_files: list[CustomZipFile] = []
            for candidate, attachment in downloads:
                target = _atomic_write_custom_zip(
                    staging_dir,
                    candidate.file_name,
                    attachment.content,
                )
                created_paths.append(target)
                asin_match = re.match(r"^(B0[A-Z0-9]{8})(?:_|$)", target.name, flags=re.IGNORECASE)
                zip_files.append(
                    CustomZipFile(
                        row_index=candidate.row_index,
                        asin=asin_match.group(1).upper() if asin_match else None,
                        sku=candidate.sku,
                        msku=candidate.msku,
                        platform_order_no=platform_text,
                        trigger_text=f"lingxing_api:file_id={candidate.file_id}",
                        zip_filename=target.name,
                        zip_path=str(target),
                        zip_candidates=[candidate.file_name],
                        order_item_id=candidate.order_item_id,
                        status=CUSTOM_ZIP_DOWNLOADED,
                    )
                )
            return OrderCustomZipBundle(
                platform_order_no=platform_text,
                zip_files=zip_files,
                status="ok",
                warnings=warnings,
            )
        except asyncio.CancelledError:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        except CustomOrderApiPlanError as exc:
            for path in created_paths:
                path.unlink(missing_ok=True)
            message = str(exc)[:800]
            status = (
                CUSTOM_ZIP_NOT_FOUND
                if "没有匹配" in message or "必须且只能匹配" in message or "数量不匹配" in message
                else CUSTOM_ZIP_DOWNLOAD_ERROR
            )
            return OrderCustomZipBundle(
                platform_order_no=platform_text,
                status=status,
                error=message,
            )
        except Exception as exc:
            for path in created_paths:
                path.unlink(missing_ok=True)
            message = str(exc).splitlines()[0][:800] if str(exc) else exc.__class__.__name__
            return OrderCustomZipBundle(
                platform_order_no=platform_text,
                status=CUSTOM_ZIP_DOWNLOAD_ERROR,
                error=message,
            )

    async def get_shipping_deadline_text(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
    ) -> str | None:
        snapshot = await self._one_snapshot(platform_order_no, system_order_no)
        return snapshot.shipping_deadline

    async def get_order_processing_status(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
    ) -> OrderProcessingStatus:
        """Read the exact order again immediately before any workflow write."""

        snapshot = await self._one_snapshot(platform_order_no, system_order_no)
        buyer_cancel_requested = has_buyer_cancel_request(snapshot.payload)
        raw_status = _first(
            snapshot.payload,
            "order_status_name",
            "orderStatusName",
            "status_name",
            "statusName",
            "status",
        )
        status_text = BUYER_CANCEL_REQUEST_TEXT if buyer_cancel_requested else (_text(raw_status) or "")
        return OrderProcessingStatus(
            platform_order_no=str(platform_order_no).strip(),
            system_order_no=snapshot.global_order_no,
            buyer_cancel_requested=buyer_cancel_requested,
            status_text=status_text,
        )

    async def update_phone(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
        phone: str,
    ) -> ApiWriteOutcome:
        # An empty item list is explicitly supported by updateOrder and keeps
        # the request scoped to address_info.receiver_tel.
        try:
            desired_identity = _phone_identity(phone)
            if not desired_identity:
                raise ValueError("收件电话缺少可验证的数字。")
            before = await self._one_snapshot(platform_order_no, system_order_no)
            current_phone = _snapshot_receiver_phone(before)
            if current_phone and _phone_identity(current_phone) == desired_identity:
                return ApiWriteOutcome(
                    status="succeeded",
                    message="领星订单中的收件电话已经是目标值，本次未重复写入。",
                    details={
                        "no_op": True,
                        "verification": "already_applied",
                        "global_order_no": system_order_no,
                    },
                )

            async def verify(_initial: MutationResult) -> MutationVerification:
                last: _ApiOrderSnapshot | None = None
                last_attempt = None
                transient_error_count = 0
                last_error: Exception | None = None
                async for attempt in iter_readback_attempts(
                    self.verification_delays_seconds,
                    sleeper=self.sleeper,
                ):
                    last_attempt = attempt
                    try:
                        last = await self._one_snapshot(platform_order_no, system_order_no)
                    except Exception as exc:
                        transient_error_count += 1
                        last_error = exc
                        continue
                    observed = _snapshot_receiver_phone(last)
                    if observed and _phone_identity(observed) == desired_identity:
                        return MutationVerification(
                            VerificationOutcome.CONFIRMED_APPLIED,
                            message="已通过订单列表读回确认收件电话生效。",
                            before={"receiver_tel": current_phone},
                            after={"receiver_tel": observed},
                            details=_readback_details(
                                attempt,
                                transient_error_count=transient_error_count,
                                last_error=last_error,
                            ),
                        )
                return MutationVerification(
                    VerificationOutcome.INCONCLUSIVE,
                    message="电话写入后尚无法读回确认，必须人工复核，禁止网页重试。",
                    before={"receiver_tel": current_phone},
                    after={"receiver_tel": _snapshot_receiver_phone(last)} if last else None,
                    details=_readback_details(
                        last_attempt,
                        transient_error_count=transient_error_count,
                        last_error=last_error,
                    ),
                )

            result = await self.gateway.update_phone(
                system_order_no,
                phone,
                order_item_list=[],
                verify=verify,
            )
            return _mutation_outcome(result)
        except ManualReviewRequired as exc:
            return self._manual_review_outcome(exc)
        except (CapabilityUnavailable, CustomOrderApiPlanError, ValueError) as exc:
            return ApiWriteOutcome(status="failed", message=str(exc))

    async def update_tent_skus(
        self,
        *,
        plan: TentSkuAdjustmentPlan,
        order_lines: list[OrderFolderLine],
    ) -> TentSkuAdjustmentResult:
        actions: list[str] = []
        try:
            before = await self._one_snapshot(plan.platform_order_no, plan.system_order_no)
            wire_items, replacements, expected_totals = self._build_sku_update_payload(
                plan,
                order_lines,
                before,
            )
            actions.extend(
                f"api_replace_whole_row:{source.item_id or source.order_item_no}:"
                f"{source.local_sku or source.msku or '-'}->{target_sku}:x{source.quantity}"
                for source, target_sku in replacements
            )
            actions.extend(
                f"api_add:{item['sku']}:x{item['quantity']}"
                for item in wire_items
                if item.get("type") == 1
            )

            async def verify(_initial: MutationResult) -> MutationVerification:
                last: _ApiOrderSnapshot | None = None
                last_attempt = None
                transient_error_count = 0
                last_error: Exception | None = None
                async for attempt in iter_readback_attempts(
                    self.verification_delays_seconds,
                    sleeper=self.sleeper,
                ):
                    last_attempt = attempt
                    try:
                        last = await self._one_snapshot(
                            plan.platform_order_no,
                            plan.system_order_no,
                        )
                    except Exception as exc:
                        transient_error_count += 1
                        last_error = exc
                        continue
                    if self._sku_update_applied(last, replacements, expected_totals):
                        return MutationVerification(
                            VerificationOutcome.CONFIRMED_APPLIED,
                            message="已通过订单列表读回确认整行换货和新增配件生效。",
                            before=_snapshot_summary(before),
                            after=_snapshot_summary(last),
                            details=_readback_details(
                                attempt,
                                transient_error_count=transient_error_count,
                                last_error=last_error,
                            ),
                        )
                return MutationVerification(
                    VerificationOutcome.INCONCLUSIVE,
                    message="API 写入后的商品行尚无法读回确认，必须人工复核，禁止网页重试。",
                    before=_snapshot_summary(before),
                    after=_snapshot_summary(last) if last else None,
                    details=_readback_details(
                        last_attempt,
                        transient_error_count=transient_error_count,
                        last_error=last_error,
                    ),
                )

            result = await self.gateway.update_order_items(
                plan.system_order_no,
                wire_items,
                verify=verify,
            )
            if result.state is MutationState.SUCCEEDED:
                actions.append(f"api_verified:{result.request_id or '-'}")
                return TentSkuAdjustmentResult(status="sku_adjustment_complete", actions=actions)
            outcome = _mutation_outcome(result)
            return TentSkuAdjustmentResult(
                status="sku_adjustment_api_failed",
                actions=actions,
                error=outcome.message or outcome.status,
                fallback_eligible=(
                    bool(result.definitely_not_executed)
                    and not bool(result.details.get("browser_fallback_forbidden"))
                ),
                request_id=result.request_id,
            )
        except ManualReviewRequired as exc:
            outcome = self._manual_review_outcome(exc)
            actions.append(f"api_manual_review:{outcome.request_id or '-'}")
            return TentSkuAdjustmentResult(
                status="sku_adjustment_manual_review",
                actions=actions,
                error=outcome.message,
            )
        except (CapabilityUnavailable, CustomOrderApiPlanError, ValueError) as exc:
            return TentSkuAdjustmentResult(
                status="sku_adjustment_api_failed",
                actions=actions,
                error=str(exc),
            )

    async def split_tent_packages(
        self,
        *,
        plan: TentPackageSplitPlan,
    ) -> TentPackageSplitResult:
        actions: list[str] = []
        if not plan.required:
            return TentPackageSplitResult(status="package_split_not_required")
        try:
            before = await self._one_snapshot(plan.platform_order_no, plan.system_order_no)
            wire_groups = self._build_split_groups(plan, before)
            actions.extend(
                f"api_split_group:{index}:lines={len(group)}"
                for index, group in enumerate(wire_groups, start=1)
            )

            result = await self.gateway.split_order(
                plan.system_order_no,
                wire_groups,
                split_mod=1,
            )
            if result.state is not MutationState.SUCCEEDED:
                outcome = _mutation_outcome(result)
                return TentPackageSplitResult(
                    status="package_split_api_failed",
                    actions=actions,
                    error=outcome.message or outcome.status,
                    fallback_eligible=(
                        bool(result.definitely_not_executed)
                        and not bool(result.details.get("browser_fallback_forbidden"))
                    ),
                    request_id=result.request_id,
                )
            system_order_nos, response_validation = self._validate_complete_split_response(
                result,
                expected_group_count=len(wire_groups),
                original_global_order_no=before.global_order_no,
            )
            if response_validation["status"] != "complete":
                actions.append(f"api_ack_manual_review:{result.request_id or '-'}")
                return TentPackageSplitResult(
                    status="package_split_manual_review",
                    actions=actions,
                    error=(
                        "拆包 API 返回业务成功，但 data.result 未完整给出本次拆包的"
                        "全部系统单号；当前阶段已转人工复核，禁止自动重发或网页回退。"
                    ),
                    request_id=result.request_id,
                    response_validation=response_validation,
                )
            instruction_group_index = self._instruction_split_group_index(
                plan,
                expected_group_count=len(wire_groups),
            )
            instruction_system_order_no = (
                system_order_nos[instruction_group_index]
                if instruction_group_index is not None
                else None
            )
            response_validation["instruction_group_index"] = instruction_group_index
            response_validation["instruction_target_mapped"] = bool(
                instruction_system_order_no
            )
            projected_routing_packages = self._project_split_routing_packages(
                before,
                wire_groups,
                system_order_nos,
            )
            response_validation["projected_package_count"] = len(
                projected_routing_packages
            )
            actions.append(f"api_ack_complete:{result.request_id or '-'}")
            return TentPackageSplitResult(
                status="package_split_complete",
                actions=actions,
                system_order_nos=system_order_nos,
                request_id=result.request_id,
                instruction_system_order_no=instruction_system_order_no,
                response_validation=response_validation,
                projected_routing_packages=projected_routing_packages,
            )
        except ManualReviewRequired as exc:
            outcome = self._manual_review_outcome(exc)
            actions.append(f"api_manual_review:{outcome.request_id or '-'}")
            return TentPackageSplitResult(
                status="package_split_manual_review",
                actions=actions,
                error=outcome.message,
                request_id=outcome.request_id,
                response_validation={
                    "status": "transport_or_business_result_ambiguous",
                    "post_write_readback": "skipped",
                },
            )
        except (CapabilityUnavailable, CustomOrderApiPlanError, ValueError) as exc:
            return TentPackageSplitResult(
                status="package_split_api_failed",
                actions=actions,
                error=str(exc),
            )

    async def set_instruction_remark(
        self,
        *,
        platform_order_no: str,
        candidate_system_order_nos: list[str],
        remark: str,
        target_system_order_no: str | None = None,
    ) -> InstructionRemarkOutcome:
        try:
            candidates = {value.strip() for value in candidate_system_order_nos if value.strip()}
            explicit_target = str(target_system_order_no or "").strip()
            if explicit_target:
                if candidates and explicit_target not in candidates:
                    raise CustomOrderApiPlanError(
                        "拆包响应映射的说明书系统单号不在完整拆包结果中，未写客服备注。"
                    )
            target = await self._instruction_target_snapshot_with_retry(
                platform_order_no=platform_order_no,
                candidate_system_order_nos=candidates,
                target_system_order_no=explicit_target or None,
            )
            next_text, action = _merge_instruction_customer_remark(target.remark, remark)
            if action == "skip":
                return InstructionRemarkOutcome(
                    status="succeeded",
                    message="说明书客服备注已存在，无需重复写入。",
                    action=action,
                    target_system_order_no=target.global_order_no,
                )

            async def verify(_initial: MutationResult) -> MutationVerification:
                last: _ApiOrderSnapshot | None = None
                last_attempt = None
                transient_error_count = 0
                last_error: Exception | None = None
                async for attempt in iter_readback_attempts(
                    self.verification_delays_seconds,
                    sleeper=self.sleeper,
                ):
                    last_attempt = attempt
                    try:
                        last = await self._one_snapshot(
                            platform_order_no,
                            target.global_order_no,
                        )
                    except Exception as exc:
                        transient_error_count += 1
                        last_error = exc
                        continue
                    if last.remark.strip() == next_text.strip():
                        return MutationVerification(
                            VerificationOutcome.CONFIRMED_APPLIED,
                            message="已通过订单列表读回确认客服备注生效。",
                            before={"global_order_no": target.global_order_no, "remark": target.remark},
                            after={"global_order_no": last.global_order_no, "remark": last.remark},
                            details=_readback_details(
                                attempt,
                                transient_error_count=transient_error_count,
                                last_error=last_error,
                            ),
                        )
                return MutationVerification(
                    VerificationOutcome.INCONCLUSIVE,
                    message="客服备注尚无法读回确认，必须人工复核，禁止网页重试。",
                    before={"global_order_no": target.global_order_no, "remark": target.remark},
                    after=(
                        {"global_order_no": last.global_order_no, "remark": last.remark}
                        if last
                        else None
                    ),
                    details=_readback_details(
                        last_attempt,
                        transient_error_count=transient_error_count,
                        last_error=last_error,
                    ),
                )

            result = await self.gateway.set_order_remark(
                target.global_order_no,
                next_text,
                append=False,
                verify=verify,
            )
            outcome = _mutation_outcome(result)
            return InstructionRemarkOutcome(
                status=outcome.status,
                message=outcome.message,
                request_id=outcome.request_id,
                details=outcome.details,
                action=action if outcome.succeeded else None,
                target_system_order_no=target.global_order_no,
            )
        except ManualReviewRequired as exc:
            outcome = self._manual_review_outcome(exc)
            return InstructionRemarkOutcome(
                status="manual_review",
                message=outcome.message,
                request_id=outcome.request_id,
                details=outcome.details,
            )
        except (CapabilityUnavailable, CustomOrderApiPlanError, ValueError) as exc:
            return InstructionRemarkOutcome(status="failed", message=str(exc))

    async def set_tent_warehouse_logistics(
        self,
        *,
        plan: TentSkuAdjustmentPlan,
        candidate_system_order_nos: list[str],
        apply: bool,
        projected_packages: tuple[TentRoutingPackage, ...] | None = None,
    ) -> WarehouseLogisticsOutcome:
        """规划并设置拆单后的帐篷仓库物流，不使用网页或购买配送。"""

        try:
            use_projected_preview = bool(projected_packages and not apply)
            snapshots: list[_ApiOrderSnapshot] = []
            if use_projected_preview:
                packages = tuple(projected_packages or ())
                projection_details = {
                    "projection_attempts": 0,
                    "projection_waited_seconds": 0.0,
                }
            else:
                snapshots, projection_details = (
                    await self._warehouse_routing_snapshots_with_retry(
                        platform_order_no=plan.platform_order_no,
                        candidate_system_order_nos={
                            str(value).strip()
                            for value in candidate_system_order_nos
                            if str(value).strip()
                        },
                    )
                )
                packages = tuple(
                    TentRoutingPackage(
                        system_order_no=snapshot.global_order_no,
                        items=tuple(
                            TentRoutingItem(
                                sku=item.local_sku or item.msku or "",
                                quantity=item.quantity,
                                item_id=item.item_id,
                                order_item_no=item.order_item_no,
                            )
                            for item in snapshot.items
                        ),
                    )
                    for snapshot in snapshots
                )
            routing_plan = build_tent_warehouse_routing_plan(
                sku_plan=plan,
                packages=packages,
            )
            details: dict[str, Any] = {
                "plan": routing_plan.to_log_dict(),
                "writes": [],
                "projection_source": (
                    "split_ack" if use_projected_preview else "order_list"
                ),
                **projection_details,
            }
            if routing_plan.manual_required:
                return WarehouseLogisticsOutcome(
                    status="manual_review",
                    message=routing_plan.reason,
                    details=details,
                    plan=routing_plan,
                )
            ready_decisions = [
                decision for decision in routing_plan.decisions if decision.status == "ready"
            ]
            if not ready_decisions:
                return WarehouseLogisticsOutcome(
                    status="succeeded",
                    message=routing_plan.reason,
                    details=details,
                    plan=routing_plan,
                )
            if not apply:
                return WarehouseLogisticsOutcome(
                    status="preview",
                    message="预览模式仅输出帐篷仓库物流计划，未执行写入。",
                    details=details,
                    plan=routing_plan,
                )

            targets = await self._resolve_warehouse_logistics_targets(ready_decisions)
            snapshots_by_no = {row.global_order_no: row for row in snapshots}
            pending: list[tuple[TentWarehouseRoutingDecision, str, str]] = []
            for decision in ready_decisions:
                target_warehouse_id, target_logistics_id = targets[decision.system_order_no]
                snapshot = snapshots_by_no.get(decision.system_order_no)
                if snapshot is None:
                    raise CustomOrderApiPlanError(
                        f"仓库物流计划中的系统单号 {decision.system_order_no} 已不在读回结果中。"
                    )
                current_warehouse_id, current_logistics_id = _snapshot_shipping_route(snapshot)
                audit = {
                    "system_order_no": decision.system_order_no,
                    "current_warehouse_id": current_warehouse_id,
                    "current_logistics_type_id": current_logistics_id,
                    "target_warehouse_id": target_warehouse_id,
                    "target_logistics_type_id": target_logistics_id,
                    "status": "pending",
                }
                details["writes"].append(audit)
                if (
                    current_warehouse_id == target_warehouse_id
                    and current_logistics_id == target_logistics_id
                ):
                    audit["status"] = "already_applied"
                    continue
                # ERP may apply its own default warehouse/logistics rule as
                # soon as a split package appears.  Once this workflow has an
                # exact tent-routing target, that pre-existing route is not
                # treated as an operator conflict: write the planned pair and
                # rely on the read-after-write verification below.
                audit["overwriting_existing_route"] = bool(
                    current_warehouse_id is not None or current_logistics_id is not None
                )
                pending.append((decision, target_warehouse_id, target_logistics_id))

            written: list[str] = []
            for decision, warehouse_id, logistics_id in pending:
                result = await self.gateway.set_shipping_channel(
                    [
                        {
                            "global_order_no": decision.system_order_no,
                            "logistics": {
                                "logistics_type_id": _wire_identifier(logistics_id),
                                "sys_wid": _wire_identifier(warehouse_id),
                            },
                        }
                    ]
                )
                audit = next(
                    row
                    for row in details["writes"]
                    if row["system_order_no"] == decision.system_order_no
                )
                audit["request_id"] = result.request_id
                if result.state is not MutationState.SUCCEEDED:
                    outcome = _mutation_outcome(result)
                    audit["status"] = outcome.status
                    if written or outcome.manual_review_required:
                        return WarehouseLogisticsOutcome(
                            status="manual_review",
                            message=(
                                outcome.message
                                or "仓库物流写入结果不明确或已发生部分写入，必须人工复核；禁止自动重发。"
                            ),
                            request_id=outcome.request_id,
                            details=details,
                            plan=routing_plan,
                        )
                    return WarehouseLogisticsOutcome(
                        status="failed",
                        message=outcome.message or "仓库物流 API 明确写入失败。",
                        request_id=outcome.request_id,
                        details=details,
                        plan=routing_plan,
                    )
                verified, readback_details = await self._verify_warehouse_route_with_retry(
                    platform_order_no=plan.platform_order_no,
                    system_order_no=decision.system_order_no,
                    warehouse_id=warehouse_id,
                    logistics_type_id=logistics_id,
                )
                audit.update(readback_details)
                if not verified:
                    audit["status"] = "manual_review"
                    return WarehouseLogisticsOutcome(
                        status="manual_review",
                        message=(
                            f"系统单号 {decision.system_order_no} 的仓库物流写入尚无法读回确认，"
                            "必须人工复核，禁止自动重发或网页兜底。"
                        ),
                        request_id=result.request_id,
                        details=details,
                        plan=routing_plan,
                    )
                audit["status"] = "verified"
                written.append(decision.system_order_no)
            return WarehouseLogisticsOutcome(
                status="succeeded",
                message="帐篷拆单后的仓库和物流渠道已逐单读回确认。",
                details=details,
                plan=routing_plan,
            )
        except ManualReviewRequired as exc:
            outcome = self._manual_review_outcome(exc)
            return WarehouseLogisticsOutcome(
                status="manual_review",
                message=outcome.message,
                request_id=outcome.request_id,
                details=outcome.details,
            )
        except (CapabilityUnavailable, CustomOrderApiPlanError, ValueError) as exc:
            return WarehouseLogisticsOutcome(status="manual_review", message=str(exc))

    async def _warehouse_routing_snapshots_with_retry(
        self,
        *,
        platform_order_no: str,
        candidate_system_order_nos: set[str],
    ) -> tuple[list[_ApiOrderSnapshot], dict[str, Any]]:
        last_error: Exception | None = None
        async for attempt in iter_readback_attempts(
            self.warehouse_projection_delays_seconds,
            sleeper=self.sleeper,
        ):
            try:
                snapshots = await self._snapshots(platform_order_no)
                if candidate_system_order_nos:
                    indexed = {row.global_order_no: row for row in snapshots}
                    if set(indexed).issuperset(candidate_system_order_nos):
                        return (
                            [
                                indexed[value]
                                for value in sorted(candidate_system_order_nos)
                            ],
                            {
                                "projection_attempts": attempt.number,
                                "projection_waited_seconds": round(
                                    attempt.waited_seconds,
                                    3,
                                ),
                            },
                        )
                    missing = sorted(candidate_system_order_nos - set(indexed))
                    raise CustomOrderApiPlanError(
                        "拆单后的系统单尚未全部出现在订单列表：" + ", ".join(missing)
                    )
                if snapshots:
                    return (
                        sorted(snapshots, key=lambda row: row.global_order_no),
                        {
                            "projection_attempts": attempt.number,
                            "projection_waited_seconds": round(
                                attempt.waited_seconds,
                                3,
                            ),
                        },
                    )
            except Exception as exc:
                last_error = exc
        raise CustomOrderApiPlanError(
            str(last_error or "无法读取拆单后的帐篷系统订单。")
        )

    async def _resolve_warehouse_logistics_targets(
        self,
        decisions: Sequence[TentWarehouseRoutingDecision],
    ) -> dict[str, tuple[str, str]]:
        warehouses = []
        offset = 0
        for _ in range(10):
            page = await self.gateway.list_warehouses(
                # Lingxing's warehouse master data uses type=3 for third-party
                # overseas warehouses.  type=1 only returns the local/default
                # warehouse and therefore cannot resolve the tent routes.
                warehouse_type=3,
                is_delete=0,
                offset=offset,
                length=1000,
            )
            warehouses.extend(page.items)
            if page.next_offset is None:
                break
            offset = page.next_offset
        else:
            raise CustomOrderApiPlanError("仓库列表超过安全分页上限。")

        logistics_types = []
        page_number = 1
        for _ in range(10):
            page = await self.gateway.list_logistics_types(
                # Third-party overseas warehouse channels are returned under
                # provider_type=2; type=1 is the platform-provider list.
                provider_type=2,
                page=page_number,
                length=1000,
            )
            logistics_types.extend(page.items)
            if page.next_offset is None:
                break
            page_number += 1
        else:
            raise CustomOrderApiPlanError("物流渠道列表超过安全分页上限。")

        resolved: dict[str, tuple[str, str]] = {}
        for decision in decisions:
            code = str(decision.target_warehouse_code or "").strip()
            warehouse_name = str(decision.target_warehouse_name or "").strip()
            channel_name = str(decision.target_channel_name or "").strip()
            if not code or not warehouse_name or not channel_name:
                raise CustomOrderApiPlanError("仓库物流计划缺少完整目标名称。")
            if not channel_name.startswith(f"{warehouse_name}-"):
                raise CustomOrderApiPlanError(
                    f"系统单号 {decision.system_order_no} 的渠道不属于目标仓库，禁止交叉写入。"
                )
            warehouse_matches = [
                row
                for row in warehouses
                if row.name == warehouse_name
                and _lookup_payload_value(
                    row.payload,
                    "t_warehouse_code",
                    "warehouse_code",
                    "warehouseCode",
                    "warehouse_no",
                    "warehouseNo",
                    "warehouse_sn",
                    "warehouseSn",
                    "code",
                )
                == code
            ]
            if len(warehouse_matches) != 1 or not _positive_identifier(warehouse_matches[0].identifier):
                raise CustomOrderApiPlanError(
                    f"无法按仓名和仓库代码唯一匹配 {warehouse_name} / {code}。"
                )
            warehouse_id = _positive_identifier(warehouse_matches[0].identifier) or ""
            channel_suffix = channel_name.removeprefix(f"{warehouse_name}-")
            channel_matches = [
                row
                for row in logistics_types
                if row.name == channel_suffix
                and _lookup_payload_value(
                    row.payload,
                    "wid",
                    "sys_wid",
                    "warehouse_id",
                    "warehouseId",
                )
                == warehouse_id
            ]
            if len(channel_matches) != 1 or not _positive_identifier(channel_matches[0].identifier):
                raise CustomOrderApiPlanError(
                    f"无法在 provider_type=2 的仓库 {warehouse_name} 中唯一匹配物流渠道 {channel_suffix}。"
                )
            resolved[decision.system_order_no] = (
                warehouse_id,
                _positive_identifier(channel_matches[0].identifier) or "",
            )
        return resolved

    async def _verify_warehouse_route_with_retry(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
        warehouse_id: str,
        logistics_type_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        last_attempt = None
        last_error: Exception | None = None
        transient_error_count = 0
        last_route: tuple[str | None, str | None] = (None, None)
        async for attempt in iter_readback_attempts(
            self.verification_delays_seconds,
            sleeper=self.sleeper,
        ):
            last_attempt = attempt
            try:
                snapshot = await self._one_snapshot(platform_order_no, system_order_no)
                last_route = _snapshot_shipping_route(snapshot)
            except Exception as exc:
                transient_error_count += 1
                last_error = exc
                continue
            if last_route == (warehouse_id, logistics_type_id):
                return True, {
                    "readback_warehouse_id": last_route[0],
                    "readback_logistics_type_id": last_route[1],
                    **_readback_details(
                        attempt,
                        transient_error_count=transient_error_count,
                        last_error=last_error,
                    ),
                }
        return False, {
            "readback_warehouse_id": last_route[0],
            "readback_logistics_type_id": last_route[1],
            **_readback_details(
                last_attempt,
                transient_error_count=transient_error_count,
                last_error=last_error,
            ),
        }

    async def _snapshots(self, platform_order_no: str) -> list[_ApiOrderSnapshot]:
        platform_order_no = str(platform_order_no).strip()
        if not platform_order_no:
            raise CustomOrderApiPlanError("平台单号不能为空。")
        offset = 0
        length = 200
        records: list[OrderRecord] = []
        seen: set[tuple[str | None, str | None]] = set()
        for _ in range(10):
            page = await self.gateway.list_orders(
                offset=offset,
                length=length,
                filters={"platform_order_nos": [platform_order_no]},
            )
            for record in page.items:
                identity = (record.global_order_no, record.order_number)
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(record)
            if page.next_offset is None:
                break
            offset = page.next_offset
        else:
            raise CustomOrderApiPlanError("按平台单号查询订单超过安全分页上限。")

        snapshots = [_snapshot(record) for record in records]
        matched = [
            row
            for row in snapshots
            if platform_order_no in row.platform_order_nos
            or (not row.platform_order_nos and len(snapshots) == 1)
        ]
        if not matched:
            raise CustomOrderApiPlanError(f"领星 API 未找到平台单号 {platform_order_no}。")
        return matched

    async def _one_snapshot(
        self,
        platform_order_no: str,
        system_order_no: str,
    ) -> _ApiOrderSnapshot:
        system_order_no = str(system_order_no).strip()
        matches = [
            row
            for row in await self._snapshots(platform_order_no)
            if row.global_order_no == system_order_no
        ]
        if len(matches) != 1:
            raise CustomOrderApiPlanError(
                f"平台单号 {platform_order_no} 下无法唯一定位系统单号 {system_order_no}。"
            )
        return matches[0]

    async def _instruction_target_snapshot_with_retry(
        self,
        *,
        platform_order_no: str,
        candidate_system_order_nos: set[str],
        target_system_order_no: str | None,
    ) -> _ApiOrderSnapshot:
        """Wait for a newly split Instruction order to become queryable.

        Lingxing can acknowledge a split before its order-list projection
        exposes the child order and item rows.  Locating the exact target is a
        read-only operation, so it is safe to repeat with the same bounded
        schedule used by post-write verification.  No remark write happens
        until one exact target containing the Instruction SKU is visible.
        """

        last_error: Exception | None = None
        async for _attempt in iter_readback_attempts(
            self.verification_delays_seconds,
            sleeper=self.sleeper,
        ):
            try:
                if target_system_order_no:
                    target = await self._one_snapshot(
                        platform_order_no,
                        target_system_order_no,
                    )
                    if not any(
                        _sku_key(item.local_sku) == _sku_key(INSTRUCTION_SKU)
                        for item in target.items
                    ):
                        raise CustomOrderApiPlanError(
                            "拆包响应映射的目标系统订单尚未读到 Instruction 商品行，"
                            "拆包保持已完成，说明书备注留待后续处理。"
                        )
                    return target

                snapshots = await self._snapshots(platform_order_no)
                if candidate_system_order_nos:
                    snapshots = [
                        row
                        for row in snapshots
                        if row.global_order_no in candidate_system_order_nos
                    ]
                targets = [
                    row
                    for row in snapshots
                    if any(
                        _sku_key(item.local_sku) == _sku_key(INSTRUCTION_SKU)
                        for item in row.items
                    )
                ]
                if len(targets) != 1:
                    raise CustomOrderApiPlanError(
                        "无法从拆包后的订单中唯一定位包含 Instruction 的系统订单，"
                        "未写客服备注。"
                    )
                return targets[0]
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise last_error
        raise CustomOrderApiPlanError(
            "无法从拆包后的订单中唯一定位包含 Instruction 的系统订单，未写客服备注。"
        )

    @staticmethod
    def _build_sku_update_payload(
        plan: TentSkuAdjustmentPlan,
        order_lines: list[OrderFolderLine],
        snapshot: _ApiOrderSnapshot,
    ) -> tuple[
        list[dict[str, Any]],
        list[tuple[_ApiOrderItem, str]],
        Counter[str],
    ]:
        if plan.manual_required:
            raise CustomOrderApiPlanError(plan.manual_reason or "SKU 计划要求人工处理。")
        if not plan.replace_main_items:
            raise CustomOrderApiPlanError("SKU 计划缺少按原商品行绑定的整行换货动作。")

        source_lines = {
            str(line.order_item_id): line
            for line in order_lines
            if line.order_item_id
        }
        used_item_ids: set[str] = set()
        wire_items: list[dict[str, Any]] = []
        replacements: list[tuple[_ApiOrderItem, str]] = []
        simulated_skus: dict[int, str] = {
            index: _sku_key(item.local_sku)
            for index, item in enumerate(snapshot.items)
        }

        for action in plan.replace_main_items:
            target_sku = _text(action.sku)
            source_order_item_id = _text(action.source_order_item_id)
            if target_sku is None or source_order_item_id is None:
                raise CustomOrderApiPlanError("整行换货动作缺少目标 SKU 或原始商品行 ID。")
            source_line = source_lines.get(source_order_item_id)
            if source_line is None:
                raise CustomOrderApiPlanError(
                    f"整行换货动作无法在原订单行中定位 {source_order_item_id}。"
                )
            expected_quantity = _positive_quantity(
                action.source_original_quantity,
                label=f"原商品行 {source_order_item_id}",
            )
            if action.quantity != expected_quantity or source_line.quantity != expected_quantity:
                raise CustomOrderApiPlanError(
                    f"原商品行 {source_order_item_id} 必须整行换货："
                    f"计划数量 {action.quantity}，原始数量 {source_line.quantity}。"
                )

            matches = [
                (index, item)
                for index, item in enumerate(snapshot.items)
                if source_order_item_id in {item.order_item_no, item.item_id}
            ]
            if len(matches) != 1:
                raise CustomOrderApiPlanError(
                    f"领星 API 商品行无法唯一匹配原订单行 {source_order_item_id}。"
                )
            index, api_item = matches[0]
            stable_id = api_item.item_id or api_item.order_item_no
            if stable_id is None or stable_id in used_item_ids:
                raise CustomOrderApiPlanError("整行换货动作重复或缺少领星商品行 ID。")
            if api_item.quantity != expected_quantity:
                raise CustomOrderApiPlanError(
                    f"领星商品行 {source_order_item_id} 当前数量 {api_item.quantity} "
                    f"与原始数量 {expected_quantity} 不一致，未执行换货。"
                )
            if action.source_sku and api_item.msku and _sku_key(action.source_sku) != _sku_key(api_item.msku):
                raise CustomOrderApiPlanError(
                    f"领星商品行 {source_order_item_id} 的 MSKU 与原始商品行不一致。"
                )

            update: dict[str, Any] = {"sku": target_sku, "type": 3}
            if api_item.item_id:
                update["id"] = api_item.item_id
            if api_item.msku:
                update["msku"] = api_item.msku
            if "id" not in update and "msku" not in update:
                raise CustomOrderApiPlanError(
                    f"领星商品行 {source_order_item_id} 缺少覆盖所需的 id/msku。"
                )
            # Lingxing documents that quantities of online items cannot be
            # modified.  Omitting quantity preserves the original whole-row
            # quantity, which was checked above against all three sources.
            wire_items.append(update)
            replacements.append((api_item, target_sku))
            simulated_skus[index] = _sku_key(target_sku)
            used_item_ids.add(stable_id)

        added_totals: Counter[str] = Counter()
        for action in plan.add_items:
            sku = _text(action.sku)
            if sku is None:
                raise CustomOrderApiPlanError("新增配件动作缺少 SKU。")
            quantity = _positive_quantity(action.quantity, label=f"新增配件 {sku}")
            wire_items.append(
                {
                    "sku": sku,
                    "quantity": quantity,
                    "type": 1,
                    "platformOrderNo": plan.platform_order_no,
                }
            )
            added_totals[_sku_key(sku)] += quantity

        expected_totals: Counter[str] = Counter()
        for index, item in enumerate(snapshot.items):
            sku = simulated_skus[index]
            if sku:
                expected_totals[sku] += item.quantity
        expected_totals.update(added_totals)
        return wire_items, replacements, expected_totals

    @staticmethod
    def _sku_update_applied(
        snapshot: _ApiOrderSnapshot,
        replacements: list[tuple[_ApiOrderItem, str]],
        expected_totals: Counter[str],
    ) -> bool:
        by_id = {
            item.item_id or item.order_item_no: item
            for item in snapshot.items
            if item.item_id or item.order_item_no
        }
        for before, target_sku in replacements:
            stable_id = before.item_id or before.order_item_no
            current = by_id.get(stable_id)
            if current is None:
                # Some ERP implementations recreate a row on overwrite; the
                # aggregate check below remains authoritative in that case.
                continue
            if current.quantity != before.quantity or _sku_key(current.local_sku) != _sku_key(target_sku):
                return False
        actual_totals: Counter[str] = Counter()
        for item in snapshot.items:
            sku = _sku_key(item.local_sku)
            if sku:
                actual_totals[sku] += item.quantity
        return actual_totals == expected_totals

    @staticmethod
    def _build_split_groups(
        plan: TentPackageSplitPlan,
        snapshot: _ApiOrderSnapshot,
    ) -> list[list[dict[str, Any]]]:
        if not snapshot.items:
            raise CustomOrderApiPlanError("拆包前的领星订单没有商品行。")
        remaining = [item.quantity for item in snapshot.items]
        target_groups: list[list[dict[str, Any]]] = []

        for package in plan.packages_to_split:
            group: list[dict[str, Any]] = []
            for wanted in package.items:
                sku = _text(wanted.sku)
                if sku is None:
                    raise CustomOrderApiPlanError(f"拆包组 {package.package_key} 含空 SKU。")
                needed = _positive_quantity(wanted.quantity, label=f"拆包商品 {sku}")
                for index, item in enumerate(snapshot.items):
                    if needed <= 0:
                        break
                    if _sku_key(item.local_sku) != _sku_key(sku) or remaining[index] <= 0:
                        continue
                    if item.item_id is None:
                        raise CustomOrderApiPlanError(f"拆包商品 {sku} 缺少领星商品行主键。")
                    consumed = min(needed, remaining[index])
                    group.append({"item_id": item.item_id, "quantity": consumed})
                    remaining[index] -= consumed
                    needed -= consumed
                if needed:
                    raise CustomOrderApiPlanError(
                        f"拆包计划需要 {sku} x{wanted.quantity}，领星订单中的可用数量不足。"
                    )
            if not group:
                raise CustomOrderApiPlanError(f"拆包组 {package.package_key} 为空。")
            target_groups.append(group)

        leftover: list[dict[str, Any]] = []
        for index, item in enumerate(snapshot.items):
            if remaining[index] <= 0:
                continue
            if item.item_id is None:
                raise CustomOrderApiPlanError("原包裹剩余商品缺少领星商品行主键。")
            leftover.append({"item_id": item.item_id, "quantity": remaining[index]})

        wire_groups = ([leftover] if leftover else []) + target_groups
        if len(wire_groups) < 2 or any(not group for group in wire_groups):
            raise CustomOrderApiPlanError("拆包至少需要两个非空包裹组。")
        if sum(sum(int(item["quantity"]) for item in group) for group in wire_groups) != sum(
            item.quantity for item in snapshot.items
        ):
            raise CustomOrderApiPlanError("拆包计划没有完整守恒原订单商品数量。")
        return wire_groups

    @staticmethod
    def _project_split_routing_packages(
        snapshot: _ApiOrderSnapshot,
        wire_groups: Sequence[Sequence[Mapping[str, Any]]],
        system_order_nos: Sequence[str],
    ) -> tuple[TentRoutingPackage, ...]:
        """Map the strong split acknowledgement back to the submitted SKU groups."""

        if len(wire_groups) != len(system_order_nos):
            raise CustomOrderApiPlanError(
                "拆包响应系统单数量与已提交包裹组数量不一致。"
            )
        items_by_id = {
            str(item.item_id): item
            for item in snapshot.items
            if item.item_id is not None
        }
        packages: list[TentRoutingPackage] = []
        for system_order_no, group in zip(system_order_nos, wire_groups, strict=True):
            projected_items: list[TentRoutingItem] = []
            for raw in group:
                item_id = str(raw.get("item_id") or "").strip()
                source = items_by_id.get(item_id)
                if source is None:
                    raise CustomOrderApiPlanError(
                        f"拆包投影无法定位领星商品行 {item_id or '-'}。"
                    )
                quantity = _positive_quantity(
                    raw.get("quantity"),
                    label=f"拆包投影商品 {item_id}",
                )
                sku = _text(source.local_sku) or _text(source.msku)
                if sku is None:
                    raise CustomOrderApiPlanError(
                        f"拆包投影商品行 {item_id} 缺少 SKU。"
                    )
                projected_items.append(
                    TentRoutingItem(
                        sku=sku,
                        quantity=quantity,
                        item_id=source.item_id,
                        order_item_no=source.order_item_no,
                    )
                )
            packages.append(
                TentRoutingPackage(
                    system_order_no=str(system_order_no),
                    items=tuple(projected_items),
                )
            )
        return tuple(packages)

    @staticmethod
    def _validate_complete_split_response(
        result: MutationResult,
        *,
        expected_group_count: int,
        original_global_order_no: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """Validate splitOrder's strong acknowledgement without post-write reads."""

        summary: dict[str, Any] = {
            "status": "invalid",
            "source": "data.result",
            "expected_group_count": expected_group_count,
            "returned_count": 0,
            "unique_count": 0,
            "includes_original": False,
            "post_write_readback": "skipped",
        }
        data = result.details.get("data")
        if not isinstance(data, Mapping):
            summary["reason"] = "missing_data_mapping"
            return [], summary
        raw_result = data.get("result")
        if not isinstance(raw_result, list):
            summary["reason"] = "missing_result_list"
            return [], summary
        summary["returned_count"] = len(raw_result)
        values: list[str] = []
        for index, raw in enumerate(raw_result):
            if not isinstance(raw, Mapping):
                summary["reason"] = "invalid_result_item"
                summary["invalid_result_index"] = index
                return [], summary
            value = _text(raw.get("global_order_no"))
            if value is None:
                summary["reason"] = "missing_global_order_no"
                summary["invalid_result_index"] = index
                return [], summary
            values.append(value)
        summary["returned_order_nos"] = values
        summary["unique_count"] = len(set(values))
        summary["includes_original"] = original_global_order_no in values
        if len(values) != expected_group_count:
            summary["reason"] = "result_count_mismatch"
            return [], summary
        if len(set(values)) != len(values):
            summary["reason"] = "duplicate_global_order_no"
            return [], summary
        if original_global_order_no not in values:
            summary["reason"] = "original_global_order_no_missing"
            return [], summary
        summary["status"] = "complete"
        summary["reason"] = "complete_strong_acknowledgement"
        return values, summary

    @staticmethod
    def _instruction_split_group_index(
        plan: TentPackageSplitPlan,
        *,
        expected_group_count: int,
    ) -> int | None:
        """Map the unique Instruction package to data.result's request-order index."""

        matching_package_indexes = [
            index
            for index, package in enumerate(plan.packages_to_split)
            if any(_sku_key(item.sku) == _sku_key(INSTRUCTION_SKU) for item in package.items)
        ]
        if len(matching_package_indexes) != 1:
            return None
        leading_original_group_count = expected_group_count - len(plan.packages_to_split)
        if leading_original_group_count not in {0, 1}:
            return None
        return leading_original_group_count + matching_package_indexes[0]

    @staticmethod
    def _manual_review_outcome(exc: ManualReviewRequired) -> ApiWriteOutcome:
        result = exc.result
        return ApiWriteOutcome(
            status="manual_review",
            message=(result.message if result else str(exc)) or str(exc),
            request_id=result.request_id if result else None,
            details=dict(result.details) if result else {},
        )


__all__ = ["CustomOrderApiPlanError", "LingxingCustomOrderApiOperations"]
