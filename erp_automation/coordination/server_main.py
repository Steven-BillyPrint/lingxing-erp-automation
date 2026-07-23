"""Command-line entry point for the authoritative Linux coordination service."""

from __future__ import annotations

import argparse
import base64
import logging
import os
import signal
import threading
from pathlib import Path

from erp_automation.app import create_default_controller
from erp_automation.configuration import EncryptedConfigurationStore, HostKeyAesGcmBackend

from .http_server import create_http_server
from .service import CoordinatedControllerService
from .store import CoordinationStore


def _read_required_secret(
    *,
    environment_name: str,
    file_environment_name: str,
    label: str,
) -> str:
    value = str(os.environ.get(environment_name) or "").strip()
    file_name = str(os.environ.get(file_environment_name) or "").strip()
    if value and file_name:
        raise ValueError(f"{label} must be configured as a value or file, not both.")
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{label} is not configured.")
    return value


def _load_host_key() -> bytes:
    encoded = _read_required_secret(
        environment_name="ERP_AUTOMATION_HOST_KEY",
        file_environment_name="ERP_AUTOMATION_HOST_KEY_FILE",
        label="Host encryption key",
    )
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("Host encryption key must be valid base64.") from exc
    if len(key) != 32:
        raise ValueError("Host encryption key must decode to exactly 32 bytes.")
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("ERP_AUTOMATION_HOME", ""),
        help="Server application workspace.",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("ERP_COORDINATION_BIND", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ERP_COORDINATION_PORT", "18765")),
    )
    parser.add_argument(
        "--certificate",
        default=os.environ.get("ERP_COORDINATION_CERT_FILE", ""),
    )
    parser.add_argument(
        "--private-key",
        default=os.environ.get("ERP_COORDINATION_PRIVATE_KEY_FILE", ""),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace or ".").expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    api_token = _read_required_secret(
        environment_name="ERP_COORDINATION_TOKEN",
        file_environment_name="ERP_COORDINATION_TOKEN_FILE",
        label="Coordination API token",
    )
    config_store = EncryptedConfigurationStore(
        workspace / "data" / "config.enc",
        backend=HostKeyAesGcmBackend(_load_host_key()),
    )
    controller = create_default_controller(
        workspace,
        config_store=config_store,
    )
    coordination_store = CoordinationStore(
        workspace / "data" / "coordination.sqlite3"
    )
    service = CoordinatedControllerService(controller, coordination_store)
    server = create_http_server(
        (args.bind, args.port),
        service,
        api_token=api_token,
        certificate_file=args.certificate or None,
        private_key_file=args.private_key or None,
    )
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        if not stop_requested.is_set():
            stop_requested.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        service.close()
        prepare_result = controller.prepare_close()
        if not prepare_result.accepted:
            logging.getLogger(__name__).warning(prepare_result.message)
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
