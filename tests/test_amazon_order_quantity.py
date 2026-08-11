from __future__ import annotations

import json

from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_QUANTITY_CONFIG_MISSING,
    AMAZON_QUANTITY_NO_MATCH,
    AMAZON_QUANTITY_RESOLVED,
    AMAZON_ORDER_SUMMARY_RESOLVED,
    AmazonOrderQuantityClient,
    AmazonOrderQuantityConfig,
    select_order_item_quantity,
)


def test_select_order_item_quantity_sums_same_asin_and_sku():
    """验证Amazon 订单数量查询中的选择 订单行 数量汇总相同ASIN并SKU场景。"""
    items = [
        {"ASIN": "B0CQLN5GNL", "SellerSKU": 'BillyPrint-Car Magnet-12"x24"-2', "QuantityOrdered": 1, "OrderItemId": "a"},
        {"ASIN": "B0CQLN5GNL", "SellerSKU": 'BillyPrint-Car Magnet-12"x24"-2', "QuantityOrdered": 1, "OrderItemId": "b"},
        {"ASIN": "B0DRCY4HM5", "SellerSKU": "Car-Magent-3x10in-1pcs", "QuantityOrdered": 30, "OrderItemId": "c"},
    ]

    selected = select_order_item_quantity(items, asin="B0CQLN5GNL", sku='BillyPrint-Car Magnet-12"x24"-2')

    assert selected["quantity"] == 2
    assert [item["order_item_id"] for item in selected["matched_items"]] == ["a", "b"]


def test_select_order_item_quantity_uses_asin_when_sku_does_not_match():
    """验证Amazon 订单数量查询中的选择 订单行 数量使用ASIN当SKU 不会 匹配场景。"""
    items = [
        {"ASIN": "B0CRRGTPFH", "SellerSKU": "TENT-ROLL-BAG-10X10-50MM", "QuantityOrdered": 2, "OrderItemId": "x"},
    ]

    selected = select_order_item_quantity(items, asin="B0CRRGTPFH", sku="canopytents 共2")

    assert selected["quantity"] == 2


def test_amazon_order_quantity_client_requests_lwa_rdt_then_order_items():
    """验证Amazon 订单数量查询中的Amazon订单数量客户端请求LWARDT再处理 订单行场景。"""
    calls: list[dict] = []

    def fake_transport(method, url, headers, body, timeout):
        """模拟请求传输行为，隔离测试中的外部依赖。"""
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body.decode("utf-8") if body else None,
            }
        )
        if url == "https://api.amazon.com/auth/o2/token":
            return 200, {}, json.dumps({"access_token": "LWA", "expires_in": 3600}).encode()
        if url.endswith("/tokens/2021-03-01/restrictedDataToken"):
            assert headers["x-amz-access-token"] == "LWA"
            return 200, {}, json.dumps({"restrictedDataToken": "RDT", "expiresIn": 3600}).encode()
        if url.endswith("/orders/v0/orders/112-5663586-1765001/orderItems"):
            assert headers["x-amz-access-token"] == "RDT"
            return (
                200,
                {},
                json.dumps(
                    {
                        "payload": {
                            "AmazonOrderId": "112-5663586-1765001",
                            "OrderItems": [
                                {"ASIN": "B0CQLN5GNL", "SellerSKU": "sku-a", "QuantityOrdered": 1, "OrderItemId": "1"},
                                {"ASIN": "B0CQLN5GNL", "SellerSKU": "sku-a", "QuantityOrdered": 1, "OrderItemId": "2"},
                            ],
                        }
                    }
                ).encode(),
            )
        raise AssertionError(url)

    client = AmazonOrderQuantityClient(
        AmazonOrderQuantityConfig(
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            endpoint="https://sellingpartnerapi-na.amazon.com",
        ),
        transport=fake_transport,
    )

    result = client.get_order_item_quantity_sync("112-5663586-1765001", "B0CQLN5GNL", "sku-a")

    assert result.status == AMAZON_QUANTITY_RESOLVED
    assert result.quantity == 2
    rdt_body = json.loads(calls[1]["body"])
    assert rdt_body == {
        "restrictedResources": [
            {
                "method": "GET",
                "path": "/orders/v0/orders/112-5663586-1765001/orderItems",
                "dataElements": ["buyerInfo"],
            }
        ]
    }


def test_amazon_order_quantity_client_reports_missing_config():
    """验证Amazon 订单数量查询中的Amazon订单数量客户端报告缺失配置场景。"""
    client = AmazonOrderQuantityClient(None)

    result = client.get_order_item_quantity_sync("112-5663586-1765001", "B0CQLN5GNL")

    assert result.status == AMAZON_QUANTITY_CONFIG_MISSING
    assert result.quantity is None


def test_amazon_order_summary_reads_authoritative_order_total_and_destination():
    calls: list[dict] = []

    def fake_transport(method, url, headers, body, timeout):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body.decode("utf-8") if body else None,
            }
        )
        if url == "https://api.amazon.com/auth/o2/token":
            return 200, {}, json.dumps(
                {"access_token": "LWA", "expires_in": 3600}
            ).encode()
        if url.endswith("/tokens/2021-03-01/restrictedDataToken"):
            return 200, {}, json.dumps(
                {"restrictedDataToken": "RDT", "expiresIn": 3600}
            ).encode()
        if url.endswith("/orders/v0/orders/112-2749063-2058610"):
            assert headers["x-amz-access-token"] == "RDT"
            return 200, {}, json.dumps(
                {
                    "payload": {
                        "AmazonOrderId": "112-2749063-2058610",
                        "OrderTotal": {
                            "Amount": "207.21",
                            "CurrencyCode": "USD",
                        },
                        "BuyerInfo": {"BuyerName": "API Buyer"},
                        "ShippingAddress": {
                            "Name": "API Buyer",
                            "CountryCode": "US",
                            "StateOrRegion": "CA",
                            "City": "Los Angeles",
                            "AddressLine1": "123 Main Street",
                            "PostalCode": "90012-1234",
                        },
                    }
                }
            ).encode()
        raise AssertionError(url)

    client = AmazonOrderQuantityClient(
        AmazonOrderQuantityConfig(
            refresh_token="refresh",
            client_id="client",
            client_secret="secret",
            endpoint="https://sellingpartnerapi-na.amazon.com",
        ),
        transport=fake_transport,
    )

    result = client.get_order_summary_sync("112-2749063-2058610")

    assert result.status == AMAZON_ORDER_SUMMARY_RESOLVED
    assert result.total_complete is True
    assert result.order_total == "207.21"
    assert result.order_currency == "USD"
    assert result.recipient_name == "API Buyer"
    assert result.postal_code == "90012-1234"
    assert "Los Angeles" in result.shipping_address_text
    rdt_body = json.loads(calls[1]["body"])
    assert rdt_body["restrictedResources"][0] == {
        "method": "GET",
        "path": "/orders/v0/orders/112-2749063-2058610",
        "dataElements": ["buyerInfo", "shippingAddress"],
    }


def test_amazon_order_quantity_client_reports_no_match():
    """验证Amazon 订单数量查询中的Amazon订单数量客户端报告无匹配场景。"""
    def fake_transport(method, url, headers, body, timeout):
        """模拟请求传输行为，隔离测试中的外部依赖。"""
        if url == "https://api.amazon.com/auth/o2/token":
            return 200, {}, json.dumps({"access_token": "LWA", "expires_in": 3600}).encode()
        if url.endswith("/tokens/2021-03-01/restrictedDataToken"):
            return 200, {}, json.dumps({"restrictedDataToken": "RDT", "expiresIn": 3600}).encode()
        return 200, {}, json.dumps({"payload": {"OrderItems": [{"ASIN": "B000000000", "QuantityOrdered": 1}]}}).encode()

    client = AmazonOrderQuantityClient(
        AmazonOrderQuantityConfig(refresh_token="r", client_id="c", client_secret="s"),
        transport=fake_transport,
    )

    result = client.get_order_item_quantity_sync("112-5663586-1765001", "B0CQLN5GNL")

    assert result.status == AMAZON_QUANTITY_NO_MATCH
    assert result.quantity is None
