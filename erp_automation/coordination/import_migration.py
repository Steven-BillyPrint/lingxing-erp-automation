"""Import an encrypted desktop migration package into the server workspace."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from erp_automation.app import create_default_controller
from erp_automation.configuration import EncryptedConfigurationStore, HostKeyAesGcmBackend

from .server_main import _load_host_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("ERP_AUTOMATION_HOME", ""),
        help="Server application workspace.",
    )
    parser.add_argument("--package", required=True, help="Encrypted migration package.")
    parser.add_argument(
        "--passphrase-file",
        required=True,
        help="Root-owned file containing the one-time migration passphrase.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace or ".").expanduser().resolve()
    package_path = Path(args.package).expanduser().resolve()
    passphrase_path = Path(args.passphrase_file).expanduser().resolve()
    passphrase = passphrase_path.read_text(encoding="utf-8").strip()

    config_store = EncryptedConfigurationStore(
        workspace / "data" / "config.enc",
        backend=HostKeyAesGcmBackend(_load_host_key()),
    )
    controller = create_default_controller(workspace, config_store=config_store)
    try:
        result = controller.import_portable_migration(
            str(package_path),
            passphrase,
            overwrite=True,
        )
    finally:
        controller.close()

    print(f"import_accepted={result.accepted}")
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
