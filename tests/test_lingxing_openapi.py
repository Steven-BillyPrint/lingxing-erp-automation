from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from erp_automation.integrations.lingxing import (
    ENDPOINTS,
    FileInterProcessLock,
    IssuedToken,
    LingxingAPIError,
    LingxingAmbiguousWriteError,
    LingxingAuthError,
    LingxingCredentials,
    LingxingOpenAPIClient,
    LingxingSigner,
    LingxingTokenEndpoint,
    MemoryTokenStore,
    NullInterProcessLock,
    ResponseKind,
    StaticCredentialProvider,
    TokenBundle,
    TokenManager,
    canonicalize_params,
)


APP_ID = "1234567890abcdef"
APP_SECRET = "unit-test-app-secret"
ACCESS_TOKEN = "unit-test-access-token"
REFRESH_TOKEN = "unit-test-refresh-token"


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: object | None = None,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        if content is None and payload is not None:
            content = json.dumps(payload).encode("utf-8")
        self.content = content or b""
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHTTPClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.outcomes:
            raise AssertionError(f"unexpected HTTP request: {method} {url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


class NeverTokenEndpoint:
    async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken:
        raise AssertionError("valid seeded token should not be reissued")

    async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken:
        raise AssertionError("valid seeded token should not be refreshed")


@dataclass
class MutableClock:
    value: float

    def __call__(self) -> float:
        return self.value


def _seeded_token(clock: MutableClock, *, access_token: str = ACCESS_TOKEN) -> TokenBundle:
    return TokenBundle(
        access_token=access_token,
        refresh_token=REFRESH_TOKEN,
        issued_at=clock.value,
        expires_at=clock.value + 3600,
        refresh_expires_at=clock.value + 7200,
        generation=1,
    )


def _client_with_seeded_token(
    http: FakeHTTPClient,
    *,
    clock: MutableClock | None = None,
    max_read_retries: int = 2,
    retry_base_delay: float = 0.01,
    sleeper=asyncio.sleep,
) -> LingxingOpenAPIClient:
    clock = clock or MutableClock(1_700_000_000)
    store = MemoryTokenStore(_seeded_token(clock))
    manager = TokenManager(
        StaticCredentialProvider(LingxingCredentials(APP_ID, APP_SECRET)),
        store,
        NullInterProcessLock(),
        NeverTokenEndpoint(),
        refresh_skew_seconds=60,
        clock=clock,
    )
    return LingxingOpenAPIClient(
        http,
        manager,
        app_id=APP_ID,
        clock=clock,
        max_read_retries=max_read_retries,
        retry_base_delay=retry_base_delay,
        sleeper=sleeper,
    )


def test_signature_matches_known_vector_and_null_is_not_empty() -> None:
    params = {
        "z": "",
        "n": None,
        "b": 2,
        "a": {"z": 1, "a": 2},
        "items": [{"b": 2, "a": 1}],
        "false": False,
        "empty_list": [],
    }

    signature = LingxingSigner(APP_ID).sign(params)

    assert signature.canonical_query == (
        'a={"a":2,"z":1}&b=2&empty_list=[]&false=false&'
        'items=[{"a":1,"b":2}]&n=null'
    )
    assert signature.md5_upper == "F50D8DD54BE1B7FE1D54A70889DBE800"
    assert signature.raw == "OK5dei60ZS0Y5i8WvIZwSji7Vo70S7HnYtf+txhG7CMC3sVlLAIV+Tr2nwCfD21Y"
    assert signature.url_encoded == (
        "OK5dei60ZS0Y5i8WvIZwSji7Vo70S7HnYtf%2BtxhG7CMC3sVlLAIV%2BTr2nwCfD21Y"
    )
    assert "z=" not in signature.canonical_query
    assert "n=null" in signature.canonical_query


def test_ascii_key_sorting_is_case_sensitive() -> None:
    assert canonicalize_params({"b": 1, "a": 2, "A": 3}) == "A=3&a=2&b=1"


def test_credentials_and_tokens_do_not_reveal_secrets_in_repr() -> None:
    credentials = LingxingCredentials(APP_ID, APP_SECRET)
    token = TokenBundle(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        issued_at=1,
        expires_at=2,
        refresh_expires_at=3,
    )

    assert APP_SECRET not in repr(credentials)
    assert ACCESS_TOKEN not in repr(token)
    assert REFRESH_TOKEN not in repr(token)


def test_official_multipart_token_issue_and_one_time_refresh_rotation() -> None:
    async def run() -> None:
        clock = MutableClock(1000)
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": "200",
                        "msg": "OK",
                        "data": {
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "expires_in": 7199,
                        },
                    }
                ),
                FakeResponse(
                    payload={
                        "code": 200,
                        "msg": "OK",
                        "data": {
                            "access_token": "access-2",
                            "refresh_token": "refresh-2",
                            "expires_in": "7199",
                        },
                    }
                ),
            ]
        )
        store = MemoryTokenStore()
        endpoint = LingxingTokenEndpoint(http)
        manager = TokenManager(
            StaticCredentialProvider(LingxingCredentials(APP_ID, APP_SECRET)),
            store,
            NullInterProcessLock(),
            endpoint,
            refresh_skew_seconds=600,
            clock=clock,
        )

        first = await manager.get_token()
        assert first.access_token == "access-1"
        assert first.refresh_token == "refresh-1"
        first_files = http.requests[0]["files"]
        assert first_files == {
            "appId": (None, APP_ID),
            "appSecret": (None, APP_SECRET),
        }
        assert str(http.requests[0]["url"]).endswith("/api/auth-server/oauth/access-token")

        clock.value += 6700
        second = await manager.get_token()
        assert second.access_token == "access-2"
        assert second.refresh_token == "refresh-2"
        assert second.generation == 2
        assert http.requests[1]["files"] == {
            "appId": (None, APP_ID),
            "refreshToken": (None, "refresh-1"),
        }
        assert str(http.requests[1]["url"]).endswith("/api/auth-server/oauth/refresh")

    asyncio.run(run())


def test_token_endpoint_redacts_secret_from_server_error() -> None:
    async def run() -> None:
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": "2001002",
                        "msg": f"bad appSecret={APP_SECRET}",
                        "data": None,
                        "request_id": "auth-request",
                    }
                )
            ]
        )
        endpoint = LingxingTokenEndpoint(http)
        with pytest.raises(LingxingAuthError) as captured:
            await endpoint.issue_token(LingxingCredentials(APP_ID, APP_SECRET))
        assert captured.value.code == "2001002"
        assert captured.value.request_id == "auth-request"
        assert APP_SECRET not in str(captured.value)

    asyncio.run(run())


def test_concurrent_callers_share_one_token_issue() -> None:
    class CountingEndpoint:
        def __init__(self) -> None:
            self.issue_count = 0

        async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken:
            self.issue_count += 1
            await asyncio.sleep(0)
            return IssuedToken("access", "refresh", 7199)

        async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken:
            raise AssertionError("no refresh expected")

    async def run() -> None:
        endpoint = CountingEndpoint()
        manager = TokenManager(
            StaticCredentialProvider(LingxingCredentials(APP_ID, APP_SECRET)),
            MemoryTokenStore(),
            NullInterProcessLock(),
            endpoint,
            clock=MutableClock(1000),
        )
        first, second = await asyncio.gather(manager.get_token(), manager.get_token())
        assert first.access_token == second.access_token == "access"
        assert endpoint.issue_count == 1

    asyncio.run(run())


def test_file_lock_implements_async_cross_process_contract(tmp_path) -> None:
    async def run() -> None:
        lock_path = tmp_path / "lingxing-token.lock"
        async with FileInterProcessLock(lock_path, timeout=1):
            assert lock_path.exists()

    asyncio.run(run())


def test_signed_read_request_keeps_business_body_and_request_id() -> None:
    async def run() -> None:
        clock = MutableClock(1_700_000_123)
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": 0,
                        "message": "success",
                        "request_id": "orders-request",
                        "response_time": "2026-07-14 12:00:00",
                        "data": {"list": []},
                    }
                )
            ]
        )
        client = _client_with_seeded_token(http, clock=clock, max_read_retries=0)

        response = await client.list_orders(
            offset=0,
            length=500,
            order_status=4,
            platform_code=[10001],
        )

        assert response.code == "0"
        assert response.request_id == "orders-request"
        request = http.requests[0]
        assert request["method"] == "POST"
        assert str(request["url"]).endswith(ENDPOINTS["list_orders"].path)
        body = json.loads(bytes(request["content"]).decode("utf-8"))
        assert body == {
            "length": 500,
            "offset": 0,
            "order_status": 4,
            "platform_code": [10001],
        }
        params = request["params"]
        assert params["access_token"] == ACCESS_TOKEN
        assert params["app_key"] == APP_ID
        assert params["timestamp"] == str(int(clock.value))
        expected = LingxingSigner(APP_ID).sign(
            {
                **body,
                "access_token": ACCESS_TOKEN,
                "app_key": APP_ID,
                "timestamp": str(int(clock.value)),
            }
        )
        assert params["sign"] == expected.raw
        assert expected.url_encoded != expected.raw

    asyncio.run(run())


def test_read_retries_retryable_http_status_with_backoff() -> None:
    async def run() -> None:
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        http = FakeHTTPClient(
            [
                FakeResponse(status_code=503),
                FakeResponse(payload={"code": 0, "message": "success", "data": []}),
            ]
        )
        client = _client_with_seeded_token(
            http,
            max_read_retries=2,
            retry_base_delay=0.125,
            sleeper=fake_sleep,
        )

        response = await client.get_fbm_order_detail("103000000000000001")
        assert response.code == "0"
        assert len(http.requests) == 2
        assert delays == [0.125]

    asyncio.run(run())


def test_read_retries_documented_rate_limit_code() -> None:
    async def run() -> None:
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={"code": 3001008, "message": "requests too frequently"}
                ),
                FakeResponse(payload={"code": 0, "message": "success", "data": []}),
            ]
        )
        client = _client_with_seeded_token(
            http,
            max_read_retries=1,
            retry_base_delay=0,
            sleeper=fake_sleep,
        )
        response = await client.list_orders()
        assert response.code == "0"
        assert len(http.requests) == 2
        assert delays == [0]

    asyncio.run(run())


def test_write_transport_failure_is_not_retried_and_is_marked_ambiguous() -> None:
    async def run() -> None:
        http = FakeHTTPClient([TimeoutError(f"request URL contained {ACCESS_TOKEN}")])
        client = _client_with_seeded_token(http, max_read_retries=5)

        with pytest.raises(LingxingAmbiguousWriteError) as captured:
            await client.update_orders(
                [
                    {
                        "global_order_no": 103000000000000001,
                        "address_info": {"receiver_tel": "5551234567"},
                        "order_item_list": [],
                    }
                ]
            )

        assert len(http.requests) == 1
        assert ACCESS_TOKEN not in str(captured.value)
        assert captured.value.operation == "update_order"

    asyncio.run(run())


def test_write_partial_success_raises_rich_api_error_with_request_id() -> None:
    async def run() -> None:
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": 10001,
                        "message": "partial",
                        "request_id": "partial-request",
                        "data": {"error_details": [{"global_order_no": "1"}]},
                    }
                )
            ]
        )
        client = _client_with_seeded_token(http)

        with pytest.raises(LingxingAPIError) as captured:
            await client.update_orders(
                [{"global_order_no": 1, "order_item_list": []}]
            )
        assert captured.value.code == "10001"
        assert captured.value.request_id == "partial-request"
        assert captured.value.payload["data"]["error_details"]
        assert len(http.requests) == 1

    asyncio.run(run())


def test_write_auth_rejection_gets_one_safe_token_recovery_retry() -> None:
    async def run() -> None:
        clock = MutableClock(1000)
        old = _seeded_token(clock, access_token="old-access")
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": 2001005,
                        "message": "access token not match",
                        "request_id": "rejected-write",
                    }
                ),
                FakeResponse(
                    payload={
                        "code": 200,
                        "msg": "OK",
                        "data": {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 7199,
                        },
                    }
                ),
                FakeResponse(
                    payload={
                        "code": 10002,
                        "message": "updated",
                        "request_id": "successful-write",
                        "data": {"error_details": []},
                    }
                ),
            ]
        )
        store = MemoryTokenStore(old)
        provider = StaticCredentialProvider(LingxingCredentials(APP_ID, APP_SECRET))
        manager = TokenManager(
            provider,
            store,
            NullInterProcessLock(),
            LingxingTokenEndpoint(http),
            refresh_skew_seconds=60,
            clock=clock,
        )
        client = LingxingOpenAPIClient(
            http,
            manager,
            app_id=APP_ID,
            clock=clock,
            max_read_retries=0,
        )

        response = await client.set_order_remarks(
            [
                {
                    "global_order_no": "103000000000000001",
                    "remark": "7.15\u53d1\u8bf4\u660e\u4e66",
                    "remark_is_append": False,
                }
            ]
        )

        assert response.code == "10002"
        write_requests = [
            request
            for request in http.requests
            if str(request["url"]).endswith(ENDPOINTS["set_order_remark"].path)
        ]
        assert len(write_requests) == 2
        assert write_requests[0]["params"]["access_token"] == "old-access"
        assert write_requests[1]["params"]["access_token"] == "new-access"
        refresh_requests = [
            request
            for request in http.requests
            if str(request["url"]).endswith("/api/auth-server/oauth/refresh")
        ]
        assert len(refresh_requests) == 1
        assert refresh_requests[0]["files"]["refreshToken"] == (None, REFRESH_TOKEN)

    asyncio.run(run())


def test_split_order_preserves_documented_nested_package_groups() -> None:
    async def run() -> None:
        http = FakeHTTPClient(
            [FakeResponse(payload={"code": 0, "message": "success", "data": {"num": 1}})]
        )
        client = _client_with_seeded_token(http)
        groups = [
            [{"item_id": "item-original", "quantity": 1}],
            [{"item_id": "item-new-package", "quantity": 2}],
        ]

        await client.split_order(
            split_mod=1,
            global_order_no="103000000000000001",
            order_item=groups,
        )

        body = json.loads(bytes(http.requests[0]["content"]).decode("utf-8"))
        assert body["order_item"] == groups
        assert body["global_order_no"] == "103000000000000001"

    asyncio.run(run())


def test_attachment_download_is_read_retryable_and_sanitizes_filename() -> None:
    async def run() -> None:
        http = FakeHTTPClient(
            [
                FakeResponse(status_code=503),
                FakeResponse(
                    content=b"PK\x03\x04zip-data",
                    headers={
                        "content-type": "application/octet-stream",
                        "content-disposition": 'attachment; filename="../../order.zip"',
                        "x-request-id": "attachment-request",
                    },
                ),
            ]
        )
        client = _client_with_seeded_token(
            http,
            max_read_retries=1,
            retry_base_delay=0,
            sleeper=lambda _delay: asyncio.sleep(0),
        )

        response = await client.download_attachment("103450663351412224")
        assert response.content == b"PK\x03\x04zip-data"
        assert response.filename == "order.zip"
        assert response.request_id == "attachment-request"
        body = json.loads(bytes(http.requests[-1]["content"]).decode("utf-8"))
        assert body == {"file_id": "103450663351412224"}
        assert len(http.requests) == 2

    asyncio.run(run())


def test_custom_attachment_uses_dedicated_documented_endpoint() -> None:
    async def run() -> None:
        http = FakeHTTPClient(
            [
                FakeResponse(
                    payload={
                        "code": 0,
                        "message": "操作成功",
                        "request_id": "custom-request",
                        "data": [
                            {
                                "file_name": "custom.zip",
                                "mime_type": "application/zip",
                                "content": "UEsDBGJhc2U2NA==",
                            }
                        ],
                    }
                )
            ]
        )
        client = _client_with_seeded_token(http)

        response = await client.download_custom_attachment("custom-file-id")

        assert response.data[0]["file_name"] == "custom.zip"
        assert str(http.requests[0]["url"]).endswith(
            "/erp/sc/routing/customized/file/download"
        )
        assert ENDPOINTS["download_custom_attachment"].path == (
            "/erp/sc/routing/customized/file/download"
        )
        assert ENDPOINTS["download_custom_attachment"].response_kind is ResponseKind.JSON

    asyncio.run(run())
