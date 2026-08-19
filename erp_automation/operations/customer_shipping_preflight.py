"""Read-only live preflight for Lingxing customer-shipping fields."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from erp_automation.application.api_scanners import (
    customer_shipping_service_evidence_from_payload,
)
from erp_automation.configuration import (
    EncryptedConfigurationStore,
    HostKeyAesGcmBackend,
)
from erp_automation.integrations.lingxing.runtime import (
    create_lingxing_openapi_client,
)
from shipment_automation.models import (
    CUSTOMER_SHIPPING_EXPEDITED,
    CUSTOMER_SHIPPING_STANDARD,
    normalize_customer_shipping_service,
)


_AMAZON_PLATFORM_ORDER_RE = re.compile(
    r"^\d{3}-\d{7}-\d{7}(?:-\d+)?$"
)
_CANONICAL_SERVICES = {
    CUSTOMER_SHIPPING_STANDARD,
    CUSTOMER_SHIPPING_EXPEDITED,
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _mappings(value: object) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []

    def visit(current: object) -> None:
        if isinstance(current, Mapping):
            output.append(current)
            for child in current.values():
                visit(child)
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            for child in current:
                visit(child)

    visit(value)
    return tuple(output)


def _first_scalar(
    payload: Mapping[str, Any],
    names: frozenset[str],
) -> str:
    for mapping in _mappings(payload):
        for key, value in mapping.items():
            if str(key).casefold() not in names:
                continue
            if isinstance(value, (Mapping, list, tuple, set)):
                continue
            text = _text(value)
            if text:
                return text
    return ""


def _structured_text(value: object) -> str:
    if isinstance(value, Mapping):
        return " | ".join(
            text
            for child in value.values()
            if (text := _structured_text(child))
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return " | ".join(
            text for child in value if (text := _structured_text(child))
        )
    return _text(value)


def _list_customer_shipping(payload: Mapping[str, Any]) -> tuple[bool, str]:
    for mapping in _mappings(payload):
        if "customer_shipping_list" in mapping:
            return True, _structured_text(mapping["customer_shipping_list"])
    return False, ""


def _response_rows(data: object) -> tuple[Mapping[str, Any], ...]:
    raw_rows = data.get("list") if isinstance(data, Mapping) else data
    if not isinstance(raw_rows, list):
        return ()
    return tuple(row for row in raw_rows if isinstance(row, Mapping))


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    system_order_no = _first_scalar(
        row,
        frozenset({"global_order_no", "system_order_no"}),
    )
    platform_order_no = _first_scalar(
        row,
        frozenset(
            {
                "platform_order_no",
                "platform_order_id",
                "platform_order_name",
            }
        ),
    )
    return system_order_no, platform_order_no


def _is_amazon_row(row: Mapping[str, Any], platform_order_no: str) -> bool:
    platform_code = _first_scalar(
        row,
        frozenset({"platform_code", "platform_id"}),
    ).casefold()
    if platform_code:
        return platform_code in {"10001", "amazon"}
    platform_name = _first_scalar(
        row,
        frozenset({"platform", "platform_name"}),
    ).casefold()
    if platform_name:
        return platform_name == "amazon"
    return bool(_AMAZON_PLATFORM_ORDER_RE.fullmatch(platform_order_no))


async def probe_customer_shipping_fields(
    client: Any,
    *,
    now_epoch: int | None = None,
    maximum_detail_attempts: int = 20,
    randomizer: random.Random | random.SystemRandom | None = None,
) -> Mapping[str, Any]:
    """Prove both documented fields against random live Amazon orders."""

    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    response = await client.list_orders(
        offset=0,
        length=100,
        platform_code=[10001],
        date_type="update_time",
        start_time=now_value - 30 * 24 * 60 * 60,
        end_time=now_value + 1,
    )
    candidates: list[tuple[str, str, str, Mapping[str, Any]]] = []
    for row in _response_rows(response.data):
        system_order_no, platform_order_no = _row_identity(row)
        present, raw_service = _list_customer_shipping(row)
        service = normalize_customer_shipping_service(raw_service)
        if (
            system_order_no
            and platform_order_no
            and present
            and service in _CANONICAL_SERVICES
            and _is_amazon_row(row, platform_order_no)
        ):
            candidates.append(
                (system_order_no, platform_order_no, service, row)
            )
    if not candidates:
        raise RuntimeError(
            "Live order list returned no Amazon row with a canonical "
            "customer_shipping_list value."
        )

    chooser = randomizer or random.SystemRandom()
    chooser.shuffle(candidates)
    detail_failures = 0
    for system_order_no, platform_order_no, list_service, _row in candidates[
        :maximum_detail_attempts
    ]:
        try:
            detail = await client.get_fbm_order_detail(system_order_no)
        except Exception:
            detail_failures += 1
            await asyncio.sleep(1.1)
            continue
        if not isinstance(detail.data, Mapping):
            detail_failures += 1
            await asyncio.sleep(1.1)
            continue
        present, raw_service, field_name = (
            customer_shipping_service_evidence_from_payload(
                detail.data,
                platform_order_no=platform_order_no,
            )
        )
        detail_service = normalize_customer_shipping_service(raw_service)
        if not present or detail_service not in _CANONICAL_SERVICES:
            detail_failures += 1
            await asyncio.sleep(1.1)
            continue
        return {
            "status": "passed",
            "list_request_id": _text(getattr(response, "request_id", "")),
            "list_system_order_no": system_order_no,
            "list_platform_order_no": platform_order_no,
            "list_authoritative_field": "customer_shipping_list",
            "list_customer_shipping_service": list_service,
            "detail_system_order_no": system_order_no,
            "detail_request_id": _text(getattr(detail, "request_id", "")),
            "detail_platform_order_no": platform_order_no,
            "detail_authoritative_field": str(field_name or ""),
            "detail_customer_shipping_service": detail_service,
            "detail_failed_attempt_count": detail_failures,
            "external_write_calls": 0,
        }
    raise RuntimeError(
        "Live Amazon details returned no canonical buyer_choose_express "
        f"value after {min(len(candidates), maximum_detail_attempts)} attempts."
    )


def _required_file(environment_name: str) -> Path:
    path = Path(_text(os.environ.get(environment_name)))
    if not path.is_file():
        raise RuntimeError(f"Required preflight file is missing: {environment_name}.")
    return path


def _configuration_store(workspace: Path) -> EncryptedConfigurationStore:
    host_key_file = _required_file("ERP_AUTOMATION_HOST_KEY_FILE")
    try:
        host_key = base64.b64decode(
            host_key_file.read_text(encoding="utf-8").strip(),
            validate=True,
        )
    except ValueError as exc:
        raise RuntimeError("Server host key is invalid.") from exc
    bootstrap_email_file = _required_file(
        "ERP_BOOTSTRAP_OPERATOR_EMAIL_FILE"
    )
    bootstrap_email = bootstrap_email_file.read_text(
        encoding="utf-8"
    ).strip().casefold()
    digest = hashlib.sha256(bootstrap_email.encode("utf-8")).hexdigest()
    operator_path = workspace / "data" / "operator-config" / f"{digest}.enc"
    legacy_path = workspace / "data" / "config.enc"
    config_path = operator_path if operator_path.is_file() else legacy_path
    if not config_path.is_file():
        raise RuntimeError("Lingxing operator configuration is missing.")
    return EncryptedConfigurationStore(
        config_path,
        backend=HostKeyAesGcmBackend(host_key),
    )


async def _run(workspace: Path) -> Mapping[str, Any]:
    store = _configuration_store(workspace)
    runtime_options: dict[str, Any] = {}
    if getattr(store.backend, "name", "") == "host-key-aes-256-gcm":
        local_state = workspace / "data" / "local"
        runtime_options = {
            "token_path": local_state / "lingxing-token.enc",
            "lock_path": local_state / "lingxing-token.lock",
            "token_backend": store.backend,
        }
    client = await create_lingxing_openapi_client(
        store,
        **runtime_options,
    )
    try:
        return await probe_customer_shipping_fields(client)
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("ERP_AUTOMATION_HOME", "/runtime"),
    )
    args = parser.parse_args(argv)
    result = asyncio.run(_run(Path(args.workspace).resolve()))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
