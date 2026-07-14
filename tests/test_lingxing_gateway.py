from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from erp_automation.application import (
    CapabilityRouter,
    FAST_OUTBOUND_FAILED,
    FAST_OUTBOUND_RESULT_STATE_KEY,
    FAST_OUTBOUND_SUCCEEDED,
    LingxingGateway,
    ManualReviewRequired,
    MutationResult,
    MutationState,
    MutationVerification,
    OrderPage,
    VerificationOutcome,
)
from erp_automation.integrations.lingxing import (
    APIResponse,
    BinaryResponse,
    LingxingAPIError,
    LingxingAmbiguousWriteError,
    LingxingTransportError,
)


def api_response(
    data=None,
    *,
    code: str = "0",
    message: str = "success",
    request_id: str = "request-1",
) -> APIResponse:
    return APIResponse(
        code=code,
        message=message,
        data=data,
        request_id=request_id,
        response_time="2026-07-14 12:00:00",
        raw={"code": code, "message": message, "data": data},
    )


class RecordingClient:
    def __init__(self) -> None:
        self.outcomes: dict[str, list[object]] = defaultdict(list)
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def queue(self, method: str, *outcomes: object) -> None:
        self.outcomes[method].extend(outcomes)

    def __getattr__(self, method: str):
        async def invoke(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if not self.outcomes[method]:
                raise AssertionError(f"no queued outcome for {method}")
            outcome = self.outcomes[method].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return invoke


def browser_success(message: str = "browser") -> MutationResult:
    return MutationResult(
        state=MutationState.SUCCEEDED,
        source="browser",
        message=message,
    )


def test_order_page_is_normalized_with_stable_identifiers_and_pagination() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "list_orders",
            api_response(
                {
                    "list": [
                        {
                            "global_order_no": 103000000000000001,
                            "order_number": "ORDER-1",
                            "order_status": 4,
                        },
                        {"global_order_no": "103000000000000002"},
                    ],
                    "total": 3,
                },
                request_id="orders-request",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        page = await gateway.list_orders(
            offset=0,
            length=2,
            filters={"order_status": 4},
        )

        assert isinstance(page, OrderPage)
        assert [item.global_order_no for item in page.items] == [
            "103000000000000001",
            "103000000000000002",
        ]
        assert page.items[0].order_number == "ORDER-1"
        assert page.items[0].payload["order_status"] == 4
        assert page.total == 3
        assert page.next_offset == 2
        assert page.request_id == "orders-request"
        assert client.calls == [
            ("list_orders", (), {"offset": 0, "length": 2, "order_status": 4})
        ]

    asyncio.run(run())


def test_read_transport_failure_safely_falls_back_to_browser() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue("list_orders", LingxingTransportError("list_orders"))
        expected = OrderPage(items=(), offset=0, length=500, total=0)
        browser_calls = 0

        async def browser() -> OrderPage:
            nonlocal browser_calls
            browser_calls += 1
            return expected

        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        result = await gateway.list_orders(browser=browser)

        assert result is expected
        assert browser_calls == 1

    asyncio.run(run())


def test_detail_and_attachment_are_normalized_without_network() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "get_fbm_order_detail",
            api_response({"global_order_no": "103", "receiver_tel": "555"}),
        )
        client.queue(
            "download_attachment",
            BinaryResponse(
                content=b"PK\x03\x04",
                filename="custom.zip",
                content_type="application/zip",
                request_id="attachment-request",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        detail = await gateway.get_order_detail("ORDER-1")
        attachment = await gateway.download_attachment("file-1")

        assert detail.order_number == "ORDER-1"
        assert detail.payload["global_order_no"] == "103"
        assert attachment.content == b"PK\x03\x04"
        assert attachment.filename == "custom.zip"
        assert attachment.request_id == "attachment-request"
        assert [call[0] for call in client.calls] == [
            "get_fbm_order_detail",
            "download_attachment",
        ]

    asyncio.run(run())


def test_custom_attachment_gateway_calls_dedicated_client_method() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "download_custom_attachment",
            api_response(
                [
                    {
                        "file_name": "custom.zip",
                        "mime_type": "application/zip",
                        "content": "UEsDBA==",
                    }
                ],
                request_id="custom-request",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        attachment = await gateway.download_custom_attachment("file-2")

        assert attachment.filename == "custom.zip"
        assert attachment.content == b"PK\x03\x04"
        assert attachment.request_id == "custom-request"
        assert client.calls[0][0] == "download_custom_attachment"

    asyncio.run(run())


def test_buyer_email_is_explicitly_browser_only() -> None:
    async def run() -> None:
        client = RecordingClient()
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        result = await gateway.update_buyer_email(
            browser=lambda: browser_success("email changed in browser")
        )

        assert gateway.buyer_email_api_supported is False
        assert result.source == "browser"
        assert client.calls == []

    asyncio.run(run())


def test_phone_update_builds_only_documented_api_payload_and_normalizes_success() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            api_response({"error_details": []}, code="10002", request_id="phone-request"),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        result = await gateway.update_phone(
            "103000000000000001",
            "5551234567",
            order_item_list=[{"item_id": "item-1", "quantity": 2}],
        )

        assert result.state is MutationState.SUCCEEDED
        assert result.source == "lingxing_api"
        assert result.request_id == "phone-request"
        assert result.details["api_code"] == "10002"
        assert client.calls == [
            (
                "update_orders",
                (
                    [
                        {
                            "global_order_no": "103000000000000001",
                            "address_info": {"receiver_tel": "5551234567"},
                            "order_item_list": [{"item_id": "item-1", "quantity": 2}],
                        }
                    ],
                ),
                {},
            )
        ]

    asyncio.run(run())


def test_phone_update_nonempty_target_error_details_is_definite_failure() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            api_response(
                {
                    "error_details": [
                        {
                            "global_order_no": "103000000000000001",
                            "message": "order cannot be edited",
                        }
                    ]
                },
                code="10002",
                request_id="phone-failed",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        result = await gateway.update_phone(
            "103000000000000001",
            "5551234567",
            order_item_list=[],
        )

        assert result.state is MutationState.FAILED
        assert result.definitely_not_executed is True
        assert result.request_id == "phone-failed"
        assert result.details["ack_validation"] == "target_failed"

    asyncio.run(run())


def test_phone_update_ambiguous_error_details_requires_manual_review() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            api_response(
                {"error_details": [{"message": "unknown target"}]},
                code="10002",
                request_id="phone-ambiguous",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        with pytest.raises(ManualReviewRequired) as captured:
            await gateway.update_phone(
                "103000000000000001",
                "5551234567",
                order_item_list=[],
            )

        assert captured.value.result is not None
        assert captured.value.result.state is MutationState.UNKNOWN
        assert captured.value.result.request_id == "phone-ambiguous"

    asyncio.run(run())


def test_ambiguous_update_order_ack_cannot_be_promoted_by_readback() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            api_response(
                {"error_details": [{"message": "unknown target"}]},
                code="10002",
                request_id="phone-ambiguous",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        verification_calls = 0

        async def verify(_initial: MutationResult) -> MutationVerification:
            nonlocal verification_calls
            verification_calls += 1
            return MutationVerification(
                VerificationOutcome.CONFIRMED_APPLIED,
                message="would otherwise look applied",
            )

        with pytest.raises(ManualReviewRequired) as captured:
            await gateway.update_phone(
                "103000000000000001",
                "5551234567",
                order_item_list=[],
                verify=verify,
            )

        assert verification_calls == 0
        assert captured.value.result is not None
        assert captured.value.result.state is MutationState.UNKNOWN
        assert captured.value.result.details["verification_blocked_by_ack"] is True

    asyncio.run(run())


def test_partial_api_error_stays_unknown_and_never_retries_in_browser() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            LingxingAPIError(
                "update_order",
                "10001",
                "partial",
                request_id="partial-request",
                payload={"data": {"error_details": [{"message": "unknown target"}]}},
            ),
        )
        browser_calls = 0

        async def browser() -> MutationResult:
            nonlocal browser_calls
            browser_calls += 1
            return browser_success()

        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        with pytest.raises(ManualReviewRequired) as captured:
            await gateway.update_order_items(
                "103",
                [{"item_id": "item-1", "quantity": 1}],
                browser=browser,
                approve_browser_fallback=lambda *_: True,
            )

        assert captured.value.result is not None
        assert captured.value.result.state is MutationState.UNKNOWN
        assert captured.value.result.request_id == "partial-request"
        assert captured.value.result.definitely_not_executed is False
        assert captured.value.result.details["data"] == {
            "error_details": [{"message": "unknown target"}]
        }
        assert captured.value.result.details["verification_blocked_by_ack"] is True
        assert browser_calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("partial_code", ["10000", "10001"])
def test_partial_target_error_cannot_be_promoted_to_success_by_verify(
    partial_code: str,
) -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "update_orders",
            LingxingAPIError(
                "update_order",
                partial_code,
                "partial",
                request_id=f"partial-target-{partial_code}",
                payload={
                    "code": int(partial_code),
                    "data": {
                        "error_details": [
                            {
                                "global_order_no": "103",
                                "message": "order cannot be edited",
                            }
                        ]
                    },
                },
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        verification_calls = 0

        async def verify(_initial: MutationResult) -> MutationVerification:
            nonlocal verification_calls
            verification_calls += 1
            return MutationVerification(
                VerificationOutcome.CONFIRMED_APPLIED,
                message="would otherwise look applied",
            )

        result = await gateway.update_order_items(
            "103",
            [{"item_id": "item-1", "quantity": 1}],
            verify=verify,
        )

        assert result.state is MutationState.FAILED
        assert result.definitely_not_executed is True
        assert result.request_id == f"partial-target-{partial_code}"
        assert result.details["ack_validation"] == "target_failed"
        assert result.details["verification_blocked_by_ack"] is True
        assert result.details["api_payload"]["data"]["error_details"][0][
            "global_order_no"
        ] == "103"
        assert verification_calls == 0

    asyncio.run(run())


def test_authentication_rejection_is_definite_failure_before_business_execution() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "set_order_remarks",
            LingxingAPIError(
                "set_order_remark",
                "2001006",
                "bad signature",
                request_id="rejected-request",
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        result = await gateway.set_order_remark("103", "说明书")

        assert result.state is MutationState.FAILED
        assert result.definitely_not_executed is True
        assert result.request_id == "rejected-request"
        assert result.details["api_code"] == "2001006"

    asyncio.run(run())


def test_ambiguous_split_can_be_reconciled_by_read_after_write_hook() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "split_order",
            LingxingAmbiguousWriteError("split_order", request_id="split-request"),
        )
        seen_initial: list[MutationState] = []

        async def verify(initial: MutationResult) -> MutationVerification:
            seen_initial.append(initial.state)
            return MutationVerification(
                VerificationOutcome.CONFIRMED_APPLIED,
                message="two packages observed",
                after={"package_count": 2},
            )

        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        result = await gateway.split_order(
            "103",
            [
                [{"item_id": "source", "quantity": 1}],
                [{"item_id": "new", "quantity": 2}],
            ],
            verify=verify,
        )

        assert seen_initial == [MutationState.UNKNOWN]
        assert result.state is MutationState.SUCCEEDED
        assert result.request_id == "split-request"
        assert result.after == {"package_count": 2}
        assert result.details["verification"] == "confirmed_applied"

    asyncio.run(run())


def test_ambiguous_split_without_confirmation_never_calls_browser() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue("split_order", LingxingAmbiguousWriteError("split_order"))
        browser_calls = 0

        async def browser() -> MutationResult:
            nonlocal browser_calls
            browser_calls += 1
            return browser_success()

        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        with pytest.raises(ManualReviewRequired) as captured:
            await gateway.split_order(
                "103",
                [
                    [{"item_id": "source", "quantity": 1}],
                    [{"item_id": "new", "quantity": 1}],
                ],
                browser=browser,
                approve_browser_fallback=lambda *_: True,
            )

        assert captured.value.result.state is MutationState.UNKNOWN
        assert browser_calls == 0

    asyncio.run(run())


def test_confirmed_not_applied_allows_explicitly_approved_browser_fallback() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue("deliver_orders", LingxingAmbiguousWriteError("deliver_orders"))
        browser_calls = 0

        async def verify(_initial: MutationResult) -> MutationVerification:
            return MutationVerification(
                VerificationOutcome.CONFIRMED_NOT_APPLIED,
                message="status remained pending and API task does not exist",
            )

        async def browser() -> MutationResult:
            nonlocal browser_calls
            browser_calls += 1
            return browser_success("fallback outbound")

        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]
        result = await gateway.deliver_orders(
            ["ORDER-1"],
            verify=verify,
            browser=browser,
            approve_browser_fallback=lambda _capability, api_result: (
                api_result is not None and api_result.definitely_not_executed
            ),
        )

        assert result.source == "browser"
        assert browser_calls == 1

    asyncio.run(run())


def test_warehouse_logistics_wms_and_fast_outbound_results_are_normalized() -> None:
    async def run() -> None:
        client = RecordingClient()
        client.queue(
            "list_warehouses",
            api_response({"list": [{"wid": 7, "warehouse_name": "默认仓"}], "total": 1}),
        )
        client.queue(
            "list_logistics_types",
            api_response(
                {
                    "list": [{"logistics_type_id": 9, "logistics_type_name": "UPS"}],
                    "total": 1,
                }
            ),
        )
        client.queue(
            "list_wms_orders",
            APIResponse(
                code="0",
                message="success",
                data=[{"waybill_no": "WB-1"}],
                request_id="request-1",
                response_time="2026-07-14 12:00:00",
                raw={"code": 0, "data": [{"waybill_no": "WB-1"}], "total": 1},
            ),
        )
        client.queue(
            "get_fast_outbound_result",
            api_response(
                {
                    "success": [{"global_order_no": "103", "wo_number": "WO-1"}],
                    "failure": [
                        {"global_order_no": "104", "error_message": "正在处理"}
                    ],
                }
            ),
        )
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        warehouses = await gateway.list_warehouses()
        logistics = await gateway.list_logistics_types(provider_type=1)
        waybills = await gateway.list_wms_orders(filters={"status": 1})
        outbound = await gateway.get_fast_outbound_result(["103"])

        assert warehouses.items[0].identifier == "7"
        assert warehouses.items[0].name == "默认仓"
        assert logistics.items[0].identifier == "9"
        assert logistics.items[0].name == "UPS"
        assert waybills.items == ({"waybill_no": "WB-1"},)
        assert waybills.total == 1
        assert outbound == (
            {
                "global_order_no": "103",
                "wo_number": "WO-1",
                FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED,
            },
            {
                "global_order_no": "104",
                "error_message": "正在处理",
                FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
            },
        )
        assert client.calls[2] == (
            "list_wms_orders",
            (),
            {"status": 1, "page": 1, "page_size": 100},
        )

    asyncio.run(run())


def test_remaining_documented_write_methods_are_routed_and_normalized() -> None:
    async def run() -> None:
        client = RecordingClient()
        for method in (
            "edit_order_logistics",
            "review_orders",
            "set_tracking_no",
            "fast_outbound",
        ):
            client.queue(method, api_response({"accepted": True}, code="0"))
        gateway = LingxingGateway(client, CapabilityRouter())  # type: ignore[arg-type]

        shipping = await gateway.set_shipping_channel(
            [{"global_order_no": "103", "logistics_type_id": 9}]
        )
        review = await gateway.review_orders(["103", 104])
        tracking = await gateway.set_tracking_no(
            waybill_no="WB-1",
            wo_number="WO-1",
            tracking_no="TRACK-1",
            logistics_freight=12.5,
            logistics_freight_currency_code="USD",
        )
        outbound = await gateway.fast_outbound(
            [{"global_order_no": "103", "package_no": "P1"}]
        )

        assert all(
            result.state is MutationState.SUCCEEDED
            for result in (shipping, review, tracking, outbound)
        )
        assert client.calls[0] == (
            "edit_order_logistics",
            ([{"global_order_no": "103", "logistics_type_id": 9}],),
            {},
        )
        assert client.calls[1] == ("review_orders", (["103", "104"],), {})
        assert client.calls[2][0] == "set_tracking_no"
        assert client.calls[2][2]["tracking_no"] == "TRACK-1"
        assert client.calls[3] == (
            "fast_outbound",
            ([{"global_order_no": "103", "package_no": "P1"}],),
            {},
        )

    asyncio.run(run())
