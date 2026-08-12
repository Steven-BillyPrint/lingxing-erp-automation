from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import erp_automation.application.custom_order_api as custom_order_api_module
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.application.lingxing_gateway import AttachmentData, OrderDetail
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import BatchOrderItem, OrderCustomZipBundle
from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_ORDER_SUMMARY_RESOLVED,
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityResult,
    AmazonOrderSummaryResult,
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


def test_api_custom_zip_resolves_multiple_amazon_items_from_one_lingxing_row(
    tmp_path: Path,
) -> None:
    """领星合并为一个数量行时，使用每个 ZIP JSON 的真实 OrderItemId。"""

    async def run() -> None:
        order_item_ids = ["166310972513321", "166676476984761"]
        filenames = [
            "B0DL6GL3D3_01_CustomizedInfo.zip",
            "B0DL6GL3D3_02_CustomizedInfo.zip",
        ]
        detail = OrderDetail(
            order_number="103731347446512771",
            payload={
                "order_number": "103731347446512771",
                "order_item": [
                    {
                        "platform_order_id": "702-8772842-4295444",
                        "order_item_no": order_item_ids[0],
                        "MSKU": "Custom-Table-Runner-24x72in",
                        "sku": "Custom-Table-Runner-24x72in",
                        "quantity": 2,
                        "newAttachments": [
                            {
                                "file_id": str(7000 + index),
                                "file_name": filename,
                                "file_type": 2,
                            }
                            for index, filename in enumerate(filenames)
                        ],
                    }
                ],
            },
        )
        attachments = {
            str(7000 + index): AttachmentData(
                content=_custom_zip(
                    order_id="702-8772842-4295444",
                    order_item_id=order_item_id,
                ),
                filename=filenames[index],
                content_type="application/zip",
            )
            for index, order_item_id in enumerate(order_item_ids)
        }

        class CollapsedRowGateway:
            def __init__(self) -> None:
                self.downloaded: list[str] = []

            async def get_order_detail(self, order_number: str) -> OrderDetail:
                assert order_number == "103731347446512771"
                return detail

            async def download_order_attachment(self, file_id: str) -> AttachmentData:
                self.downloaded.append(str(file_id))
                return attachments[str(file_id)]

        gateway = CollapsedRowGateway()
        operations = LingxingCustomOrderApiOperations(
            gateway,  # type: ignore[arg-type]
            attachment_download_min_interval_seconds=0,
        )

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no="702-8772842-4295444",
            system_order_no="103731347446512771",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids=set(order_item_ids),
        )

        assert bundle.status == "ok"
        assert gateway.downloaded == ["7000", "7001"]
        assert {item.order_item_id for item in bundle.zip_files} == set(order_item_ids)
        assert all(Path(item.zip_path).is_file() for item in bundle.zip_files)
        assert any(
            warning.endswith(f":{order_item_ids[1]}")
            for warning in bundle.warnings
            if warning.startswith("custom_zip_order_item_resolved_from_json:")
        )

    asyncio.run(run())


def test_api_custom_zip_downloads_space_every_attachment_request(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        order_item_ids = [f"item-{index}" for index in range(4)]
        items = []
        attachments: dict[str, AttachmentData] = {}
        for index, order_item_id in enumerate(order_item_ids):
            asin = f"B0{index:08d}"
            filename = f"{asin}_CustomizedInfo.zip"
            file_id = str(9000 + index)
            items.append(
                {
                    "platform_order_id": PLATFORM_ORDER_NO,
                    "order_item_no": order_item_id,
                    "sku": f"SKU-{index}",
                    "newAttachments": [
                        {
                            "file_id": file_id,
                            "file_name": filename,
                            "file_type": 2,
                        }
                    ],
                }
            )
            attachments[file_id] = AttachmentData(
                content=_custom_zip(order_item_id=order_item_id),
                filename=filename,
                content_type="application/zip",
            )

        class ConcurrentGateway:
            def __init__(self) -> None:
                self.started_at: list[float] = []

            async def get_order_detail(self, _order_number: str) -> OrderDetail:
                return OrderDetail(
                    order_number=SYSTEM_ORDER_NO,
                    payload={
                        "order_number": SYSTEM_ORDER_NO,
                        "order_item": items,
                    },
                )

            async def download_order_attachment(
                self,
                file_id: str,
            ) -> AttachmentData:
                self.started_at.append(clock[0])
                return attachments[str(file_id)]

        gateway = ConcurrentGateway()
        clock = [100.0]
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            clock[0] += delay

        operations = LingxingCustomOrderApiOperations(
            gateway,  # type: ignore[arg-type]
            attachment_download_concurrency=4,
            attachment_download_min_interval_seconds=2.0,
            attachment_download_clock=lambda: clock[0],
            sleeper=fake_sleep,
        )

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=4,
            expected_order_item_ids=set(order_item_ids),
        )

        assert bundle.status == "ok"
        assert gateway.started_at == [100.0, 102.0, 104.0, 106.0]
        assert delays == [2.0, 2.0, 2.0]
        assert [item.order_item_id for item in bundle.zip_files] == order_item_ids

    asyncio.run(run())


def test_api_custom_zip_retries_briefly_until_attachment_projection_appears(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        missing = _detail()
        missing.payload["order_item"][0]["newAttachments"] = []

        class EventuallyConsistentGateway(_Gateway):
            def __init__(self) -> None:
                super().__init__()
                self.details = [missing, self.detail]

            async def get_order_detail(self, order_number: str) -> OrderDetail:
                self.calls.append(("get_order_detail", order_number))
                return self.details.pop(0)

        delays: list[float] = []

        async def sleeper(seconds: float) -> None:
            delays.append(seconds)

        gateway = EventuallyConsistentGateway()
        operations = LingxingCustomOrderApiOperations(
            gateway,  # type: ignore[arg-type]
            sleeper=sleeper,
        )

        bundle = await operations.download_custom_zip_bundle(
            platform_order_no=PLATFORM_ORDER_NO,
            system_order_no=SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={ORDER_ITEM_ID},
        )

        assert bundle.status == "ok"
        assert delays == [0.25]
        assert gateway.calls[:2] == [
            ("get_order_detail", SYSTEM_ORDER_NO),
            ("get_order_detail", SYSTEM_ORDER_NO),
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
        assert gateway.calls == [
            ("get_order_detail", SYSTEM_ORDER_NO),
            ("get_order_detail", SYSTEM_ORDER_NO),
            ("get_order_detail", SYSTEM_ORDER_NO),
        ]
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


def test_collect_folder_context_always_uses_web_detail_recipient_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """API 返回什么姓名都不能覆盖原网页详情姓名。"""

    async def read_recipient(_page: object) -> str:
        return "Web Detail Name"

    class QuantityClient:
        async def get_order_items(
            self, platform_order_no: str
        ) -> AmazonOrderQuantityResult:
            return AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_RESOLVED,
                platform_order_no=platform_order_no,
                quantity=1,
                item_count=1,
                order_items=[],
            )

        async def get_order_summary(
            self, platform_order_no: str
        ) -> AmazonOrderSummaryResult:
            return AmazonOrderSummaryResult(
                status=AMAZON_ORDER_SUMMARY_RESOLVED,
                platform_order_no=platform_order_no,
                recipient_name="Amazon API Name",
            )

    monkeypatch.setattr(
        contact_sync,
        "read_detail_recipient_name",
        read_recipient,
    )

    context = asyncio.run(
        contact_sync.collect_order_folder_json_context(
            object(),
            BatchOrderItem(SYSTEM_ORDER_NO, PLATFORM_ORDER_NO, ""),
            QuantityClient(),  # type: ignore[arg-type]
            SYSTEM_ORDER_NO,
            staging_root=tmp_path,
            download_custom_zip=False,
            api_operations=object(),  # type: ignore[arg-type]
        )
    )

    assert context["recipient_name"] == "Web Detail Name"
    assert context["recipient_name_source"] == "lingxing_browser_detail"


@pytest.mark.parametrize(
    "api_status",
    [CUSTOM_ZIP_DOWNLOAD_ERROR, CUSTOM_ZIP_NOT_FOUND],
)
def test_collect_folder_context_does_not_use_browser_after_api_read_failure(
    monkeypatch,
    tmp_path: Path,
    api_status: str,
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
                status=api_status,
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

    assert context["zip_bundle"].status == api_status
    assert context["zip_bundle"].warnings == []
    assert context["zip_bundle"].error == "api sign fail"
    assert calls == ["api"]


def test_collect_folder_context_does_not_use_browser_for_attachment_rate_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def read_recipient(_page: object) -> str:
        return "Jane Doe"

    class QuantityClient:
        async def get_order_items(
            self,
            platform_order_no: str,
        ) -> AmazonOrderQuantityResult:
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
        async def download_custom_zip_bundle(
            self,
            **_kwargs: Any,
        ) -> OrderCustomZipBundle:
            calls.append("api")
            return OrderCustomZipBundle(
                platform_order_no=PLATFORM_ORDER_NO,
                status=CUSTOM_ZIP_DOWNLOAD_ERROR,
                error=(
                    "订单附件下载失败（code=3001008, "
                    "message=new requests too frequently. please request later.）"
                ),
            )

    async def confirm(*_args: Any, **_kwargs: Any) -> bool:
        calls.append("confirm")
        return True

    async def browser(*_args: Any, **_kwargs: Any) -> OrderCustomZipBundle:
        calls.append("browser")
        raise AssertionError("rate-limited API must not fall back to browser")

    monkeypatch.setattr(contact_sync, "read_detail_recipient_name", read_recipient)
    monkeypatch.setattr(
        contact_sync,
        "download_order_custom_zip_bundle",
        browser,
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
            interaction_policy=SimpleNamespace(
                confirm_browser_fallback=confirm
            ),
        )
    )

    assert context["zip_bundle"].status == CUSTOM_ZIP_DOWNLOAD_ERROR
    assert context["zip_bundle"].warnings == [
        "lingxing_attachment_rate_limited_browser_fallback_skipped"
    ]
    assert calls == ["api"]
