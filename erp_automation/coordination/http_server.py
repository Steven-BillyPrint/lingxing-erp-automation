"""Minimal authenticated HTTP transport for the coordination service."""

from __future__ import annotations

import gzip
import hmac
import json
import logging
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .access import (
    CloudflareAccessError,
    CloudflareAccessUnavailableError,
    CloudflareAccessVerifier,
    OperatorIdentity,
)
from .codec import to_jsonable
from .service import (
    ROLLING_UPDATE_DRAIN_RPC_METHODS,
    ClientUpdateRequiredError,
    CoordinatedControllerService,
    InstanceRegistrationExpiredError,
)


LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 8 * 1024 * 1024


class CoordinationHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: CoordinatedControllerService,
        *,
        api_token: str,
        access_verifier: CloudflareAccessVerifier | None = None,
    ) -> None:
        super().__init__(server_address, CoordinationRequestHandler)
        self.coordination_service = service
        self.api_token = api_token
        self.access_verifier = access_verifier

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self.access_verifier is not None:
                self.access_verifier.close()


class CoordinationRequestHandler(BaseHTTPRequestHandler):
    server: CoordinationHttpServer
    protocol_version = "HTTP/1.1"
    _operator_identity: OperatorIdentity | None = None

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], format % args)

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        accepts_gzip = "gzip" in {
            item.split(";", 1)[0].strip().casefold()
            for item in str(self.headers.get("Accept-Encoding") or "").split(",")
        }
        compressed = len(encoded) >= 1024 and accepts_gzip
        body = gzip.compress(encoded, compresslevel=1) if compressed else encoded
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_instance_registration_expired(
        self,
        error: InstanceRegistrationExpiredError,
    ) -> None:
        self._send(
            HTTPStatus.CONFLICT,
            {
                "ok": False,
                "error": "instance_registration_expired",
                "message": str(error),
            },
        )

    def _shared_token_authenticated(self) -> bool:
        authorization = str(self.headers.get("Authorization") or "")
        scheme, separator, provided = authorization.partition(" ")
        return (
            bool(separator)
            and scheme.casefold() == "bearer"
            and hmac.compare_digest(provided.strip(), self.server.api_token)
        )

    def _discard_bounded_request_body(self) -> None:
        """Drain a small rejected request so Windows can close it gracefully."""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return
        if 0 < length <= MAX_REQUEST_BYTES:
            self.rfile.read(length)

    def _require_authentication(self) -> bool:
        if not self._shared_token_authenticated():
            self._discard_bounded_request_body()
            self.close_connection = True
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "Authentication required."},
            )
            return False
        verifier = self.server.access_verifier
        if verifier is None:
            self._operator_identity = None
            return True
        access_token = str(
            self.headers.get("Cf-Access-Jwt-Assertion")
            or self.headers.get("Cf-Access-Token")
            or ""
        ).strip()
        try:
            self._operator_identity = verifier.verify(access_token)
            return True
        except CloudflareAccessUnavailableError as exc:
            LOGGER.error(
                "Cloudflare Access verification is unavailable for %s: %s",
                self.client_address[0],
                exc,
            )
            self._discard_bounded_request_body()
            self.close_connection = True
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "error": "access_verification_unavailable",
                    "message": (
                        "The server cannot currently verify company login. "
                        "Please retry shortly."
                    ),
                },
            )
            return False
        except CloudflareAccessError as exc:
            LOGGER.warning(
                "Rejected Cloudflare Access identity from %s: %s",
                self.client_address[0],
                exc,
            )
        self._discard_bounded_request_body()
        self.close_connection = True
        self._send(
            HTTPStatus.UNAUTHORIZED,
            {
                "ok": False,
                "error": "Cloudflare company email login is required.",
            },
        )
        return False

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ValueError("Content-Length is invalid.") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body size is invalid.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Request JSON must be an object.")
        return payload

    def _client_version(self) -> str:
        return str(self.headers.get("X-ERP-Client-Version") or "").strip()

    def _send_client_update_required(
        self,
        error: ClientUpdateRequiredError,
    ) -> None:
        self._send(
            HTTPStatus.UPGRADE_REQUIRED,
            {
                "ok": False,
                "error": "client_update_required",
                "required_version": error.required_version,
                "manifest_url": (
                    "https://github.com/Steven-BillyPrint/"
                    "lingxing-erp-automation/releases/latest/download/latest.json"
                ),
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            verifier = self.server.access_verifier
            access_ready = verifier is None or verifier.ready
            status = "healthy" if access_ready else "degraded"
            self._send(
                HTTPStatus.OK if access_ready else HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": access_ready,
                    "status": status,
                    "access_verification_ready": access_ready,
                    "required_client_version": (
                        self.server.coordination_service.required_client_version
                    ),
                    "rollout_previous_client_version": (
                        self.server.coordination_service
                        .rollout_previous_client_version
                    ),
                    "client_rollout_pending_activation": (
                        self.server.coordination_service
                        .client_rollout_pending_activation
                    ),
                    "client_rollout_grace_remaining_seconds": (
                        self.server.coordination_service
                        .client_rollout_grace_remaining_seconds
                    ),
                    "client_rollout_grace_deadline_epoch": (
                        self.server.coordination_service
                        .client_rollout_grace_deadline_epoch
                    ),
                },
            )
            return
        if not self._require_authentication():
            return
        if path != "/v1/snapshot":
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found."})
            return
        instance_id = str(self.headers.get("X-ERP-Instance-ID") or "").strip()
        if not instance_id:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "X-ERP-Instance-ID is required."},
            )
            return
        try:
            update_deferred = (
                self.server.coordination_service.authorize_client_request(
                    self._client_version(),
                    instance_id=instance_id,
                    allow_active_task_drain=True,
                )
            )
            query = parse_qs(parsed.query)
            raw_known_revision = str(
                (query.get("known_revision") or [""])[0]
            ).strip()
            known_revision = (
                int(raw_known_revision)
                if raw_known_revision
                else None
            )
            if known_revision is not None and known_revision < 0:
                raise ValueError("known_revision must not be negative.")
            snapshot_mode = str(
                (query.get("snapshot_mode") or [""])[0]
            ).strip()
            if snapshot_mode not in {"", "summary_v1"}:
                raise ValueError("Unsupported snapshot_mode.")
            payload = self.server.coordination_service.snapshot_payload(
                instance_id,
                known_revision=known_revision,
                summary_only=snapshot_mode == "summary_v1",
                identity=self._operator_identity,
            )
            payload["client_update_deferred"] = update_deferred
            payload["required_version"] = (
                self.server.coordination_service.required_client_version
            )
            self._send(HTTPStatus.OK, {"ok": True, **payload})
        except ClientUpdateRequiredError as exc:
            self._send_client_update_required(exc)
        except InstanceRegistrationExpiredError as exc:
            self._send_instance_registration_expired(exc)
        except (KeyError, TypeError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:
            LOGGER.exception("Snapshot request failed")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "Shared controller snapshot failed."},
            )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/v1/safety/pause":
            if not self._shared_token_authenticated():
                self._discard_bounded_request_body()
                self.close_connection = True
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "Authentication required."},
                )
                return
            try:
                payload = self._read_json()
                result = self.server.coordination_service.activate_fail_safe_pause(
                    str(
                        payload.get("instance_id")
                        or self.headers.get("X-ERP-Instance-ID")
                        or ""
                    ),
                    str(payload.get("reason") or "")
                )
                self._send(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "result_type": "control_result",
                        "result": to_jsonable(result),
                    },
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
            except Exception:
                LOGGER.exception("Fail-safe pause request failed")
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "Fail-safe pause failed."},
                )
            return
        if not self._require_authentication():
            return
        try:
            payload = self._read_json()
            if path == "/v1/instances/register":
                result = self.server.coordination_service.register(
                    str(payload.get("instance_id") or ""),
                    str(payload.get("display_name") or ""),
                    str(payload.get("browser_endpoint") or ""),
                    str(payload.get("client_version") or ""),
                    logistics_browser_endpoint=str(
                        payload.get("logistics_browser_endpoint") or ""
                    ),
                    identity=self._operator_identity,
                )
            elif path == "/v1/instances/browser-endpoint":
                result = self.server.coordination_service.allocate_browser_endpoint(
                    str(payload.get("instance_id") or ""),
                    str(payload.get("display_name") or ""),
                    str(payload.get("client_version") or ""),
                    identity=self._operator_identity,
                )
            elif path == "/v1/instances/heartbeat":
                instance_id = str(payload.get("instance_id") or "")
                update_deferred = (
                    self.server.coordination_service.authorize_client_request(
                        self._client_version(),
                        instance_id=instance_id,
                        allow_active_task_drain=True,
                    )
                )
                result = self.server.coordination_service.heartbeat(
                    instance_id,
                    identity=self._operator_identity,
                )
                result["client_update_deferred"] = update_deferred
                result["required_version"] = (
                    self.server.coordination_service.required_client_version
                )
            elif path == "/v1/instances/deregister":
                self.server.coordination_service.deregister(
                    str(payload.get("instance_id") or ""),
                    identity=self._operator_identity,
                )
                result = {}
            elif path == "/v1/configuration/export":
                self.server.coordination_service.authorize_client_request(
                    self._client_version(),
                    instance_id=str(payload.get("instance_id") or ""),
                )
                result = (
                    self.server.coordination_service.export_portable_configuration(
                        instance_id=str(payload.get("instance_id") or ""),
                        request_id=str(payload.get("request_id") or ""),
                        passphrase=str(payload.get("passphrase") or ""),
                        identity=self._operator_identity,
                    )
                )
            elif path == "/v1/configuration/import":
                self.server.coordination_service.authorize_client_request(
                    self._client_version(),
                    instance_id=str(payload.get("instance_id") or ""),
                )
                result = (
                    self.server.coordination_service.import_portable_configuration(
                        instance_id=str(payload.get("instance_id") or ""),
                        request_id=str(payload.get("request_id") or ""),
                        passphrase=str(payload.get("passphrase") or ""),
                        package_base64=str(payload.get("package_base64") or ""),
                        identity=self._operator_identity,
                    )
                )
            elif path == "/v1/rpc":
                instance_id = str(payload.get("instance_id") or "")
                method = str(payload.get("method") or "")
                raw_args = payload.get("args")
                drain_method_allowed = (
                    method in ROLLING_UPDATE_DRAIN_RPC_METHODS
                    and (
                        method
                        not in {
                            "set_emergency_stop_writes",
                            "set_execution_paused",
                        }
                        or (
                            isinstance(raw_args, list)
                            and len(raw_args) >= 1
                            and raw_args[0] is True
                        )
                    )
                )
                update_deferred = (
                    self.server.coordination_service.authorize_client_request(
                        self._client_version(),
                        instance_id=instance_id,
                        allow_active_task_drain=drain_method_allowed,
                    )
                )
                result = self.server.coordination_service.invoke(
                    instance_id=instance_id,
                    request_id=str(payload.get("request_id") or ""),
                    method=method,
                    raw_args=raw_args,
                    raw_kwargs=payload.get("kwargs"),
                    identity=self._operator_identity,
                )
                result["client_update_deferred"] = update_deferred
                result["required_version"] = (
                    self.server.coordination_service.required_client_version
                )
            else:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "Not found."},
                )
                return
            self._send(HTTPStatus.OK, {"ok": True, **result})
        except ClientUpdateRequiredError as exc:
            self._send_client_update_required(exc)
        except InstanceRegistrationExpiredError as exc:
            self._send_instance_registration_expired(exc)
        except (KeyError, TypeError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:
            LOGGER.exception("Coordination request failed: %s", path)
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "Shared controller operation failed."},
            )


def create_http_server(
    address: tuple[str, int],
    service: CoordinatedControllerService,
    *,
    api_token: str,
    access_verifier: CloudflareAccessVerifier | None = None,
    certificate_file: str | None = None,
    private_key_file: str | None = None,
) -> CoordinationHttpServer:
    if len(api_token) < 32:
        raise ValueError("Coordination API token must contain at least 32 characters.")
    server = CoordinationHttpServer(
        address,
        service,
        api_token=api_token,
        access_verifier=access_verifier,
    )
    if bool(certificate_file) != bool(private_key_file):
        server.server_close()
        raise ValueError("TLS certificate and private key must be configured together.")
    if certificate_file and private_key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate_file, private_key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
