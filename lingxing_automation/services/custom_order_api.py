"""API boundary for documented customization reads and mutations.

Contact writeback is intentionally handled by the browser orchestration so
phone and buyer e-mail share one detail-page save and readback verification.
Every other custom-order read and mutation crosses this API boundary.  The
phone method remains on this low-level protocol for explicit diagnostics and
compatibility, but the custom-order workflow does not call it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models import BatchOrderItem, OrderCustomZipBundle, OrderFolderLine
from .tent_package_split_adjuster import TentPackageSplitResult
from .tent_package_split_planner import TentPackageSplitPlan
from .tent_sku_adjuster import TentSkuAdjustmentResult
from .tent_sku_planner import TentSkuAdjustmentPlan
from .tent_warehouse_routing import TentRoutingPackage, TentWarehouseRoutingPlan


@dataclass(frozen=True)
class ApiWriteOutcome:
    """A transport-neutral API write result consumed by the browser flow."""

    status: str
    message: str = ""
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def manual_review_required(self) -> bool:
        return self.status == "manual_review"


@dataclass(frozen=True)
class InstructionRemarkOutcome(ApiWriteOutcome):
    action: str | None = None
    target_system_order_no: str | None = None


@dataclass(frozen=True)
class WarehouseLogisticsOutcome(ApiWriteOutcome):
    """帐篷仓库物流规划或写入结果。"""

    plan: TentWarehouseRoutingPlan | None = None


@dataclass(frozen=True)
class OrderProcessingStatus:
    """The current order disposition checked immediately before processing."""

    platform_order_no: str
    system_order_no: str
    buyer_cancel_requested: bool = False
    status_text: str = ""


@dataclass(frozen=True)
class CustomOrderApiContext:
    """Authoritative API data used by one custom-order processing run.

    The browser flow may use the identities below to open the one detail page
    needed for phone/e-mail writeback, but it must not rebuild business fields
    from the DOM.
    """

    item: BatchOrderItem
    system_order_nos: tuple[str, ...]
    recipient_name: str | None = None
    shipping_address_text: str = ""
    shipping_postal_code: str | None = None
    shipping_postal_source: str = "lingxing_openapi"
    request_ids: tuple[str, ...] = ()


class CustomOrderApiOperations(Protocol):
    """Documented Lingxing OpenAPI operations exposed to custom-order code."""

    async def get_order_context(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
    ) -> CustomOrderApiContext: ...

    async def download_custom_zip_bundle(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
        staging_root: str | Path,
        expected_zip_count: int | None,
        expected_order_item_ids: set[str] | None,
    ) -> OrderCustomZipBundle: ...

    async def get_shipping_deadline_text(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
    ) -> str | None: ...

    async def get_order_processing_status(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
    ) -> OrderProcessingStatus: ...

    async def update_phone(
        self,
        *,
        platform_order_no: str,
        system_order_no: str,
        phone: str,
    ) -> ApiWriteOutcome: ...

    async def update_tent_skus(
        self,
        *,
        plan: TentSkuAdjustmentPlan,
        order_lines: list[OrderFolderLine],
    ) -> TentSkuAdjustmentResult: ...

    async def split_tent_packages(
        self,
        *,
        plan: TentPackageSplitPlan,
    ) -> TentPackageSplitResult: ...

    async def set_instruction_remark(
        self,
        *,
        platform_order_no: str,
        candidate_system_order_nos: list[str],
        remark: str,
        target_system_order_no: str | None = None,
    ) -> InstructionRemarkOutcome: ...

    async def set_tent_warehouse_logistics(
        self,
        *,
        plan: TentSkuAdjustmentPlan,
        candidate_system_order_nos: list[str],
        apply: bool,
        projected_packages: tuple[TentRoutingPackage, ...] | None = None,
    ) -> WarehouseLogisticsOutcome: ...


__all__ = [
    "ApiWriteOutcome",
    "CustomOrderApiContext",
    "CustomOrderApiOperations",
    "InstructionRemarkOutcome",
    "OrderProcessingStatus",
    "WarehouseLogisticsOutcome",
]
