"""Command-line entry point for the authoritative Linux coordination service."""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import os
import re
import shutil
import signal
import threading
from pathlib import Path

from erp_automation.app import create_default_controller
from erp_automation.configuration import EncryptedConfigurationStore, HostKeyAesGcmBackend

from .access import CloudflareAccessVerifier, OperatorIdentity
from .http_server import create_http_server
from .service import CoordinatedControllerService
from .store import CoordinationStore

_BOOTSTRAP_OPERATOR_EMAIL_PATTERN = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@billyprint\.com$"
)



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


def _read_required_client_version() -> str:
    value = str(os.environ.get("ERP_REQUIRED_CLIENT_VERSION") or "").strip()
    file_name = str(
        os.environ.get("ERP_REQUIRED_CLIENT_VERSION_FILE") or ""
    ).strip()
    if value and file_name:
        raise ValueError(
            "Required client version must be configured as a value or file, not both."
        )
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    if value and not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", value):
        raise ValueError("Required client version is invalid.")
    return value


def _read_client_rollout_grace_seconds() -> int:
    raw_value = str(
        os.environ.get("ERP_CLIENT_ROLLOUT_GRACE_SECONDS") or "900"
    ).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "ERP_CLIENT_ROLLOUT_GRACE_SECONDS must be an integer."
        ) from exc
    if value < 0 or value > 86_400:
        raise ValueError(
            "ERP_CLIENT_ROLLOUT_GRACE_SECONDS must be between 0 and 86400."
        )
    return value


def _read_optional_rollout_file(file_name: str) -> str:
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError):
        # Rollout metadata is deliberately optional. A failed or interrupted
        # deployment can restore a service definition from before these
        # markers existed, so absence must mean "no rollout" rather than
        # preventing the coordinator from starting.
        return ""


def _read_client_rollout_grace_deadline_epoch() -> int:
    value = str(
        os.environ.get("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_EPOCH") or ""
    ).strip()
    file_name = str(
        os.environ.get("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_FILE") or ""
    ).strip()
    if value and file_name:
        raise ValueError(
            "Client rollout grace deadline must be configured as a value or "
            "file, not both."
        )
    if file_name:
        value = _read_optional_rollout_file(file_name)
    if not value or value == "pending":
        return 0
    try:
        deadline = int(value)
    except ValueError as exc:
        raise ValueError(
            "Client rollout grace deadline must be a Unix epoch integer."
        ) from exc
    if deadline < 0:
        raise ValueError(
            "Client rollout grace deadline must be a non-negative epoch."
        )
    return deadline


def _read_client_rollout_pending_activation() -> bool:
    value = str(
        os.environ.get("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_EPOCH") or ""
    ).strip()
    file_name = str(
        os.environ.get("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_FILE") or ""
    ).strip()
    if value and file_name:
        raise ValueError(
            "Client rollout grace deadline must be configured as a value or "
            "file, not both."
        )
    if file_name:
        value = _read_optional_rollout_file(file_name)
    return value == "pending"


def _read_rollout_previous_client_version() -> str:
    value = str(
        os.environ.get("ERP_ROLLOUT_PREVIOUS_CLIENT_VERSION") or ""
    ).strip()
    file_name = str(
        os.environ.get("ERP_ROLLOUT_PREVIOUS_CLIENT_VERSION_FILE") or ""
    ).strip()
    if value and file_name:
        raise ValueError(
            "Rollout previous client version must be configured as a value "
            "or file, not both."
        )
    if file_name:
        value = _read_optional_rollout_file(file_name)
    if value and not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", value):
        raise ValueError("Rollout previous client version is invalid.")
    return value


def _environment_enabled(name: str, *, default: bool = False) -> bool:
    value = str(os.environ.get(name) or "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _bootstrap_legacy_operator_config(
    *,
    operator_config_root: Path,
    legacy_config_path: Path,
    operator_email: str,
) -> Path:
    normalized_email = str(operator_email or "").strip().casefold()
    if not _BOOTSTRAP_OPERATOR_EMAIL_PATTERN.fullmatch(normalized_email):
        raise ValueError(
            "Bootstrap operator email must be a @billyprint.com address."
        )

    owner_digest = hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()
    config_path = operator_config_root / f"{owner_digest}.enc"
    owner_marker = operator_config_root / ".legacy-config-owner.sha256"

    if not config_path.exists() and not legacy_config_path.is_file():
        return config_path

    if owner_marker.is_file():
        recorded_owner = owner_marker.read_text(encoding="ascii").strip().casefold()
        if recorded_owner != owner_digest:
            raise RuntimeError(
                "Legacy configuration is already assigned to another operator."
            )
    else:
        temporary_marker = owner_marker.with_name(
            f"{owner_marker.name}.{os.getpid()}.tmp"
        )
        try:
            temporary_marker.write_text(owner_digest + "\n", encoding="ascii")
            os.replace(temporary_marker, owner_marker)
        finally:
            temporary_marker.unlink(missing_ok=True)

    if not config_path.exists():
        temporary_config = config_path.with_name(
            f".{config_path.name}.{os.getpid()}.tmp"
        )
        try:
            shutil.copyfile(legacy_config_path, temporary_config)
            os.replace(temporary_config, config_path)
        finally:
            temporary_config.unlink(missing_ok=True)
    return config_path


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
    # httpx logs full request URLs at INFO. Some third-party APIs put credentials
    # in their query string, so never send those URLs to the service journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    api_token = _read_required_secret(
        environment_name="ERP_COORDINATION_TOKEN",
        file_environment_name="ERP_COORDINATION_TOKEN_FILE",
        label="Coordination API token",
    )
    host_backend = HostKeyAesGcmBackend(_load_host_key())
    require_cloudflare = _environment_enabled(
        "ERP_REQUIRE_CLOUDFLARE_ACCESS",
        default=True,
    )
    access_verifier = None
    if require_cloudflare:
        bundled_certificates = (
            Path(__file__).resolve().parents[2]
            / "deploy"
            / "server"
            / "cloudflare-access-jwks.json"
        )
        access_verifier = CloudflareAccessVerifier(
            team_domain=_read_required_secret(
                environment_name="ERP_CLOUDFLARE_ACCESS_TEAM_DOMAIN",
                file_environment_name="ERP_CLOUDFLARE_ACCESS_TEAM_DOMAIN_FILE",
                label="Cloudflare Access team domain",
            ),
            audience=_read_required_secret(
                environment_name="ERP_CLOUDFLARE_ACCESS_AUDIENCE",
                file_environment_name="ERP_CLOUDFLARE_ACCESS_AUDIENCE_FILE",
                label="Cloudflare Access application audience",
            ),
            allowed_email_domain=str(
                os.environ.get("ERP_CLOUDFLARE_ALLOWED_EMAIL_DOMAIN")
                or "billyprint.com"
            ),
            certificate_cache_path=(
                workspace / "data" / "cloudflare-access-jwks.json"
            ),
            bootstrap_certificates_path=bundled_certificates,
        )
        access_verifier.prepare()

    operator_config_root = workspace / "data" / "operator-config"
    operator_config_root.mkdir(parents=True, exist_ok=True)
    bootstrap_operator_email = str(
        os.environ.get("ERP_BOOTSTRAP_OPERATOR_EMAIL") or ""
    ).strip().casefold()
    bootstrap_operator_email_file = str(
        os.environ.get("ERP_BOOTSTRAP_OPERATOR_EMAIL_FILE") or ""
    ).strip()
    if bootstrap_operator_email and bootstrap_operator_email_file:
        raise ValueError(
            "Bootstrap operator email must be configured as a value or file, not both."
        )
    if bootstrap_operator_email_file:
        bootstrap_operator_email = (
            Path(bootstrap_operator_email_file)
            .read_text(encoding="utf-8")
            .strip()
            .casefold()
        )
    if bootstrap_operator_email and not _BOOTSTRAP_OPERATOR_EMAIL_PATTERN.fullmatch(
        bootstrap_operator_email
    ):
        raise ValueError(
            "Bootstrap operator email must be a @billyprint.com address."
        )
    legacy_config_path = workspace / "data" / "config.enc"
    factory_lock = threading.RLock()

    def create_operator_controller(
        identity: OperatorIdentity,
    ):
        normalized_email = identity.email.casefold()
        digest = hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()
        config_path = operator_config_root / f"{digest}.enc"
        if (
            bootstrap_operator_email
            and normalized_email == bootstrap_operator_email
        ):
            with factory_lock:
                config_path = _bootstrap_legacy_operator_config(
                    operator_config_root=operator_config_root,
                    legacy_config_path=legacy_config_path,
                    operator_email=normalized_email,
                )
        return create_default_controller(
            workspace,
            config_store=EncryptedConfigurationStore(
                config_path,
                backend=host_backend,
            ),
        )

    controller = (
        None
        if require_cloudflare
        else create_default_controller(
            workspace,
            config_store=EncryptedConfigurationStore(
                legacy_config_path,
                backend=host_backend,
            ),
        )
    )
    coordination_store = CoordinationStore(
        workspace / "data" / "coordination.sqlite3"
    )
    service = CoordinatedControllerService(
        controller,
        coordination_store,
        required_client_version=_read_required_client_version(),
        rollout_previous_client_version=(
            _read_rollout_previous_client_version()
        ),
        client_rollout_grace_seconds=_read_client_rollout_grace_seconds(),
        client_rollout_grace_deadline_epoch=(
            _read_client_rollout_grace_deadline_epoch()
        ),
        client_rollout_pending_activation=(
            _read_client_rollout_pending_activation()
        ),
        controller_factory=(
            create_operator_controller if require_cloudflare else None
        ),
    )
    server = create_http_server(
        (args.bind, args.port),
        service,
        api_token=api_token,
        access_verifier=access_verifier,
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
        if controller is not None:
            prepare_result = controller.prepare_close()
            if not prepare_result.accepted:
                logging.getLogger(__name__).warning(prepare_result.message)
            controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
