"""Desktop-side proxy for the authoritative shared controller."""

from __future__ import annotations

import os
import socket
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from erp_automation.ui.controller import ControlResult
from erp_automation.ui.models import DesktopSnapshot, LogPage

from .codec import (
    decode_control_result,
    decode_interactions,
    decode_log_page,
    decode_snapshot,
    to_jsonable,
)
from .service import MUTATION_METHODS, READ_METHODS, RPC_METHODS


class CoordinationConnectionError(RuntimeError):
    """The shared controller could not be reached or authenticated."""


class RemoteBackgroundTaskController:
    """Implement the complete desktop controller protocol over authenticated HTTP."""

    def __init__(
        self,
        server_url: str,
        *,
        token: str,
        ca_file: str | Path | None = None,
        display_name: str = "",
        timeout_seconds: float = 30.0,
        instance_id: str | None = None,
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
        self._client = httpx.Client(
            base_url=normalized_url,
            headers={
                "Authorization": f"Bearer {normalized_token}",
                "Accept": "application/json",
                "User-Agent": "lingxing-erp-desktop-coordination/1",
            },
            timeout=max(3.0, float(timeout_seconds)),
            verify=verify,
        )
        self._lock = threading.RLock()
        self._last_snapshot = DesktopSnapshot(
            backend_message="正在连接共享 ERP 后台…"
        )
        self._revision = 0
        self._last_error = ""
        try:
            payload = self._request(
                "POST",
                "/v1/instances/register",
                json={
                    "instance_id": self.instance_id,
                    "display_name": self.display_name,
                },
            )
            self._revision = int(payload.get("revision") or 0)
        except CoordinationConnectionError as exc:
            self._last_error = str(exc)

    @property
    def revision(self) -> int:
        return self._revision

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
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
                payload = self._request(
                    "GET",
                    "/v1/snapshot",
                    headers={"X-ERP-Instance-ID": self.instance_id},
                )
                snapshot = decode_snapshot(payload.get("snapshot"))
                self._revision = int(payload.get("revision") or self._revision)
                self._last_snapshot = snapshot
                self._last_error = ""
                return deepcopy(snapshot)
            except (CoordinationConnectionError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                stale = deepcopy(self._last_snapshot)
                stale.backend_message = (
                    f"共享 ERP 后台暂时不可用；当前显示最近一次数据。{self._last_error}"
                )
                return stale

    def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        request_id = uuid4().hex
        with self._lock:
            try:
                payload = self._request(
                    "POST",
                    "/v1/rpc",
                    json={
                        "instance_id": self.instance_id,
                        "request_id": request_id,
                        "method": method,
                        "args": to_jsonable(list(args)),
                        "kwargs": to_jsonable(kwargs),
                        "expected_revision": self._revision,
                    },
                )
                self._revision = int(payload.get("revision") or self._revision)
                result_type = str(payload.get("result_type") or "json")
                result = payload.get("result")
                if result_type == "control_result":
                    return decode_control_result(result)
                if result_type == "log_page":
                    return decode_log_page(result)
                if result_type == "interactions":
                    return decode_interactions(result)
                if method == "full_log_text" and isinstance(result, list):
                    return tuple(str(item) for item in result[:2])
                return result
            except (CoordinationConnectionError, TypeError, ValueError) as exc:
                message = str(exc)
                if method in MUTATION_METHODS:
                    return ControlResult(False, message)
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

    def prepare_close(self) -> ControlResult:
        """Detach only this window; server-owned work continues for other users."""

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
