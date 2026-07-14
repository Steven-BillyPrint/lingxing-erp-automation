"""API mutation boundary for the customization workflow.

The legacy browser workflow owns the orchestration and the steps for which
Lingxing does not expose an OpenAPI (buyer e-mail and the unmasked address).
The desktop application injects an implementation of this protocol so every
documented ERP mutation is executed by OpenAPI instead of by DOM automation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models import OrderCustomZipBundle, OrderFolderLine
from .tent_package_split_adjuster import TentPackageSplitResult
from .tent_package_split_planner import TentPackageSplitPlan
from .tent_sku_adjuster import TentSkuAdjustmentResult
from .tent_sku_planner import TentSkuAdjustmentPlan


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


class CustomOrderApiOperations(Protocol):
    """Documented Lingxing OpenAPI operations used by a custom-order run."""

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
    ) -> InstructionRemarkOutcome: ...


__all__ = [
    "ApiWriteOutcome",
    "CustomOrderApiOperations",
    "InstructionRemarkOutcome",
]
