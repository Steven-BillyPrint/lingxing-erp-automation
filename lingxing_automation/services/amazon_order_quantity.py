from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..config import (
    ConfigurationSource,
    get_configuration_value,
    parse_env_bool,
    read_lingxing_env,
)
from ..services.custom_attachment_downloader import normalize_item_match_text

AMAZON_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
DEFAULT_SP_API_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
SANDBOX_SP_API_ENDPOINT = "https://sandbox.sellingpartnerapi-na.amazon.com"
ORDER_ITEMS_RDT_DATA_ELEMENTS = ["buyerInfo"]

AMAZON_QUANTITY_RESOLVED = "amazon_quantity_resolved"
AMAZON_QUANTITY_CONFIG_MISSING = "amazon_quantity_config_missing"
AMAZON_QUANTITY_AUTH_ERROR = "amazon_quantity_auth_error"
AMAZON_QUANTITY_RDT_ERROR = "amazon_quantity_rdt_error"
AMAZON_QUANTITY_ORDER_ITEMS_ERROR = "amazon_quantity_order_items_error"
AMAZON_QUANTITY_NO_MATCH = "amazon_quantity_no_match"
AMAZON_QUANTITY_INVALID_RESPONSE = "amazon_quantity_invalid_response"

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]


@dataclass
class AmazonOrderQuantityResult:
    """Amazon Orders API 数量读取结果。日志中只保留排查字段，绝不输出 token/RDT。"""

    status: str
    platform_order_no: str
    asin: str | None = None
    sku: str | None = None
    quantity: int | None = None
    error: str | None = None
    endpoint: str | None = None
    rdt_required: bool = True
    rdt_resource_path: str | None = None
    matched_items: list[dict[str, Any]] = field(default_factory=list)
    order_items: list[dict[str, Any]] = field(default_factory=list)
    item_count: int = 0

    @property
    def ok(self) -> bool:
        """判断当前结果是否成功。"""
        return self.status == AMAZON_QUANTITY_RESOLVED and bool(self.quantity and self.quantity > 0)

    def to_log_dict(self) -> dict[str, Any]:
        """将当前对象转换为日志字典，便于批量流程记录和排查。"""
        return {
            "amazon_quantity_status": self.status,
            "amazon_quantity": self.quantity,
            "amazon_quantity_error": self.error,
            "amazon_quantity_order_id": self.platform_order_no,
            "amazon_quantity_asin": self.asin,
            "amazon_quantity_sku": self.sku,
            "amazon_quantity_item_count": self.item_count,
            "amazon_quantity_matched_items": self.matched_items,
            "amazon_order_items": self.order_items,
            "amazon_quantity_endpoint": self.endpoint,
            "amazon_quantity_rdt_required": self.rdt_required,
            "amazon_quantity_rdt_resource_path": self.rdt_resource_path,
        }


@dataclass
class AmazonOrderQuantityConfig:
    refresh_token: str
    client_id: str
    client_secret: str
    endpoint: str = DEFAULT_SP_API_ENDPOINT
    timeout_sec: float = 25

    @classmethod
    def from_env(cls, source: ConfigurationSource) -> "AmazonOrderQuantityConfig | None":
        """从环境变量创建当前配置对象。"""
        values = read_lingxing_env(source)
        refresh_token = get_configuration_value(
            values,
            "amazon.refresh_token",
            "AMAZON_REFRESH_TOKEN",
        )
        client_id = get_configuration_value(
            values,
            "amazon.lwa_client_id",
            "AMAZON_LWA_CLIENT_ID",
            "AMAZON_CLIENT_ID",
        )
        client_secret = get_configuration_value(
            values,
            "amazon.lwa_client_secret",
            "AMAZON_LWA_CLIENT_SECRET",
            "AMAZON_CLIENT_SECRET",
        )
        if not (refresh_token and client_id and client_secret):
            return None
        endpoint = get_configuration_value(
            values,
            "amazon.sp_api_endpoint",
            "AMAZON_SP_API_ENDPOINT",
        )
        if not endpoint:
            sandbox = get_configuration_value(
                values,
                "amazon.sp_api_sandbox",
                "AMAZON_SP_API_SANDBOX",
            )
            endpoint = (
                SANDBOX_SP_API_ENDPOINT
                if parse_env_bool(sandbox, default=False)
                else DEFAULT_SP_API_ENDPOINT
            )
        return cls(
            refresh_token=str(refresh_token),
            client_id=str(client_id),
            client_secret=str(client_secret),
            endpoint=str(endpoint).rstrip("/"),
        )


class AmazonOrderQuantityClient:
    """用 Amazon Orders API 读取订单商品购买数量。

    业务要求：商品数量不能再从领星 DOM 兜底读取，因为页面上的“共N”附件数量
    和商品“×N”数量很容易混淆。这里固定使用 LWA token -> RDT -> getOrderItems，
    再从当前商品对应的 QuantityOrdered 计算数量。
    """

    def __init__(
        self,
        config: AmazonOrderQuantityConfig | None,
        *,
        transport: Transport | None = None,
    ) -> None:
        """初始化Amazon订单数量客户端的运行状态。"""
        self.config = config
        self._transport = transport
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._rdt_cache: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_env(cls, source: ConfigurationSource) -> "AmazonOrderQuantityClient":
        """从环境变量创建当前配置对象。"""
        return cls(AmazonOrderQuantityConfig.from_env(source))

    async def get_order_items(self, platform_order_no: str) -> AmazonOrderQuantityResult:
        """读取整单 Amazon OrderItems。

        多商品订单必须保留 Amazon 返回的每个 OrderItem，尤其是同 ASIN 多行时的
        OrderItemId 和 QuantityOrdered；这里不做 ASIN/SKU 汇总。
        """
        import asyncio

        return await asyncio.to_thread(self.get_order_items_sync, platform_order_no)

    def get_order_items_sync(self, platform_order_no: str) -> AmazonOrderQuantityResult:
        """获取订单行 同步。"""
        if not self.config:
            return AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_CONFIG_MISSING,
                platform_order_no=platform_order_no,
                error="缺少 AMAZON_REFRESH_TOKEN / AMAZON_LWA_CLIENT_ID / AMAZON_LWA_CLIENT_SECRET，无法读取 Amazon OrderItems。",
            )
        path = _order_items_path(platform_order_no)
        base = AmazonOrderQuantityResult(
            status=AMAZON_QUANTITY_ORDER_ITEMS_ERROR,
            platform_order_no=platform_order_no,
            endpoint=self.config.endpoint,
            rdt_resource_path=path,
        )
        try:
            access_token = self._get_lwa_access_token()
            rdt = self._get_restricted_data_token(access_token, path)
            payload = self._get_order_items_payload(rdt, path)
        except Exception as exc:
            base.status = AMAZON_QUANTITY_ORDER_ITEMS_ERROR
            base.error = _safe_error(exc)
            return base
        order_items = payload.get("OrderItems") if isinstance(payload, dict) else None
        if not isinstance(order_items, list):
            base.status = AMAZON_QUANTITY_INVALID_RESPONSE
            base.error = "Amazon Orders API 响应中没有 payload.OrderItems 数组。"
            return base
        base.item_count = len(order_items)
        base.order_items = [_item_for_log(item) for item in order_items]
        base.quantity = sum(_quantity_ordered(item) for item in order_items) or None
        base.status = AMAZON_QUANTITY_RESOLVED
        return base

    async def get_order_item_quantity(self, platform_order_no: str, asin: str | None, sku: str | None = None) -> AmazonOrderQuantityResult:
        # 当前实现是轻量 HTTP 调用；放到线程里避免阻塞 Playwright 的异步流程。
        """获取订单行数量。"""
        import asyncio

        return await asyncio.to_thread(self.get_order_item_quantity_sync, platform_order_no, asin, sku)

    def get_order_item_quantity_sync(self, platform_order_no: str, asin: str | None, sku: str | None = None) -> AmazonOrderQuantityResult:
        """获取订单行 数量同步。"""
        if not self.config:
            return AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_CONFIG_MISSING,
                platform_order_no=platform_order_no,
                asin=asin,
                sku=sku,
                error="缺少 AMAZON_REFRESH_TOKEN / AMAZON_LWA_CLIENT_ID / AMAZON_LWA_CLIENT_SECRET，无法读取 Amazon QuantityOrdered。",
            )
        path = _order_items_path(platform_order_no)
        base = AmazonOrderQuantityResult(
            status=AMAZON_QUANTITY_ORDER_ITEMS_ERROR,
            platform_order_no=platform_order_no,
            asin=asin,
            sku=sku,
            endpoint=self.config.endpoint,
            rdt_resource_path=path,
        )
        try:
            access_token = self._get_lwa_access_token()
        except Exception as exc:
            base.status = AMAZON_QUANTITY_AUTH_ERROR
            base.error = _safe_error(exc)
            return base
        try:
            rdt = self._get_restricted_data_token(access_token, path)
        except Exception as exc:
            base.status = AMAZON_QUANTITY_RDT_ERROR
            base.error = _safe_error(exc)
            return base
        try:
            payload = self._get_order_items_payload(rdt, path)
        except Exception as exc:
            base.status = AMAZON_QUANTITY_ORDER_ITEMS_ERROR
            base.error = _safe_error(exc)
            return base
        order_items = payload.get("OrderItems") if isinstance(payload, dict) else None
        if not isinstance(order_items, list):
            base.status = AMAZON_QUANTITY_INVALID_RESPONSE
            base.error = "Amazon Orders API 响应中没有 payload.OrderItems 数组。"
            return base
        selected = select_order_item_quantity(order_items, asin=asin, sku=sku)
        base.item_count = len(order_items)
        base.order_items = [_item_for_log(item) for item in order_items]
        base.matched_items = selected["matched_items"]
        base.quantity = selected["quantity"]
        if selected["quantity"]:
            base.status = AMAZON_QUANTITY_RESOLVED
            return base
        base.status = AMAZON_QUANTITY_NO_MATCH
        base.error = "Amazon Orders API 返回了订单商品，但没有匹配到当前 ASIN/SKU 的 QuantityOrdered。"
        return base

    def _get_lwa_access_token(self) -> str:
        """获取LWA访问令牌。"""
        now = time.time()
        if self._access_token and now < self._access_token_expires_at - 60:
            return self._access_token
        assert self.config is not None
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.config.refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            }
        ).encode("utf-8")
        data = self._request_json(
            "POST",
            AMAZON_LWA_TOKEN_URL,
            headers={"content-type": "application/x-www-form-urlencoded"},
            body=body,
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("LWA token 响应缺少 access_token。")
        try:
            expires_in = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self._access_token = token
        self._access_token_expires_at = now + max(expires_in, 60)
        return token

    def _get_restricted_data_token(self, access_token: str, path: str) -> str:
        """获取受限数据令牌。"""
        now = time.time()
        cache_key = f"GET {path} {'|'.join(ORDER_ITEMS_RDT_DATA_ELEMENTS)}"
        cached = self._rdt_cache.get(cache_key)
        if cached and now < cached[1] - 60:
            return cached[0]
        assert self.config is not None
        # getOrderItems 在当前账号流程中需要 RDT；RDT 只授权对应 path，
        # 所以 path 必须精确到订单号，避免用错权限范围。
        body = json.dumps(
            {
                "restrictedResources": [
                    {
                        "method": "GET",
                        "path": path,
                        "dataElements": ORDER_ITEMS_RDT_DATA_ELEMENTS,
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        data = self._request_json(
            "POST",
            f"{self.config.endpoint}/tokens/2021-03-01/restrictedDataToken",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-amz-access-token": access_token,
            },
            body=body,
        )
        token = str(data.get("restrictedDataToken") or "")
        if not token:
            raise RuntimeError("RDT 响应缺少 restrictedDataToken。")
        try:
            expires_in = int(data.get("expiresIn") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self._rdt_cache[cache_key] = (token, now + max(expires_in, 60))
        return token

    def _get_order_items_payload(self, rdt: str, path: str) -> dict[str, Any]:
        """获取订单条目载荷。"""
        assert self.config is not None
        all_items: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            suffix = f"?NextToken={quote(next_token, safe='')}" if next_token else ""
            data = self._request_json(
                "GET",
                f"{self.config.endpoint}{path}{suffix}",
                headers={
                    "accept": "application/json",
                    "x-amz-access-token": rdt,
                },
                body=None,
            )
            payload = data.get("payload") if isinstance(data, dict) else None
            if not isinstance(payload, dict):
                return {}
            page_items = payload.get("OrderItems") or []
            if isinstance(page_items, list):
                all_items.extend(item for item in page_items if isinstance(item, dict))
            next_token = payload.get("NextToken")
            if not next_token:
                return {"OrderItems": all_items, "AmazonOrderId": payload.get("AmazonOrderId")}

    def _request_json(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
        """处理请求JSON相关逻辑，并返回后续流程所需结果。"""
        assert self.config is not None
        request_headers = {"user-agent": "lingxing-erp-automation/1.0", **headers}
        if self._transport:
            status, _headers, content = self._transport(method, url, request_headers, body, self.config.timeout_sec)
            if status >= 400:
                raise RuntimeError(f"HTTP {status}: {content.decode('utf-8', errors='replace')[:500]}")
            return json.loads(content.decode("utf-8") or "{}")
        req = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(req, timeout=self.config.timeout_sec) as response:  # noqa: S310 - URL 来自固定 Amazon endpoint 或 .env 显式配置。
                content = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc
        return json.loads(content.decode("utf-8") or "{}")


def _order_items_path(platform_order_no: str) -> str:
    """处理订单行 路径相关逻辑，并返回后续流程所需结果。"""
    return f"/orders/v0/orders/{quote(str(platform_order_no), safe='')}/orderItems"


def _normalized(value: str | None) -> str:
    """处理规范化相关逻辑，并返回后续流程所需结果。"""
    return (normalize_item_match_text(value) or "").strip().lower()


def select_order_item_quantity(order_items: list[dict[str, Any]], *, asin: str | None, sku: str | None = None) -> dict[str, Any]:
    """从 getOrderItems 返回值中匹配当前商品并汇总 QuantityOrdered。

    同一个 ASIN/SKU 可能在 Amazon 响应里拆成多条 OrderItem，业务上需要把这些
    QuantityOrdered 相加，作为文件夹命名的购买数量。
    """

    target_asin = _normalized(asin).upper()
    target_sku = _normalized(sku)
    asin_matches: list[dict[str, Any]] = []
    sku_matches: list[dict[str, Any]] = []
    exact_matches: list[dict[str, Any]] = []
    for item in order_items:
        item_asin = _normalized(str(item.get("ASIN") or "")).upper()
        item_sku = _normalized(str(item.get("SellerSKU") or ""))
        asin_ok = bool(target_asin and item_asin == target_asin)
        sku_ok = bool(target_sku and item_sku and (item_sku == target_sku or target_sku in item_sku or item_sku in target_sku))
        if asin_ok:
            asin_matches.append(item)
        if sku_ok:
            sku_matches.append(item)
        if asin_ok and (not target_sku or sku_ok):
            exact_matches.append(item)
    matches = exact_matches or asin_matches or sku_matches
    quantity = sum(_quantity_ordered(item) for item in matches)
    return {
        "quantity": quantity or None,
        "matched_items": [_item_for_log(item) for item in matches],
    }


def _quantity_ordered(item: dict[str, Any]) -> int:
    """读取 Amazon 订单行中的订购数量，并兼容不同字段命名。"""
    try:
        value = int(item.get("QuantityOrdered") or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _item_for_log(item: dict[str, Any]) -> dict[str, Any]:
    """整理 Amazon 订单行的日志字段，避免输出过大的原始数据。"""
    return {
        "asin": item.get("ASIN"),
        "seller_sku": item.get("SellerSKU"),
        "title": item.get("Title"),
        "quantity_ordered": item.get("QuantityOrdered"),
        "order_item_id": item.get("OrderItemId"),
    }


def _safe_error(exc: Exception) -> str:
    # Amazon token/RDT 属敏感凭据，异常日志只保留 HTTP 状态和短错误体，不输出请求头。
    """处理安全错误相关逻辑，并返回后续流程所需结果。"""
    return str(exc).replace("\n", " ")[:800] or exc.__class__.__name__
