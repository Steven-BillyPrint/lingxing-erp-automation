from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from erp_automation.operations.customer_shipping_preflight import (
    probe_customer_shipping_fields,
)


class RecordingClient:
    def __init__(self, rows, details) -> None:
        self.rows = rows
        self.details = details
        self.list_calls = []
        self.detail_calls = []

    async def list_orders(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        return SimpleNamespace(
            data={"list": list(self.rows)},
            request_id="list-request-safe-id",
        )

    async def get_fbm_order_detail(self, system_order_no):
        self.detail_calls.append(system_order_no)
        value = self.details[system_order_no]
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(
            data=value,
            request_id="detail-request-safe-id",
        )


def _row(system_order_no, platform_order_no, service):
    return {
        "global_order_no": system_order_no,
        "customer_shipping_list": [service],
        "platform_info": [
            {
                "platform_code": "10001",
                "platform_order_no": platform_order_no,
            }
        ],
        "logistics_info": {"logistics_type_name": "UPS-全程"},
    }


def test_live_preflight_proves_list_and_detail_fields_without_writes() -> None:
    client = RecordingClient(
        [
            _row(
                "103729226050104832",
                "113-2331005-1038665-2",
                "Standard",
            )
        ],
        {
            "103729226050104832": {
                "order_number": "103729226050104832",
                "buyer_choose_express": "Standard",
                "logistics_type_name": "UPS-全程",
            }
        },
    )

    result = asyncio.run(
        probe_customer_shipping_fields(
            client,
            now_epoch=1_800_000_000,
            randomizer=random.Random(7),
        )
    )

    assert result["status"] == "passed"
    assert result["list_request_id"] == "list-request-safe-id"
    assert result["list_authoritative_field"] == "customer_shipping_list"
    assert result["list_customer_shipping_service"] == "standard"
    assert result["detail_authoritative_field"] == "buyer_choose_express"
    assert result["detail_request_id"] == "detail-request-safe-id"
    assert result["detail_customer_shipping_service"] == "standard"
    assert result["external_write_calls"] == 0
    assert client.list_calls == [
        {
            "offset": 0,
            "length": 100,
            "platform_code": [10001],
            "date_type": "update_time",
            "start_time": 1_797_408_000,
            "end_time": 1_800_000_001,
        }
    ]
    assert client.detail_calls == ["103729226050104832"]


def test_live_preflight_rejects_logistics_route_without_documented_field() -> None:
    client = RecordingClient(
        [
            {
                "global_order_no": "103729226050104832",
                "platform_info": [
                    {
                        "platform_code": "10001",
                        "platform_order_no": "113-2331005-1038665-2",
                    }
                ],
                "logistics_info": {"logistics_type_name": "UPS-全程"},
            }
        ],
        {},
    )

    with pytest.raises(RuntimeError, match="customer_shipping_list"):
        asyncio.run(
            probe_customer_shipping_fields(
                client,
                now_epoch=1_800_000_000,
                randomizer=random.Random(7),
            )
        )

    assert client.detail_calls == []
