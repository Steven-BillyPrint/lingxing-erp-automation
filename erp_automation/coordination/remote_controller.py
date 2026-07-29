"""Desktop-side proxy for the authoritative shared controller."""

from __future__ import annotations

import base64
import os
import socket
import threading
from copy import deepcopy
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from erp_automation.configuration import atomic_write_bytes, backup_path_for
from erp_automation.ui.controller import ControlResult
from erp_automation.ui.models import (
    Capability,
    DesktopInteractionRequest,
    DesktopSnapshot,
    LogPage,
    TaskArea,
    TaskCommand,
    task_requires_visible_browser,
)
from shipment_automation.alibaba_logistics import logistics_detail_url

from .codec import (
    decode_control_result,
    decode_interactions,
    decode_log_page,
    decode_snapshot,
    to_jsonable,
)
from .local_browser import (
    ALIBABA_SCM_HOME_URL,
    LocalBrowserUnavailable,
    LocalChromeHost,
)
from .service import (
    MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES,
    MUTATION_METHODS,
    READ_METHODS,
    RPC_METHODS,
)

_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY = "local_visible_logistics_followup"
_QUERYABLE_LOGISTICS_STATES = frozenset({"PENDING", "WAITING", "RETRYABLE"})
_NOTIFICATION_SEND_TIMEOUT_PER_ITEM_SECONDS = 105.0
_NOTIFICATION_SEND_TIMEOUT_OVERHEAD_SECONDS = 30.0
_MAX_NOTIFICATION_SEND_TIMEOUT_SECONDS = 60.0 * 60.0


class CoordinationConnectionError(RuntimeError):
    """The shared controller could not be reached or authenticated."""


class CoordinationAuthenticationRequired(CoordinationConnectionError):
    """The operator must explicitly renew the Cloudflare Access session."""


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
        strict_registration: bool = False,
        access_token: str = "",
        access_token_provider: Callable[[], str] | None = None,
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
        self.operator_name = ""
        self.operator_email = ""
        self._access_token_provider = access_token_provider
        self._authentication_required = False
        self._authentication_error = ""
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
        request_headers = {
            "Authorization": f"Bearer {normalized_token}",
            "Accept": "application/json",
            "User-Agent": "lingxing-erp-desktop-coordination/1",
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
        self._closed = False
        self._last_snapshot = DesktopSnapshot(
            backend_message="正在连接共享 ERP 后台…"
        )
        self._last_interactions: tuple[DesktopInteractionRequest, ...] = ()
        self._snapshot_revision: int | None = None
        self._revision = 0
        self._last_error = ""
        try:
            payload = self._request(
                "POST",
                "/v1/instances/register",
                json={
                    "instance_id": self.instance_id,
                    "display_name": self.display_name,
                    "browser_endpoint": self.browser_endpoint,
                    "client_version": self.client_version,
                },
            )
            self._revision = int(payload.get("revision") or 0)
            operator = payload.get("operator")
            if isinstance(operator, dict):
                self.operator_name = str(operator.get("name") or "").strip()
                self.operator_email = str(operator.get("email") or "").strip()
        except CoordinationConnectionError as exc:
            self._last_error = str(exc)
            if strict_registration:
                self._client.close()
                if self._browser_host is not None:
                    self._browser_host.close()
                self._closed = True
                raise

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def authentication_required(self) -> bool:
        with self._lock:
            return bool(getattr(self, "_authentication_required", False))

    @staticmethod
    def _is_access_authentication_failure(response: httpx.Response) -> bool:
        if response.status_code in {401, 403}:
            return True
        if response.status_code not in {301, 302, 303, 307, 308}:
            return False
        location = str(response.headers.get("location") or "").casefold()
        return "/cdn-cgi/access/login" in location

    def _mark_authentication_required(self) -> None:
        self._authentication_required = True
        self._authentication_error = (
            "企业邮箱登录已过期。程序不会自动打开网页；"
            "请在下一次操作时按提示重新登录。"
        )

    def _authentication_result(self) -> ControlResult:
        self._mark_authentication_required()
        return ControlResult(
            False,
            self._authentication_error,
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
                self._revision = max(
                    self._revision,
                    int(payload.get("revision") or self._revision),
                )
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

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
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
                raise CoordinationConnectionError(
                    f"客户端必须更新到 {required or '最新版本'} 后才能连接共享后台。"
                )
            response.raise_for_status()
            payload = response.json()
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

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            try:
                params = (
                    {"known_revision": self._snapshot_revision}
                    if self._snapshot_revision is not None
                    else None
                )
                payload = self._request(
                    "GET",
                    "/v1/snapshot",
                    headers={"X-ERP-Instance-ID": self.instance_id},
                    params=params,
                )
                response_revision = int(
                    payload.get("revision") or self._revision
                )
                if payload.get("unchanged") is True:
                    if self._snapshot_revision != response_revision:
                        raise ValueError(
                            "Shared snapshot revision cache is inconsistent."
                        )
                    self._revision = max(self._revision, response_revision)
                    self._last_error = ""
                    return self._last_snapshot
                snapshot = decode_snapshot(payload.get("snapshot"))
                self._last_interactions = decode_interactions(
                    payload.get("interactions")
                )
                self._revision = max(self._revision, response_revision)
                self._snapshot_revision = response_revision
                self._last_snapshot = snapshot
                self._last_error = ""
                return snapshot
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
    def _prewarms_local_logistics(command: TaskCommand) -> bool:
        return bool(
            command.area is TaskArea.SHIPMENT
            and command.capability is Capability.LIST_ORDERS
            and command.payload.get(_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY)
        )

    def _logistics_prewarm_url(self) -> str:
        candidates = []
        now = datetime.now(timezone.utc)
        for row in self._last_snapshot.shipments:
            logistics_no = str(row.logistics_no or "").strip()
            if (
                not logistics_no
                or str(row.logistics_state or "").upper()
                not in _QUERYABLE_LOGISTICS_STATES
                or str(row.erp_state or "").upper() == "DONE"
                or (
                    row.identity_state
                    and str(row.identity_state).upper() != "ACTIVE"
                )
            ):
                continue
            next_attempt_text = str(
                row.logistics_next_attempt_at or ""
            ).strip()
            if next_attempt_text:
                try:
                    next_attempt = datetime.fromisoformat(
                        next_attempt_text.replace("Z", "+00:00")
                    )
                    if next_attempt.tzinfo is None:
                        next_attempt = next_attempt.replace(tzinfo=timezone.utc)
                    if next_attempt > now:
                        continue
                except ValueError:
                    continue
            candidates.append((next_attempt_text, logistics_no))
        if not candidates:
            return ALIBABA_SCM_HOME_URL
        candidates.sort(key=lambda item: (bool(item[0]), item[0], item[1]))
        try:
            return logistics_detail_url(candidates[0][1])
        except (TypeError, ValueError):
            return ALIBABA_SCM_HOME_URL

    def _rpc_request_timeout(
        self,
        method: str,
        args: tuple[Any, ...],
    ) -> httpx.Timeout | None:
        """Allow real notification delivery to finish without slowing other RPCs."""

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
                self._timeout_seconds,
                _NOTIFICATION_SEND_TIMEOUT_OVERHEAD_SECONDS
                + max(1, item_count)
                * _NOTIFICATION_SEND_TIMEOUT_PER_ITEM_SECONDS,
            ),
        )
        return httpx.Timeout(
            self._timeout_seconds,
            connect=self._timeout_seconds,
            read=read_timeout,
            write=self._timeout_seconds,
            pool=self._timeout_seconds,
        )

    def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        request_id = uuid4().hex
        with self._lock:
            try:
                if bool(getattr(self, "_authentication_required", False)):
                    if method in MUTATION_METHODS:
                        return self._authentication_result()
                    raise CoordinationAuthenticationRequired(
                        self._authentication_error
                    )
                fallback_error = self._start_browser_for_approved_fallback(
                    method,
                    args,
                )
                if fallback_error is not None:
                    return fallback_error
                if (
                    method == "submit_task"
                    and args
                    and isinstance(args[0], TaskCommand)
                ):
                    command = args[0]
                    requires_browser = task_requires_visible_browser(command)
                    prewarms_logistics = self._prewarms_local_logistics(
                        command
                    )
                    if requires_browser:
                        if (
                            self._browser_host is None
                            or not self.browser_endpoint
                        ):
                            return ControlResult(
                                False,
                                "当前桌面没有配置本机 Chrome 通道，网页任务未提交。",
                            )
                        self._browser_host.ensure_started()
                    elif (
                        prewarms_logistics
                        and self._browser_host is not None
                        and self.browser_endpoint
                    ):
                        try:
                            self._browser_host.open_url(
                                self._logistics_prewarm_url()
                            )
                        except LocalBrowserUnavailable:
                            # The API scan remains valid even when Chrome cannot
                            # be prewarmed. Its follow-up will keep queue rows
                            # pending and report the browser problem explicitly.
                            pass
                request_options: dict[str, Any] = {
                    "json": {
                        "instance_id": self.instance_id,
                        "request_id": request_id,
                        "method": method,
                        "args": to_jsonable(list(args)),
                        "kwargs": to_jsonable(kwargs),
                        "expected_revision": self._revision,
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
                self._revision = int(payload.get("revision") or self._revision)
                result_type = str(payload.get("result_type") or "json")
                result = payload.get("result")
                if result_type == "control_result":
                    control_result = decode_control_result(result)
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
                if result_type == "log_page":
                    return decode_log_page(result)
                if result_type == "interactions":
                    return decode_interactions(result)
                if method == "full_log_text" and isinstance(result, list):
                    return tuple(str(item) for item in result[:2])
                return result
            except (
                CoordinationConnectionError,
                LocalBrowserUnavailable,
                TypeError,
                ValueError,
            ) as exc:
                message = str(exc)
                if method in MUTATION_METHODS:
                    return (
                        self._authentication_result()
                        if isinstance(exc, CoordinationAuthenticationRequired)
                        else ControlResult(False, message)
                    )
                if method == "pending_interactions":
                    return ()
                if method == "list_shipment_notifications":
                    return []
                if method == "full_log_text":
                    return ("完整日志", message)
                if method == "log_directory":
                    return ""
                if method == "list_log_entries":
                    return LogPage()
                raise

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
                self._revision = int(
                    payload.get("revision") or self._revision
                )
                return ControlResult(
                    True,
                    f"加密设置已导出到当前电脑：{target}",
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
                self._revision = int(
                    payload.get("revision") or self._revision
                )
                return decode_control_result(payload.get("result"))
            except (CoordinationConnectionError, ValueError) as exc:
                if isinstance(exc, CoordinationAuthenticationRequired):
                    return self._authentication_result()
                return ControlResult(False, f"导入加密设置失败：{exc}")

    def prepare_close(self) -> ControlResult:
        """Detach only this window; server-owned work continues for other users."""

        with self._lock:
            if self._closed:
                return ControlResult(
                    True,
                    "当前窗口已退出；服务器后台任务继续运行。",
                )
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
            self._client.close()
            if self._browser_host is not None:
                self._browser_host.close()
        return ControlResult(True, "当前窗口已退出；服务器后台任务继续运行。")

    def __getattr__(self, name: str) -> Any:
        if name not in RPC_METHODS:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return self._rpc(name, *args, **kwargs)

        return call

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(READ_METHODS) | set(MUTATION_METHODS))


def _install_rpc_methods() -> None:
    """Materialize protocol methods for static/runtime structural checks."""

    def make_method(method_name: str):
        def rpc_method(self: RemoteBackgroundTaskController, *args: Any, **kwargs: Any) -> Any:
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
