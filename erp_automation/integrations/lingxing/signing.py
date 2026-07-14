from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

from .errors import LingxingConfigurationError


def canonical_json_bytes(value: object) -> bytes:
    """Serialize request JSON exactly as it participates in the signature.

    Lingxing's official Python SDK uses compact JSON and recursively sorted
    object keys for collection values.  List order is preserved.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _format_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        serializable: object = list(value) if isinstance(value, tuple) else value
        return canonical_json_bytes(serializable).decode("utf-8")
    if isinstance(value, bytes):
        raise TypeError("bytes cannot participate in a Lingxing signature directly")
    return str(value)


def canonicalize_params(params: Mapping[str, Any] | None) -> str:
    """Build Lingxing's ASCII-key-sorted canonical parameter string.

    Empty strings are excluded.  ``None`` is deliberately included as the
    literal ``null`` because the public documentation distinguishes null from
    an empty value.
    """

    if not params:
        return ""
    if any(not isinstance(key, str) for key in params):
        raise TypeError("Lingxing parameter names must be strings")

    parts: list[str] = []
    # API parameter names are ASCII. UTF-8 byte ordering keeps that exact
    # ordering while remaining deterministic if a future parameter is not.
    for key in sorted(params, key=lambda item: item.encode("utf-8")):
        value = params[key]
        if isinstance(value, str) and value == "":
            continue
        parts.append(f"{key}={_format_scalar(value)}")
    return "&".join(parts)


@dataclass(frozen=True)
class SignatureDetails:
    canonical_query: str
    md5_upper: str
    raw: str
    url_encoded: str


class LingxingSigner:
    """Generate Lingxing MD5 + AES/ECB/PKCS7 signatures."""

    def __init__(self, app_id: str) -> None:
        if not app_id:
            raise LingxingConfigurationError("Lingxing AppID is required for signing")
        self._app_id = app_id

    def sign(self, params: Mapping[str, Any]) -> SignatureDetails:
        canonical_query = canonicalize_params(params)
        md5_upper = hashlib.md5(canonical_query.encode("utf-8")).hexdigest().upper()
        raw = self._encrypt_md5(md5_upper)
        return SignatureDetails(
            canonical_query=canonical_query,
            md5_upper=md5_upper,
            raw=raw,
            url_encoded=quote(raw, safe=""),
        )

    def _encrypt_md5(self, md5_upper: str) -> str:
        key = self._app_id.encode("utf-8")
        if len(key) not in {16, 24, 32}:
            raise LingxingConfigurationError(
                "Lingxing AppID must encode to a valid AES key length (16, 24, or 32 bytes)"
            )
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:  # pragma: no cover - exercised only in a misconfigured runtime
            raise LingxingConfigurationError(
                "The cryptography package is required for Lingxing AES signatures"
            ) from exc

        padder = padding.PKCS7(128).padder()
        padded = padder.update(md5_upper.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")
