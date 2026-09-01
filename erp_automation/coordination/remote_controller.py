"""Desktop-side proxy for the authoritative shared controller."""

from __future__ import annotations

import base64
import os
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from erp_automation.configuration import atomic_write_bytes, backup_path_for
from erp_automation.contracts.controller import ControlResult
from erp_automation.contracts.models import (
    Capability,
    CustomOrderPage,
    DESKTOP_INSTANCE_ID_PAYLOAD_KEY,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSnapshot,
    LINGXING_BROWSER_LOGIN_TRIGGER,
    LogPage,
    ShipmentPage,
    TaskArea,
    TaskCommand,
    task_requires_visible_browser,
)
from .codec import (
    decode_control_result,
    decode_custom_order_page,
    decode_interactions,
    decode_log_page,
    decode_shipment_page,
    decode_snapshot,
    to_jsonable,
)
from .local_browser import (
    ALIBABA_QUOTE_URL,
    ALIBABA_SCM_HOME_URL,
    LINGXING_ORDER_MANAGEMENT_URL,
    LocalBrowserUnavailable,
    LocalChromeHost,
)
from .service import (
    MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES,
    MUTATION_METHODS,
    READ_METHODS,
    RPC_METHODS,
)


_CLIENT_VERSION_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")

_NOTIFICATION_SEND_TIMEOUT_PER_ITEM_SECONDS = 105.0
_NOTIFICATION_SEND_TIMEOUT_OVERHEAD_SECONDS = 30.0
_MAX_NOTIFICATION_SEND_TIMEOUT_SECONDS = 60.0 * 60.0
_NOTIFICATION_READ_TIMEOUT_SECONDS = 30.0
_TASK_BATCH_READ_TIMEOUT_OVERHEAD_SECONDS = 5.0
_TASK_BATCH_READ_TIMEOUT_PER_ITEM_SECONDS = 0.25
_MAX_TASK_BATCH_READ_TIMEOUT_SECONDS = 60.0
_CONCURRENT_READ_METHODS = frozenset(
    {
        "list_custom_order_page",
        "list_shipment_page",
        "list_shipment_notifications",
        "get_shipment_notification_details",
        "get_shipment_notification_review_previews",
    }
)


def _filter_snapshot_for_operator(
    snapshot: DesktopSnapshot,
    operator_email: str,
) -> DesktopSnapshot:
    """Drop foreign task content received from an older coordination server."""

    normalized_email = str(operator_email or "").strip().casefold()
    if not normalized_email:
        return snapshot
    snapshot.tasks = [
        task
        for task in snapshot.tasks
        if str(task.operator_email or "").strip().casefold()
        == normalized_email
    ]
    snapshot.today_tasks = [
        task
        for task in snapshot.today_tasks
        if str(task.operator_email or "").strip().casefold()
        == normalized_email
    ]
    visible_task_ids = {
        task.task_id for task in (*snapshot.tasks, *snapshot.today_tasks)
    }

    def log_is_private_to_operator(entry: Any) -> bool:
        entry_email = str(entry.operator_email or "").strip().casefold()
        if entry_email:
            return entry_email == normalized_email
        return bool(entry.task_id and entry.task_id in visible_task_ids)

    snapshot.logs = [
        entry
        for entry in snapshot.logs
        if log_is_private_to_operator(entry)
    ]
    return snapshot


class CoordinationConnectionError(RuntimeError):
    """The shared controller could not be reached or authenticated."""


class CoordinationAuthenticationRequired(CoordinationConnectionError):
    """The operator must explicitly renew the Cloudflare Access session."""


class CoordinationClientUpdateRequired(CoordinationConnectionError):
    """The running desktop client must leave safely and install a newer release."""

    def __init__(self, required_version: str = "") -> None:
        self.required_version = str(required_version or "").strip()
        super().__init__(
            "客户端必须更新到 "
            f"{self.required_version or '最新版本'} 后才能连接共享后台。"
        )


class RemoteBackgroundTaskController:
    """Implement the complete desktop controller protocol over authenticated HTTP."""

    snapshot_runs_in_background = True

    def __init__(
        self,
        server_url: str,
        *,
        token: str,
        ca_file: str | Path | None = None,
        display_name: str = "",
        timeout_seconds: float = 30.0,
        instance_id: str | None = None,
        client_version: str = "",
        browser_endpoint: str = "",
        browser_local_port: int = 0,
        browser_profile_dir: str | Path | None = None,
        logistics_browser_endpoint: str = "",
        logistics_browser_local_port: int = 0,
        logistics_browser_profile_dir: str | Path | None = None,
        strict_registration: bool = False,
        access_token: str = "",
        access_token_provider: Callable[[], str] | None = None,
        local_action_executor: Any | None = None,
    ) -> None:
        normalized_url = str(server_url or "").strip().rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ERP coordination server URL is invalid.")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not loopback:
            if os.environ.get("ERP_AUTOMATION_ALLOW_INSECURE_HTTP") != "1":
                raise ValueError(
                    "Remote coordination requires HTTPS unless an SSH loopback tunnel is used."
                )
        normalized_token = str(token or "").strip()
        if len(normalized_token) < 32:
            raise ValueError("ERP coordination token is missing or too short.")
        verify: bool | str = str(Path(ca_file).expanduser()) if ca_file else True
        self.instance_id = str(instance_id or uuid4().hex)
        self.display_name = (
            str(display_name or "").strip()
            or f"{os.environ.get('USERNAME') or 'operator'}@{socket.gethostname()}"
        )[:200]
        self.client_version = str(
            client_version
            or os.environ.get("ERP_AUTOMATION_CLIENT_VERSION")
            or ""
        ).strip()
        self._metadata_lock = threading.RLock()
        self.operator_name = ""
        self.operator_email = ""
        self._access_token_provider = access_token_provider
        self._transport_health_provider: (
            Callable[[], Mapping[str, Any]] | None
        ) = None
        self._authentication_required = False
        self._authentication_error = ""
        self._local_pause_requested = False
        self._fail_safe_pause_confirmed = False
        self.instance_pause_supported = False
        normalized_access_token = str(access_token or "").strip()
        if normalized_access_token:
            if len(normalized_access_token) > 32 * 1024:
                raise ValueError("Cloudflare Access token is invalid.")
        self.browser_endpoint = str(browser_endpoint or "").strip().rstrip("/")
        self._browser_host = (
            LocalChromeHost(
                browser_local_port,
                browser_profile_dir
                or Path(os.environ.get("LOCALAPPDATA") or ".")
                / "LingxingERP"
                / "browser-profile",
            )
            if browser_local_port
            else None
        )
        self.local_browser_endpoint = (
            self._browser_host.endpoint
            if self._browser_host is not None
            else self.browser_endpoint
        )
        self.logistics_browser_endpoint = str(
            logistics_browser_endpoint or ""
        ).strip().rstrip("/")
        self._logistics_browser_host = (
            LocalChromeHost(
                logistics_browser_local_port,
                logistics_browser_profile_dir
                or Path(os.environ.get("LOCALAPPDATA") or ".")
                / "LingxingERP"
                / "logistics-browser-profile",
            )
            if logistics_browser_local_port
            else None
        )
        request_headers = {
            "Authorization": f"Bearer {normalized_token}",
            "Accept": "application/json",
            "User-Agent": "lingxing-erp-desktop-coordination/1",
            "X-ERP-Instance-ID": self.instance_id,
            "X-ERP-Client-Version": self.client_version,
        }
        if normalized_access_token:
            request_headers["Cf-Access-Token"] = normalized_access_token
        self._client = httpx.Client(
            base_url=normalized_url,
            headers=request_headers,
            timeout=max(3.0, float(timeout_seconds)),
            verify=verify,
        )
        self._timeout_seconds = max(3.0, float(timeout_seconds))
        self._lock = threading.RLock()
        self._page_cache_lock = threading.RLock()
        self._closed = False
        self._last_snapshot = DesktopSnapshot(
            backend_message="正在连接共享 ERP 后台…"
        )
        self._last_interactions: tuple[DesktopInteractionRequest, ...] = ()
        self._automatic_interactions: dict[str, DesktopInteractionRequest] = {}
        self._local_action_responses: dict[str, DesktopInteractionResponse] = {}
        self._local_action_inflight: set[str] = set()
        if local_action_executor is None and self.browser_endpoint:
            from .local_alibaba_order import LocalAlibabaOrderActionExecutor

            local_action_executor = LocalAlibabaOrderActionExecutor(
                self.local_browser_endpoint
            )
        self._local_action_executor = local_action_executor
        self._local_action_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="erp-local-browser-action",
        )
        self._snapshot_revision: int | None = None
        self._startup_snapshot_pending = False
        self._prefetched_custom_order_pages: dict[
            tuple[object, ...], CustomOrderPage
        ] = {}
        self._prefetched_shipment_pages: dict[
            tuple[object, ...], ShipmentPage
        ] = {}
        self._revision = 0
        self._last_error = ""
        self._browser_cleanup_task_ids: set[str] = set()
        self._browser_close_pending = False
        self._logistics_browser_cleanup_task_ids: set[str] = set()
        self._logistics_browser_close_pending = False
        try:
            self._register_instance()
        except CoordinationConnectionError as exc:
            self._last_error = str(exc)
            if strict_registration:
                self._client.close()
                if self._browser_host is not None:
                    self._browser_host.close()
                if self._logistics_browser_host is not None:
                    self._logistics_browser_host.close()
                self._local_action_pool.shutdown(wait=False, cancel_futures=True)
                self._closed = True
                raise

    @property
    def revision(self) -> int:
        return self._current_revision()

    @property
    def authentication_required(self) -> bool:
        with self._metadata_guard():
            return bool(getattr(self, "_authentication_required", False))

    def set_transport_health_provider(
        self,
        provider: Callable[[], Mapping[str, Any]] | None,
    ) -> None:
        """Attach a read-only transport view without owning tunnel lifecycle."""

        with self._metadata_guard():
            self._transport_health_provider = provider

    def transport_health(self) -> Mapping[str, Any]:
        with self._metadata_guard():
            provider = getattr(self, "_transport_health_provider", None)
        if provider is None:
            return {"all_healthy": True, "lanes": {}}
        try:
            value = provider()
        except Exception as exc:
            return {
                "all_healthy": False,
                "lanes": {},
                "error": f"通道状态读取失败：{type(exc).__name__}。",
            }
        return value if isinstance(value, Mapping) else {
            "all_healthy": False,
            "lanes": {},
            "error": "通道状态格式无效。",
        }

    def _transport_lane_healthy(self, lane_key: str) -> bool:
        health = self.transport_health()
        lanes = health.get("lanes")
        if not isinstance(lanes, Mapping) or lane_key not in lanes:
            return True
        lane = lanes.get(lane_key)
        return not isinstance(lane, Mapping) or bool(lane.get("healthy"))

    def _metadata_guard(self) -> threading.RLock:
        lock = self.__dict__.get("_metadata_lock")
        if lock is None:
            lock = threading.RLock()
            self.__dict__["_metadata_lock"] = lock
        return lock

    def _page_cache_guard(self) -> threading.RLock:
        lock = self.__dict__.get("_page_cache_lock")
        if lock is None:
            lock = threading.RLock()
            self.__dict__["_page_cache_lock"] = lock
        return lock

    def _current_revision(self) -> int:
        with self._metadata_guard():
            return int(self._revision)

    def _advance_revision(self, value: object) -> int:
        with self._metadata_guard():
            self._revision = max(self._revision, int(value or 0))
            return self._revision

    @staticmethod
    def _is_access_authentication_failure(response: httpx.Response) -> bool:
        if response.status_code in {401, 403}:
            return True
        if response.status_code not in {301, 302, 303, 307, 308}:
            return False
        location = str(response.headers.get("location") or "").casefold()
        return "/cdn-cgi/access/login" in location

    def _mark_authentication_required(self) -> None:
        with self._metadata_guard():
            self._authentication_required = True
            self._authentication_error = (
                "企业邮箱登录已过期。程序不会自动打开网页；"
                "请在下一次操作时按提示重新登录。"
            )

    def _authentication_result(self) -> ControlResult:
        self._mark_authentication_required()
        with self._metadata_guard():
            message = self._authentication_error
        return ControlResult(
            False,
            message,
            details={"authentication_required": True},
        )

    def reauthenticate(self) -> ControlResult:
        """Open the login page only after the Qt client obtained user consent."""

        provider = self._access_token_provider
        if provider is None:
            return ControlResult(
                False,
                "当前客户端没有可用的企业邮箱登录组件，请安装最新版本。",
                details={"authentication_required": True},
            )
        with self._lock:
            try:
                refreshed = str(provider() or "").strip()
                if (
                    not refreshed
                    or len(refreshed) > 32 * 1024
                    or refreshed.count(".") != 2
                ):
                    raise CoordinationAuthenticationRequired(
                        "企业邮箱登录未返回有效凭据。"
                    )
                self._client.headers["Cf-Access-Token"] = refreshed
                payload = self._request(
                    "GET",
                    "/v1/snapshot",
                    headers={"X-ERP-Instance-ID": self.instance_id},
                )
                self._advance_revision(payload.get("revision"))
                with self._metadata_guard():
                    self._authentication_required = False
                    self._authentication_error = ""
                self._last_error = ""
                return ControlResult(
                    True,
                    "企业邮箱登录已恢复，请重新执行刚才的操作。",
                    details={"reauthenticated": True},
                )
            except (
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                self._mark_authentication_required()
                return ControlResult(
                    False,
                    f"企业邮箱登录未恢复：{exc}",
                    details={"authentication_required": True},
                )

    def _register_instance(self) -> None:
        payload = self._request(
            "POST",
            "/v1/instances/register",
            _allow_reregister=False,
            json={
                "instance_id": self.instance_id,
                "display_name": self.display_name,
                "browser_endpoint": self.browser_endpoint,
                "logistics_browser_endpoint": self.logistics_browser_endpoint,
                "client_version": self.client_version,
            },
        )
        self._advance_revision(payload.get("revision"))
        self.instance_pause_supported = bool(
            payload.get("instance_pause_supported", False)
        )
        operator = payload.get("operator")
        if isinstance(operator, dict):
            self.operator_name = str(operator.get("name") or "").strip()
            self.operator_email = str(operator.get("email") or "").strip()

    def _request(
        self,
        method: str,
        path: str,
        *,
        _allow_reregister: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            if self._is_access_authentication_failure(response):
                self._mark_authentication_required()
                raise CoordinationAuthenticationRequired(
                    self._authentication_error
                )
            if response.status_code == 426:
                payload = response.json()
                required = str(payload.get("required_version") or "").strip()
                if not _CLIENT_VERSION_PATTERN.fullmatch(required):
                    raise CoordinationConnectionError(
                        "服务器返回的强制更新版本无效，已拒绝启动未知版本更新。"
                    )
                raise CoordinationClientUpdateRequired(required)
            if response.status_code == 409:
                conflict = response.json()
                if conflict.get("error") == "instance_registration_expired":
                    if not _allow_reregister:
                        raise CoordinationConnectionError(
                            "共享 ERP 后台无法恢复当前客户端实例。"
                        )
                    self._register_instance()
                    return self._request(
                        method,
                        path,
                        _allow_reregister=False,
                        **kwargs,
                    )
            response.raise_for_status()
            payload = response.json()
            with self._metadata_guard():
                self._authentication_required = False
                self._authentication_error = ""
        except CoordinationConnectionError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise CoordinationConnectionError(
                f"无法连接共享 ERP 后台：{exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CoordinationConnectionError("共享 ERP 后台返回了无效响应。")
        if payload.get("ok") is False:
            raise CoordinationConnectionError(
                str(payload.get("error") or "共享 ERP 后台拒绝了请求。")
            )
        return payload

    @staticmethod
    def _queue_page_key(
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> tuple[object, ...]:
        return (
            max(1, int(page)),
            max(1, min(int(page_size), 200)),
            str(status or "").strip(),
            str(search_field or "platform_order_no").strip(),
            " ".join(str(search_query or "").split()),
            tuple(str(value or "").strip() for value in product_types),
        )

    def _apply_snapshot_payload_locked(
        self,
        payload: Mapping[str, Any],
    ) -> DesktopSnapshot:
        response_revision = int(payload.get("revision") or self._revision)
        self.instance_pause_supported = bool(
            payload.get(
                "instance_pause_supported",
                getattr(self, "instance_pause_supported", True),
            )
        )
        if payload.get("unchanged") is True:
            if self._snapshot_revision != response_revision:
                raise ValueError("Shared snapshot revision cache is inconsistent.")
            self._advance_revision(response_revision)
            self._last_error = ""
            self._schedule_local_actions(
                tuple(self._automatic_interactions.values())
            )
            if payload.get("client_update_deferred") is True:
                snapshot = deepcopy(self._last_snapshot)
                snapshot.backend_message = (
                    "客户端已有新版本；当前版本只能完成已经开始的任务，"
                    "任务安全结束后会自动更新。"
                )
                self._last_snapshot = snapshot
            return self._last_snapshot

        snapshot = decode_snapshot(payload.get("snapshot"))
        # Defense in depth for rolling upgrades: even if an older server still
        # sends a merged stream, never retain another operator's task content.
        snapshot = _filter_snapshot_for_operator(
            snapshot,
            str(getattr(self, "operator_email", "") or ""),
        )
        self._local_pause_requested = bool(
            snapshot.policy.instance_execution_paused
        )
        self._fail_safe_pause_confirmed = bool(
            snapshot.policy.instance_execution_paused
        )
        if payload.get("client_update_deferred") is True:
            snapshot.backend_message = (
                "客户端已有新版本；当前版本只能完成已经开始的任务，"
                "任务安全结束后会自动更新。"
            )
        decoded_interactions = decode_interactions(payload.get("interactions"))
        automatic_interactions = {
            request.request_id: request
            for request in decoded_interactions
            if request.automatic_action
        }
        self._automatic_interactions = automatic_interactions
        self._last_interactions = tuple(
            request
            for request in decoded_interactions
            if not request.automatic_action
        )
        self._schedule_local_actions(tuple(automatic_interactions.values()))
        self._advance_revision(response_revision)
        self._snapshot_revision = response_revision
        self._last_snapshot = snapshot
        self._last_error = ""
        self._cleanup_browser_after_terminal_tasks(snapshot)
        return snapshot

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            try:
                if bool(getattr(self, "_startup_snapshot_pending", False)):
                    self._startup_snapshot_pending = False
                    return self._last_snapshot
                if (
                    getattr(self, "_local_pause_requested", False)
                    and not getattr(self, "_fail_safe_pause_confirmed", False)
                ):
                    self._request_fail_safe_pause(
                        self._last_snapshot.policy.execution_pause_reason
                        or "本机重试暂停请求。"
                    )
                params: dict[str, object] = {"snapshot_mode": "summary_v1"}
                if self._snapshot_revision is not None:
                    params["known_revision"] = self._snapshot_revision
                payload = self._request(
                    "GET",
                    "/v1/snapshot",
                    headers={"X-ERP-Instance-ID": self.instance_id},
                    params=params,
                )
                return self._apply_snapshot_payload_locked(payload)
            except CoordinationClientUpdateRequired:
                raise
            except (CoordinationConnectionError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                stale = deepcopy(self._last_snapshot)
                stale.backend_message = (
                    "企业邮箱登录已过期。程序不会自动打开网页；"
                    "请在下一次操作时按提示重新登录。当前显示最近一次数据。"
                    if isinstance(exc, CoordinationAuthenticationRequired)
                    else (
                        "共享 ERP 后台暂时不可用；当前显示最近一次数据。"
                        f"{self._last_error}"
                    )
                )
                return stale

    def prime_startup_state(self) -> ControlResult:
        """Fetch the summary and both first queue pages in one round trip.

        The packaged bootstrap calls this while its progress window is still
        visible.  The main window can therefore paint real rows immediately
        instead of exposing two empty tables while three HTTP requests run.
        """

        with self._lock:
            try:
                payload = self._request(
                    "GET",
                    "/v1/snapshot",
                    headers={"X-ERP-Instance-ID": self.instance_id},
                    params={
                        "snapshot_mode": "summary_v1",
                        "include_queue_pages": "1",
                    },
                )
                if payload.get("unchanged") is True:
                    raise ValueError("Startup snapshot unexpectedly omitted its body.")
                snapshot = self._apply_snapshot_payload_locked(payload)
                custom_page = decode_custom_order_page(
                    payload.get("custom_order_page")
                )
                shipment_page = decode_shipment_page(payload.get("shipment_page"))
                default_key = self._queue_page_key()
                with self._page_cache_guard():
                    self._prefetched_custom_order_pages[default_key] = custom_page
                    self._prefetched_shipment_pages[default_key] = shipment_page
                self._startup_snapshot_pending = True
                return ControlResult(
                    True,
                    "定制订单和自动标发首屏已预加载。",
                    details={
                        "custom_order_count": custom_page.total,
                        "shipment_count": shipment_page.total,
                        "first_paint_ready": True,
                        "emergency_stop_writes": (
                            snapshot.policy.emergency_stop_writes
                        ),
                    },
                )
            except (CoordinationConnectionError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                return ControlResult(
                    False,
                    f"首屏预加载失败：{exc}",
                    details={"first_paint_ready": False},
                )

    def take_startup_snapshot(self) -> DesktopSnapshot | None:
        """Return a freshly primed snapshot without another network request."""

        with self._lock:
            if not self._startup_snapshot_pending:
                return None
            self._startup_snapshot_pending = False
            return self._last_snapshot

    def list_custom_order_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> CustomOrderPage:
        key = self._queue_page_key(
            page=page,
            page_size=page_size,
            status=status,
            search_field=search_field,
            search_query=search_query,
            product_types=product_types,
        )
        with self._page_cache_guard():
            cached = self._prefetched_custom_order_pages.pop(key, None)
            if (
                cached is not None
                and cached.dataset_revision
                == self._last_snapshot.custom_orders_summary.revision
            ):
                return deepcopy(cached)
        return self._concurrent_read_rpc(
            "list_custom_order_page",
            page=page,
            page_size=page_size,
            status=status,
            search_field=search_field,
            search_query=search_query,
            product_types=tuple(product_types),
        )

    def list_shipment_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> ShipmentPage:
        key = self._queue_page_key(
            page=page,
            page_size=page_size,
            status=status,
            search_field=search_field,
            search_query=search_query,
            product_types=product_types,
        )
        with self._page_cache_guard():
            cached = self._prefetched_shipment_pages.pop(key, None)
            if (
                cached is not None
                and cached.dataset_revision
                == self._last_snapshot.shipments_summary.revision
            ):
                return deepcopy(cached)
        return self._concurrent_read_rpc(
            "list_shipment_page",
            page=page,
            page_size=page_size,
            status=status,
            search_field=search_field,
            search_query=search_query,
            product_types=tuple(product_types),
        )

    def _schedule_local_actions(
        self,
        requests: tuple[DesktopInteractionRequest, ...],
    ) -> None:
        """Queue each targeted automatic action once; retry only its response."""

        if bool(getattr(self, "_closed", False)):
            return
        for request in requests:
            if request.request_id in self._local_action_inflight:
                continue
            self._local_action_inflight.add(request.request_id)
            self._local_action_pool.submit(
                self._execute_and_respond_local_action,
                request,
            )

    def _execute_and_respond_local_action(
        self,
        request: DesktopInteractionRequest,
    ) -> None:
        response = self._local_action_responses.get(request.request_id)
        if response is None:
            try:
                executor = self._local_action_executor
                if executor is None:
                    raise RuntimeError("当前桌面没有可用的本机浏览器执行器。")
                execute = getattr(executor, "execute", executor)
                result = execute(request.automatic_action, request.action_payload)
                if not isinstance(result, Mapping):
                    raise TypeError("本机浏览器步骤返回了无效结果。")
                response = DesktopInteractionResponse(
                    request_id=request.request_id,
                    accepted=True,
                    result_data=dict(result),
                )
            except Exception as exc:
                response = DesktopInteractionResponse(
                    request_id=request.request_id,
                    accepted=False,
                    result_data={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc) or "本机浏览器步骤执行失败。",
                    },
                )
            with self._lock:
                self._local_action_responses[request.request_id] = response

        delivered = False
        try:
            result = self._rpc("respond_interaction", response)
            delivered = isinstance(result, ControlResult) and (
                result.accepted
                or bool(result.details.get("interaction_stale"))
            )
        finally:
            with self._lock:
                self._local_action_inflight.discard(request.request_id)
                if delivered:
                    self._local_action_responses.pop(request.request_id, None)
                    self._automatic_interactions.pop(request.request_id, None)
                else:
                    # Force the next poll to fetch the authoritative request so
                    # its cached response can be retried without re-running any
                    # page mutation.
                    self._snapshot_revision = None

    def pending_interactions(self) -> tuple[DesktopInteractionRequest, ...]:
        """Return interactions received with the latest shared snapshot."""

        with self._lock:
            return tuple(self._last_interactions)

    def _start_browser_for_approved_fallback(
        self,
        method: str,
        args: tuple[Any, ...],
    ) -> ControlResult | None:
        if method != "respond_interaction" or not args:
            return None
        response = args[0]
        if not bool(getattr(response, "accepted", False)):
            return None
        request_id = str(getattr(response, "request_id", "") or "")
        request = next(
            (
                item
                for item in self._last_interactions
                if item.request_id == request_id
            ),
            None,
        )
        if request is None or request.stage != "erp_mark:browser_fallback":
            return None
        if self._browser_host is None or not self.browser_endpoint:
            return ControlResult(
                False,
                "当前桌面没有配置本机 Chrome 通道，不能批准网页回退。",
                request.task_id,
            )
        self._browser_host.ensure_started()
        return None

    @staticmethod
    def _closes_browser_after_task(command: TaskCommand) -> bool:
        if command.area is TaskArea.CUSTOMIZATION:
            return command.capability is not Capability.LIST_ORDERS
        if command.area is TaskArea.SHIPMENT:
            return command.capability in {
                Capability.ALIBABA_LOGISTICS,
                Capability.OUTBOUND_ORDER,
            }
        return False

    def _cleanup_browser_after_terminal_tasks(
        self,
        snapshot: DesktopSnapshot,
    ) -> None:
        self._cleanup_browser_lane_after_terminal_tasks(
            snapshot,
            host_attr="_browser_host",
            cleanup_ids_attr="_browser_cleanup_task_ids",
            close_pending_attr="_browser_close_pending",
            task_uses_lane=lambda task: (
                (
                    task.area is TaskArea.CUSTOMIZATION
                    and task.capability is not Capability.LIST_ORDERS
                )
                or (
                    task.area is TaskArea.SHIPMENT
                    and task.capability
                    in {
                        Capability.ALIBABA_ORDER_PREPARE,
                        Capability.ALIBABA_ORDER_DRAFT,
                    }
                )
            ),
        )
        self._cleanup_browser_lane_after_terminal_tasks(
            snapshot,
            host_attr="_logistics_browser_host",
            cleanup_ids_attr="_logistics_browser_cleanup_task_ids",
            close_pending_attr="_logistics_browser_close_pending",
            task_uses_lane=lambda task: (
                task.area is TaskArea.SHIPMENT
                and task.capability is Capability.ALIBABA_LOGISTICS
            ),
        )

    def _cleanup_browser_lane_after_terminal_tasks(
        self,
        snapshot: DesktopSnapshot,
        *,
        host_attr: str,
        cleanup_ids_attr: str,
        close_pending_attr: str,
        task_uses_lane: Callable[[Any], bool],
    ) -> None:
        cleanup_task_ids = getattr(self, cleanup_ids_attr, None)
        browser_host = getattr(self, host_attr, None)
        if browser_host is None:
            return
        if not cleanup_task_ids and not bool(
            getattr(self, close_pending_attr, False)
        ):
            return
        tasks_by_id = {task.task_id: task for task in snapshot.tasks}
        completed = {
            task_id
            for task_id in cleanup_task_ids
            if (task := tasks_by_id.get(task_id)) is not None
            and task.status.terminal
        }
        if not completed:
            if not bool(getattr(self, close_pending_attr, False)):
                return
        else:
            cleanup_task_ids.difference_update(completed)
            setattr(self, close_pending_attr, True)

        # Every task submitted by this desktop carries its instance id in the
        # authoritative snapshot.  A terminal browser task must not close the
        # shared Chrome profile while another browser-using task from the same
        # desktop is still queued, running, or waiting for the operator.  Pure
        # API/background work does not delay browser cleanup.
        active_same_instance_browser_task = any(
            not task.status.terminal
            and str(
                task.payload.get(DESKTOP_INSTANCE_ID_PAYLOAD_KEY) or ""
            ).strip()
            == str(getattr(self, "instance_id", "") or "").strip()
            and (task_uses_lane(task) or task.task_id in cleanup_task_ids)
            for task in snapshot.tasks
        )
        if cleanup_task_ids or active_same_instance_browser_task:
            return
        if bool(getattr(self, close_pending_attr, False)):
            browser_host.close_pages()
            setattr(self, close_pending_attr, False)

    def _rpc_request_timeout(
        self,
        method: str,
        args: tuple[Any, ...],
    ) -> httpx.Timeout | None:
        """Scale only long-running RPC reads without slowing connection failures."""

        base_timeout = max(
            3.0,
            float(getattr(self, "_timeout_seconds", 5.0)),
        )
        if method == "submit_tasks":
            first_arg = args[0] if args else ()
            item_count = (
                len(first_arg)
                if isinstance(first_arg, (list, tuple))
                else 1
            )
            read_timeout = min(
                _MAX_TASK_BATCH_READ_TIMEOUT_SECONDS,
                max(
                    base_timeout,
                    _TASK_BATCH_READ_TIMEOUT_OVERHEAD_SECONDS
                    + max(1, item_count)
                    * _TASK_BATCH_READ_TIMEOUT_PER_ITEM_SECONDS,
                ),
            )
            return httpx.Timeout(
                base_timeout,
                connect=base_timeout,
                read=read_timeout,
                write=base_timeout,
                pool=base_timeout,
            )
        if method in {
            "list_shipment_notifications",
            "get_shipment_notification_details",
            "get_shipment_notification_review_previews",
            "list_custom_order_page",
            "list_shipment_page",
        }:
            return httpx.Timeout(
                base_timeout,
                connect=base_timeout,
                read=max(
                    base_timeout,
                    _NOTIFICATION_READ_TIMEOUT_SECONDS,
                ),
                write=base_timeout,
                pool=base_timeout,
            )
        if method == "approve_shipment_notifications":
            first_arg = args[0] if args else ()
            item_count = (
                len(first_arg)
                if isinstance(first_arg, (list, tuple, set, frozenset))
                else 1
            )
        elif method in {
            "approve_shipment_notification",
            "retry_shipment_notification",
        }:
            item_count = 1
        else:
            return None
        read_timeout = min(
            _MAX_NOTIFICATION_SEND_TIMEOUT_SECONDS,
            max(
                base_timeout,
                _NOTIFICATION_SEND_TIMEOUT_OVERHEAD_SECONDS
                + max(1, item_count)
                * _NOTIFICATION_SEND_TIMEOUT_PER_ITEM_SECONDS,
            ),
        )
        return httpx.Timeout(
            base_timeout,
            connect=base_timeout,
            read=read_timeout,
            write=base_timeout,
            pool=base_timeout,
        )

    def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        request_id = uuid4().hex
        batch_command_count = (
            len(args[0])
            if method == "submit_tasks"
            and args
            and isinstance(args[0], (list, tuple))
            else 0
        )

        def submission_result(value: ControlResult) -> Any:
            return (
                tuple(value for _index in range(batch_command_count))
                if method == "submit_tasks"
                else value
            )

        with self._lock:
            try:
                if method in {"submit_task", "submit_tasks"} and getattr(
                    self,
                    "_local_pause_requested",
                    False,
                ):
                    return submission_result(
                        ControlResult(
                            False,
                            "本机任务正在停止或已暂停，恢复前不会提交新任务。",
                            details={
                                "execution_paused": True,
                                "instance_execution_paused": True,
                                "reason": "instance_execution_paused",
                            },
                        )
                    )
                if bool(getattr(self, "_authentication_required", False)):
                    if method in MUTATION_METHODS:
                        return submission_result(self._authentication_result())
                    raise CoordinationAuthenticationRequired(
                        self._authentication_error
                    )
                fallback_error = self._start_browser_for_approved_fallback(
                    method,
                    args,
                )
                if fallback_error is not None:
                    return fallback_error
                submitted_commands: tuple[TaskCommand, ...] = ()
                if method == "submit_task" and args and isinstance(args[0], TaskCommand):
                    submitted_commands = (args[0],)
                elif (
                    method == "submit_tasks"
                    and args
                    and isinstance(args[0], (list, tuple))
                ):
                    submitted_commands = tuple(
                        command
                        for command in args[0]
                        if isinstance(command, TaskCommand)
                    )
                    if len(submitted_commands) != len(args[0]):
                        raise TypeError("批量任务包含无效命令。")
                prepared_browser_lanes: set[str] = set()
                for command in submitted_commands:
                    requires_browser = task_requires_visible_browser(command)
                    if requires_browser:
                        logistics_query = (
                            command.area is TaskArea.SHIPMENT
                            and command.capability is Capability.ALIBABA_LOGISTICS
                        )
                        browser_host = (
                            getattr(self, "_logistics_browser_host", None)
                            if logistics_query
                            else self._browser_host
                        )
                        browser_endpoint = (
                            str(
                                getattr(self, "logistics_browser_endpoint", "")
                                or ""
                            )
                            if logistics_query
                            else self.browser_endpoint
                        )
                        lane_key = "logistics" if logistics_query else "browser"
                        transport_lane_key = (
                            "logistics_browser"
                            if logistics_query
                            else "main_browser"
                        )
                        if not self._transport_lane_healthy(transport_lane_key):
                            lane_label = (
                                "物流查询浏览器"
                                if logistics_query
                                else "主浏览器"
                            )
                            return submission_result(
                                ControlResult(
                                    False,
                                    f"本机{lane_label}通道正在自动重连，"
                                    "网页任务未提交；请等待左侧连接状态恢复后重试。",
                                    details={
                                        "local_browser_unavailable": True,
                                        "browser_tunnel_recovering": True,
                                        "browser_lane": transport_lane_key,
                                        "retry_suppressed": True,
                                    },
                                )
                            )
                        if (
                            browser_host is None
                            or not browser_endpoint
                        ):
                            return submission_result(
                                ControlResult(
                                    False,
                                    "当前桌面没有配置本机 Chrome 通道，网页任务未提交。",
                                    details={
                                        "local_browser_unavailable": True,
                                        "retry_suppressed": True,
                                    },
                                )
                            )
                        if lane_key in prepared_browser_lanes:
                            continue
                        if (
                            command.area is TaskArea.SHIPMENT
                            and command.capability
                            is Capability.ALIBABA_ORDER_PREPARE
                        ):
                            browser_host.open_url(ALIBABA_QUOTE_URL)
                        elif (
                            command.area is TaskArea.MAINTENANCE
                            and str(command.payload.get("trigger") or "").strip()
                            == LINGXING_BROWSER_LOGIN_TRIGGER
                        ):
                            browser_host.open_url(
                                LINGXING_ORDER_MANAGEMENT_URL
                            )
                        elif logistics_query:
                            # Always open or activate the low-risk SCM landing
                            # page before the remote worker deep-links into a
                            # logistics detail.  ``ensure_started`` only uses
                            # its initial URL for a cold Chrome process, while
                            # ``open_url`` also handles an already healthy
                            # process whose previous task closed all pages.
                            browser_host.open_url(ALIBABA_SCM_HOME_URL)
                        else:
                            browser_host.ensure_started()
                        prepared_browser_lanes.add(lane_key)
                request_options: dict[str, Any] = {
                    "json": {
                        "instance_id": self.instance_id,
                        "request_id": request_id,
                        "method": method,
                        "args": to_jsonable(list(args)),
                        "kwargs": to_jsonable(kwargs),
                        "expected_revision": self._current_revision(),
                    }
                }
                request_timeout = self._rpc_request_timeout(method, args)
                if request_timeout is not None:
                    request_options["timeout"] = request_timeout
                payload = self._request(
                    "POST",
                    "/v1/rpc",
                    **request_options,
                )
                self._advance_revision(payload.get("revision"))
                result_type = str(payload.get("result_type") or "json")
                result = payload.get("result")
                if result_type == "control_result":
                    control_result = decode_control_result(result)
                    if (
                        method == "submit_task"
                        and args
                        and isinstance(args[0], TaskCommand)
                        and control_result.accepted
                        and control_result.task_id
                        and self._closes_browser_after_task(args[0])
                    ):
                        cleanup_ids = (
                            self._logistics_browser_cleanup_task_ids
                            if (
                                args[0].area is TaskArea.SHIPMENT
                                and args[0].capability
                                is Capability.ALIBABA_LOGISTICS
                            )
                            else self._browser_cleanup_task_ids
                        )
                        cleanup_ids.add(str(control_result.task_id))
                    if method == "respond_interaction" and args:
                        response = args[0]
                        interaction_id = str(
                            getattr(response, "request_id", "") or ""
                        )
                        if (
                            interaction_id
                            and (
                                control_result.accepted
                                or bool(
                                    control_result.details.get(
                                        "interaction_stale"
                                    )
                                )
                            )
                        ):
                            # The shared snapshot may keep the same revision
                            # briefly after a response.  Remove the answered
                            # request locally so the modal cannot reopen.
                            self._last_interactions = tuple(
                                request
                                for request in self._last_interactions
                                if request.request_id != interaction_id
                            )
                    return control_result
                if result_type == "control_results":
                    if not isinstance(result, list):
                        raise TypeError("共享后台返回了无效的批量任务结果。")
                    control_results = tuple(
                        decode_control_result(value) for value in result
                    )
                    if len(control_results) != len(submitted_commands):
                        raise ValueError("共享后台返回的批量任务数量不一致。")
                    for command, control_result in zip(
                        submitted_commands,
                        control_results,
                    ):
                        if (
                            control_result.accepted
                            and control_result.task_id
                            and self._closes_browser_after_task(command)
                        ):
                            cleanup_ids = (
                                self._logistics_browser_cleanup_task_ids
                                if (
                                    command.area is TaskArea.SHIPMENT
                                    and command.capability
                                    is Capability.ALIBABA_LOGISTICS
                                )
                                else self._browser_cleanup_task_ids
                            )
                            cleanup_ids.add(str(control_result.task_id))
                    return control_results
                if result_type == "log_page":
                    return decode_log_page(result)
                if result_type == "custom_order_page":
                    return decode_custom_order_page(result)
                if result_type == "shipment_page":
                    return decode_shipment_page(result)
                if result_type == "interactions":
                    return decode_interactions(result)
                if method in {"full_log_text", "scan_log_text"} and isinstance(result, list):
                    return tuple(str(item) for item in result[:2])
                return result
            except (
                CoordinationConnectionError,
                LocalBrowserUnavailable,
                TypeError,
                ValueError,
            ) as exc:
                message = str(exc)
                if method == "submit_tasks":
                    if isinstance(exc, CoordinationAuthenticationRequired):
                        return submission_result(self._authentication_result())
                    if isinstance(exc, LocalBrowserUnavailable):
                        return submission_result(
                            ControlResult(
                                False,
                                message,
                                details={
                                    "local_browser_unavailable": True,
                                    "retry_suppressed": True,
                                },
                            )
                        )
                    cause: BaseException | None = exc
                    read_timed_out = False
                    while cause is not None:
                        if isinstance(cause, httpx.ReadTimeout):
                            read_timed_out = True
                            break
                        cause = cause.__cause__
                    if read_timed_out:
                        return submission_result(
                            ControlResult(
                                False,
                                "批量提交请求已发送，正在等待服务器确认；"
                                "程序会通过任务列表继续核对，请勿重复提交。",
                                details={
                                    "submission_outcome_unknown": True,
                                    "non_modal": True,
                                    "retry_suppressed": True,
                                },
                            )
                        )
                    return submission_result(ControlResult(False, message))
                if method in MUTATION_METHODS:
                    if isinstance(exc, CoordinationAuthenticationRequired):
                        return self._authentication_result()
                    if isinstance(exc, LocalBrowserUnavailable):
                        return ControlResult(
                            False,
                            message,
                            details={
                                "local_browser_unavailable": True,
                                "retry_suppressed": True,
                            },
                        )
                    return ControlResult(False, message)
                if method == "pending_interactions":
                    return ()
                if method in {"full_log_text", "scan_log_text"}:
                    return (
                        "扫描日志" if method == "scan_log_text" else "完整日志",
                        message,
                    )
                if method == "log_directory":
                    return ""
                if method == "list_log_entries":
                    return LogPage()
                raise

    def _concurrent_read_rpc(
        self,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run immutable queue/detail reads outside the mutation RPC lock.

        The main lock protects mutations, browser preparation and optimistic
        state. Queue pages and notification previews are immutable read
        projections; serializing their network wait behind a snapshot made
        navigation and approval appear frozen. httpx clients support
        concurrent thread use, while revision metadata has its own short lock.
        """

        if method not in _CONCURRENT_READ_METHODS:
            raise ValueError("Unsupported concurrent read RPC method.")
        with self._metadata_guard():
            if bool(getattr(self, "_authentication_required", False)):
                raise CoordinationAuthenticationRequired(
                    self._authentication_error
                )
        if bool(getattr(self, "_closed", False)):
            raise CoordinationConnectionError("共享 ERP 客户端已经关闭。")
        try:
            request_options: dict[str, Any] = {
                "json": {
                    "instance_id": self.instance_id,
                    "request_id": uuid4().hex,
                    "method": method,
                    "args": to_jsonable(list(args)),
                    "kwargs": to_jsonable(kwargs),
                    "expected_revision": self._current_revision(),
                }
            }
            request_timeout = self._rpc_request_timeout(method, args)
            if request_timeout is not None:
                request_options["timeout"] = request_timeout
            payload = self._request(
                "POST",
                "/v1/rpc",
                **request_options,
            )
            self._advance_revision(payload.get("revision"))
            result_type = str(payload.get("result_type") or "json")
            result = payload.get("result")
            if (
                method == "list_custom_order_page"
                and result_type == "custom_order_page"
            ):
                return decode_custom_order_page(result)
            if (
                method == "list_shipment_page"
                and result_type == "shipment_page"
            ):
                return decode_shipment_page(result)
            if method in {
                "list_shipment_notifications",
                "get_shipment_notification_details",
                "get_shipment_notification_review_previews",
            } and result_type == "json":
                return result
            raise TypeError("共享后台返回了无效的只读队列结果。")
        except (
            CoordinationConnectionError,
            TypeError,
            ValueError,
        ) as exc:
            self._last_error = str(exc)
            raise

    def _request_fail_safe_pause(self, reason: str) -> ControlResult:
        """Use the narrow bearer-token safety endpoint without requiring SSO."""

        try:
            response = self._client.post(
                "/v1/safety/pause",
                json={
                    "instance_id": str(getattr(self, "instance_id", "") or ""),
                    "reason": str(reason or ""),
                },
            )
            response.raise_for_status()
            payload = response.json()
            result = decode_control_result(payload.get("result"))
            self._fail_safe_pause_confirmed = True
            if result.accepted or result.details.get("execution_paused"):
                return ControlResult(
                    True,
                    result.message or "服务器已确认本机暂停。",
                    details={
                        **dict(result.details),
                        "fail_safe_endpoint": True,
                    },
                )
            return result
        except (httpx.HTTPError, TypeError, ValueError):
            self._fail_safe_pause_confirmed = False
            return ControlResult(
                True,
                "本机已进入暂停保护；服务器暂时失联，"
                "服务端会在客户端心跳超时后停止仅属于本机的任务。",
                details={
                    "execution_paused": True,
                    "server_confirmation_pending": True,
                },
            )

    def set_execution_paused(
        self,
        enabled: bool,
        reason: str = "",
    ) -> ControlResult:
        normalized_reason = str(reason or "").strip()[:500] or (
            "用户暂停本机任务。" if enabled else ""
        )
        if not getattr(self, "instance_pause_supported", True):
            return ControlResult(
                False,
                "当前共享服务版本不支持本机级暂停；为避免误停其他主机，已禁止执行。请先升级服务端。",
                details={"instance_pause_supported": False},
            )
        if enabled:
            with self._lock:
                self._local_pause_requested = True
                self._fail_safe_pause_confirmed = False
                self._last_snapshot.policy.execution_paused = True
                self._last_snapshot.policy.execution_pause_reason = normalized_reason
                self._last_snapshot.policy.instance_execution_paused = True
                self._last_snapshot.policy.instance_execution_pause_state = "pausing"
            if not self.authentication_required:
                result = self._rpc(
                    "set_execution_paused",
                    True,
                    normalized_reason,
                )
                if isinstance(result, ControlResult) and result.accepted:
                    self._fail_safe_pause_confirmed = True
                    return result
            return self._request_fail_safe_pause(normalized_reason)
        result = self._rpc("set_execution_paused", False, "")
        if isinstance(result, ControlResult) and result.accepted:
            with self._lock:
                self._local_pause_requested = False
                self._fail_safe_pause_confirmed = False
                self._last_snapshot.policy.execution_paused = False
                self._last_snapshot.policy.execution_pause_reason = ""
                self._last_snapshot.policy.instance_execution_paused = False
                self._last_snapshot.policy.instance_execution_pause_state = "active"
                self._last_snapshot.policy.execution_paused = False
        return result

    def export_portable_migration(
        self,
        destination: str,
        passphrase: str,
        *,
        include_state: bool,
    ) -> ControlResult:
        """Download a server-created encrypted settings package to this PC."""

        if include_state:
            return ControlResult(
                False,
                "共享客户端只允许导出设置；SQLite 业务状态统一保存在服务器。",
            )
        with self._lock:
            try:
                payload = self._request(
                    "POST",
                    "/v1/configuration/export",
                    json={
                        "instance_id": self.instance_id,
                        "request_id": uuid4().hex,
                        "passphrase": str(passphrase or ""),
                    },
                )
                result = decode_control_result(payload.get("result"))
                if not result.accepted:
                    return result
                encoded = str(payload.get("package_base64") or "")
                package = base64.b64decode(encoded, validate=True)
                if (
                    not package
                    or len(package) > MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES
                ):
                    raise ValueError("服务器返回的加密设置包大小无效。")
                target = Path(destination)
                atomic_write_bytes(
                    target,
                    package,
                    backup_path=backup_path_for(target),
                )
                self._advance_revision(payload.get("revision"))
                return ControlResult(
                    True,
                    f"加密设置已导出到当前电脑：{target}",
                    details=dict(result.details),
                )
            except (CoordinationConnectionError, OSError, ValueError) as exc:
                if isinstance(exc, CoordinationAuthenticationRequired):
                    return self._authentication_result()
                return ControlResult(False, f"导出加密设置失败：{exc}")

    def import_portable_migration(
        self,
        package_path: str,
        passphrase: str,
        *,
        overwrite: bool,
        configuration_only: bool = False,
    ) -> ControlResult:
        """Upload a local encrypted settings package to the shared server."""

        if not overwrite:
            return ControlResult(False, "共享设置导入必须明确允许覆盖并保留备份。")
        source = Path(package_path)
        try:
            package = source.read_bytes()
        except OSError as exc:
            return ControlResult(False, f"读取本机加密设置包失败：{exc}")
        if (
            not package
            or len(package) > MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES
        ):
            return ControlResult(False, "本机加密设置包大小无效。")
        with self._lock:
            try:
                payload = self._request(
                    "POST",
                    "/v1/configuration/import",
                    json={
                        "instance_id": self.instance_id,
                        "request_id": uuid4().hex,
                        "passphrase": str(passphrase or ""),
                        "package_base64": base64.b64encode(package).decode(
                            "ascii"
                        ),
                    },
                )
                self._advance_revision(payload.get("revision"))
                result = decode_control_result(payload.get("result"))
                if result.accepted:
                    # The imported settings must be fetched even if a caller
                    # previously cached a snapshot at the same local object.
                    self._snapshot_revision = None
                return result
            except (CoordinationConnectionError, ValueError) as exc:
                if isinstance(exc, CoordinationAuthenticationRequired):
                    return self._authentication_result()
                return ControlResult(False, f"导入加密设置失败：{exc}")

    def prepare_close(self) -> ControlResult:
        """Pause owned work before detaching this desktop window."""

        with self._lock:
            if self._closed:
                return ControlResult(
                    True,
                    "当前窗口已退出；服务器后台任务继续运行。",
                )
            active = any(
                not task.status.terminal
                for task in self._last_snapshot.tasks
                if str(
                    task.payload.get(DESKTOP_INSTANCE_ID_PAYLOAD_KEY) or ""
                ).strip()
                == self.instance_id
            )
        pause_result = (
            self.set_execution_paused(
                True,
                "桌面程序关闭，已停止本机任务。",
            )
            if active
            else ControlResult(True, "没有活动任务。")
        )
        with self._lock:
            self._closed = True
        try:
            self._request(
                "POST",
                "/v1/instances/deregister",
                json={"instance_id": self.instance_id},
            )
        except CoordinationConnectionError:
            pass
        finally:
            self._local_action_pool.shutdown(wait=False, cancel_futures=True)
            self._client.close()
            if self._browser_host is not None:
                self._browser_host.close()
            logistics_browser_host = getattr(
                self,
                "_logistics_browser_host",
                None,
            )
            if logistics_browser_host is not None:
                logistics_browser_host.close()
        return ControlResult(
            pause_result.accepted,
            (
                "当前窗口已退出；活动任务已进入暂停保护。"
                if active
                else "当前窗口已退出；没有活动任务。"
            ),
            details=dict(pause_result.details),
        )

    def __getattr__(self, name: str) -> Any:
        if name not in RPC_METHODS:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if name in _CONCURRENT_READ_METHODS:
                return self._concurrent_read_rpc(name, *args, **kwargs)
            return self._rpc(name, *args, **kwargs)

        return call

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(READ_METHODS) | set(MUTATION_METHODS))


def _install_rpc_methods() -> None:
    """Materialize protocol methods for static/runtime structural checks."""

    def make_method(method_name: str):
        def rpc_method(self: RemoteBackgroundTaskController, *args: Any, **kwargs: Any) -> Any:
            if method_name in _CONCURRENT_READ_METHODS:
                return self._concurrent_read_rpc(method_name, *args, **kwargs)
            return self._rpc(method_name, *args, **kwargs)

        rpc_method.__name__ = method_name
        rpc_method.__qualname__ = (
            f"RemoteBackgroundTaskController.{method_name}"
        )
        return rpc_method

    for method_name in RPC_METHODS:
        if method_name not in RemoteBackgroundTaskController.__dict__:
            setattr(RemoteBackgroundTaskController, method_name, make_method(method_name))


_install_rpc_methods()
