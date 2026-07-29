from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import erp_automation.application.custom_order_api as custom_order_api_module
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.application.lingxing_gateway import AttachmentData, OrderDetail
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import BatchOrderItem, OrderCustomZipBundle
from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityResult,
)
from lingxing_automation.services.custom_zip_downloader import (
    CUSTOM_ZIP_DOWNLOAD_ERROR,
    CUSTOM_ZIP_NOT_FOUND,
)


PLATFORM_ORDER_NO = "111-2222222-3333333"
SYSTEM_ORDER_NO = "103000000000000001"
ORDER_ITEM_ID = "164596991889281"


def _custom_zip(*, order_id: str = PLATFORM_ORDER_NO, order_item_id: str = ORDER_ITEM_ID) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{order_item_id}.json",
            json.dumps(
                {
                    "orderId": order_id,
                    "orderItemId": order_item_id,
                    "asin": "B0CRRGTPFH",
                }
            ),
        )
        archive.writestr("artwork/example.txt", "safe")
    return output.getvalue()


def _detail(*, file_name: str = "B0CRRGTPFH_CustomizedInfo.zip", order_item_id: str = ORDER_ITEM_ID) -> OrderDetail:
    return OrderDetail(
        order_number=SYSTEM_ORDER_NO,
        payload={
            "order_number": SYSTEM_ORDER_NO,
            "order_item": [
                {
                    "platform_order_id": PLATFORM_ORDER_NO,
                    "MSKU": "TENT-MSKU",
                    "order_item_no": order_item_id,
                    "sku": "LOCAL-SKU",
                    "quality": 1,
                    "newAttachments": [
                        {
                            "file_id": 987654321,
                            "file_name": file_name,
                            "file_type": 2,
                        }
                    ],
                }
            ],
        },
        request_id="detail-request",
    )


class _Gateway:
    def __init__(
        self,
        *,
        detail: OrderDetail | None = None,
        attachment: AttachmentData | None = None,
    ) -> None:
        self.detail = detail or _detail()
        self.attachment = attachment or AttachmentData(
            content=_custom_zip(),
            filename="B0CRRGTPFH_CustomizedInfo.zip",
            content_type="application/zip",
            request_id="download-request",
        )
        self.calls: list[tuple[str, str]] = []

    async def get_order_detail(self, order_number: str) -> OrderDetail:
        self.calls.append(("get_order_detail", order_number))
        return self.detail

    async def download_order_attachment(self, file_id: str) -> AttachmentData:
        self.calls.append(("download_order_attachment", file_id))
        return self.attachment

    async def download_custom_attachment(self, file_id: str) -> AttachmentData:
        raise AssertionError(
            f"Order newAttachments file_id must not use custom-file endpoint: {file_id}"
        )


def test_api_custom_zip_download_validates_and_atomically_writes_staging(tmp_path: Path) -> None:
    async def run() -> None:
        gateway = _Gateway()
        expected_content = gateway.attachment.content
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == "ok"
        assert len(bundle.zip_files) == 1
        zip_file = bundle.zip_files[0]
        assert zip_file.order_item_id == ORDER_ITEM_ID
        assert zip_file.platform_order_no == PLATFORM_ORDER_NO
        assert zip_file.trigger_text == "lingxing_api:file_id=987654321"
        assert Path(zip_file.zip_path).read_bytes() == expected_content
        assert Path(zip_file.zip_path).parent == tmp_path / PLATFORM_ORDER_NO
        assert not list((tmp_path / PLATFORM_ORDER_NO).glob(".lingxing-api-*.tmp"))
        assert gateway.calls == [
            ("get_order_detail", SYSTEM_ORDER_NO),
            ("download_order_attachment", "987654321"),
        ]

    asyncio.run(run())


def test_api_custom_zip_requires_exact_expected_order_item_before_download(tmp_path: Path) -> None:
    async def run() -> None:
        gateway = _Gateway()
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={"different-item"},
        )

        assert bundle.status == CUSTOM_ZIP_NOT_FOUND
        assert "different-item" in (bundle.error or "")
        assert gateway.calls == [("get_order_detail", SYSTEM_ORDER_NO)]
        assert not (tmp_path / PLATFORM_ORDER_NO).exists()

    asyncio.run(run())


def test_api_custom_zip_rejects_unsafe_detail_filename_without_downloading(tmp_path: Path) -> None:
    async def run() -> None:
        gateway = _Gateway(detail=_detail(file_name="../escape.zip"))
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == CUSTOM_ZIP_DOWNLOAD_ERROR
        assert "文件名" in (bundle.error or "")
        assert gateway.calls == [("get_order_detail", SYSTEM_ORDER_NO)]
        assert not (tmp_path / "escape.zip").exists()

    asyncio.run(run())


def test_api_custom_zip_rejects_invalid_magic_and_leaves_no_partial_file(tmp_path: Path) -> None:
    async def run() -> None:
        gateway = _Gateway(
            attachment=AttachmentData(
                content=b"not-a-zip",
                filename="B0CRRGTPFH_CustomizedInfo.zip",
                content_type="application/octet-stream",
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == CUSTOM_ZIP_DOWNLOAD_ERROR
        assert "ZIP 魔数" in (bundle.error or "")
        assert not (tmp_path / PLATFORM_ORDER_NO).exists()

    asyncio.run(run())


def test_api_custom_zip_rejects_download_over_size_limit(monkeypatch, tmp_path: Path) -> None:
    async def run() -> None:
        content = _custom_zip()
        monkeypatch.setattr(custom_order_api_module, "MAX_CUSTOM_ZIP_BYTES", len(content) - 1)
        gateway = _Gateway(
            attachment=AttachmentData(
                content=content,
                filename="B0CRRGTPFH_CustomizedInfo.zip",
                content_type="application/zip",
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == CUSTOM_ZIP_DOWNLOAD_ERROR
        assert "大小不在安全范围" in (bundle.error or "")
        assert not (tmp_path / PLATFORM_ORDER_NO).exists()

    asyncio.run(run())


def test_api_custom_zip_rejects_json_order_item_mismatch(tmp_path: Path) -> None:
    async def run() -> None:
        gateway = _Gateway(
            attachment=AttachmentData(
                content=_custom_zip(order_item_id="wrong-item"),
                filename="B0CRRGTPFH_CustomizedInfo.zip",
                content_type="application/zip",
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == CUSTOM_ZIP_DOWNLOAD_ERROR
        assert "orderItemId" in (bundle.error or "")
        assert not (tmp_path / PLATFORM_ORDER_NO).exists()

    asyncio.run(run())


def test_collect_folder_context_routes_zip_to_api_without_browser_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def read_recipient(_page: object) -> str:
        calls.append("page_recipient")
        return "Jane Doe"

    async def forbidden_browser_download(*_args: Any, **_kwargs: Any) -> OrderCustomZipBundle:
        raise AssertionError("API-injected ZIP collection must not use browser automation")

    class QuantityClient:
        async def get_order_items(self, platform_order_no: str) -> AmazonOrderQuantityResult:
            return AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_RESOLVED,
                platform_order_no=platform_order_no,
                quantity=1,
                item_count=1,
                order_items=[
                    {
                        "asin": "B0CRRGTPFH",
                        "seller_sku": "TENT-MSKU",
                        "quantity_ordered": 1,
                        "order_item_id": ORDER_ITEM_ID,
                    }
                ],
            )

    class Operations:
        async def download_custom_zip_bundle(self, **kwargs: Any) -> OrderCustomZipBundle:
            calls.append("api_zip")
            assert kwargs["platform_order_no"] == PLATFORM_ORDER_NO
            assert kwargs["system_order_no"] == SYSTEM_ORDER_NO
            assert kwargs["expected_zip_count"] == 1
            assert kwargs["expected_order_item_ids"] == {ORDER_ITEM_ID}
            return OrderCustomZipBundle(
                platform_order_no=PLATFORM_ORDER_NO,
                status=CUSTOM_ZIP_DOWNLOAD_ERROR,
                error="injected API failure",
            )

    monkeypatch.setattr(contact_sync, "read_detail_recipient_name", read_recipient)
    monkeypatch.setattr(
        contact_sync,
        "download_order_custom_zip_bundle",
        forbidden_browser_download,
    )

    context = asyncio.run(
        contact_sync.collect_order_folder_json_context(
            object(),
            BatchOrderItem(SYSTEM_ORDER_NO, PLATFORM_ORDER_NO, ""),
            QuantityClient(),  # type: ignore[arg-type]
            SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            download_custom_zip=True,
            api_operations=Operations(),  # type: ignore[arg-type]
        )
    )

    assert context["recipient_name"] == "Jane Doe"
    assert context["zip_bundle"].error == "injected API failure"
    assert calls == ["page_recipient", "api_zip"]


def test_collect_folder_context_asks_then_uses_browser_after_api_read_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def read_recipient(_page: object) -> str:
        return "Jane Doe"

    class QuantityClient:
        async def get_order_items(self, platform_order_no: str) -> AmazonOrderQuantityResult:
            return AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_RESOLVED,
                platform_order_no=platform_order_no,
                quantity=1,
                item_count=1,
                order_items=[
                    {
                        "asin": "B0CRRGTPFH",
                        "quantity_ordered": 1,
                        "order_item_id": ORDER_ITEM_ID,
                    }
                ],
            )

    class Operations:
        async def download_custom_zip_bundle(self, **_kwargs: Any) -> OrderCustomZipBundle:
            calls.append("api")
            return OrderCustomZipBundle(
                platform_order_no=PLATFORM_ORDER_NO,
                status=CUSTOM_ZIP_DOWNLOAD_ERROR,
                error="api sign fail",
            )

    async def confirm(operation: str, error: str, is_write: bool) -> bool:
        calls.append(f"confirm:{operation}:{is_write}:{error}")
        return True

    async def browser(*_args: Any, **_kwargs: Any) -> OrderCustomZipBundle:
        calls.append("browser")
        return OrderCustomZipBundle(
            platform_order_no=PLATFORM_ORDER_NO,
            status=CUSTOM_ZIP_NOT_FOUND,
            error="browser fixture complete",
        )

    monkeypatch.setattr(contact_sync, "read_detail_recipient_name", read_recipient)
    monkeypatch.setattr(contact_sync, "download_order_custom_zip_bundle", browser)

    context = asyncio.run(
        contact_sync.collect_order_folder_json_context(
            object(),
            BatchOrderItem(SYSTEM_ORDER_NO, PLATFORM_ORDER_NO, ""),
            QuantityClient(),  # type: ignore[arg-type]
            SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            download_custom_zip=True,
            api_operations=Operations(),  # type: ignore[arg-type]
            interaction_policy=SimpleNamespace(confirm_browser_fallback=confirm),
        )
    )

    assert context["zip_bundle"].status == CUSTOM_ZIP_NOT_FOUND
    assert context["zip_bundle"].warnings == [
        "订单附件 API 失败后经用户确认改用网页：api sign fail"
    ]
    assert calls == ["api", "confirm:custom_zip_download:False:api sign fail", "browser"]
