"""Cloudflare Access JWT validation and trusted desktop operator identities."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


MAX_ACCESS_TOKEN_BYTES = 32 * 1024
DEFAULT_CERTIFICATE_CACHE_SECONDS = 6 * 60 * 60
DEFAULT_CLOCK_SKEW_SECONDS = 30
_EMAIL_LOCAL_PATTERN = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class CloudflareAccessError(ValueError):
    """The supplied Cloudflare Access identity could not be trusted."""


@dataclass(frozen=True)
class OperatorIdentity:
    """Identity extracted exclusively from a verified Cloudflare Access JWT."""

    email: str
    name: str
    subject: str

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.email}）" if self.name != self.email else self.email


def _base64url_decode(value: str, *, label: str) -> bytes:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > MAX_ACCESS_TOKEN_BYTES:
        raise CloudflareAccessError(f"Cloudflare Access {label} is invalid.")
    if re.fullmatch(r"[A-Za-z0-9_-]+", normalized) is None:
        raise CloudflareAccessError(
            f"Cloudflare Access {label} is not valid base64url."
        )
    try:
        encoded = (normalized + "=" * (-len(normalized) % 4)).encode("ascii")
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise CloudflareAccessError(
            f"Cloudflare Access {label} is not valid base64url."
        ) from exc


def _json_segment(value: str, *, label: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(_base64url_decode(value, label=label).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CloudflareAccessError(
            f"Cloudflare Access {label} is not valid JSON."
        ) from exc
    if not isinstance(decoded, Mapping):
        raise CloudflareAccessError(
            f"Cloudflare Access {label} must be a JSON object."
        )
    return decoded


def _bounded_text(
    value: Any,
    *,
    label: str,
    maximum: int,
    required: bool = True,
) -> str:
    normalized = str(value or "").strip()
    if (required and not normalized) or len(normalized) > maximum:
        raise CloudflareAccessError(f"Cloudflare Access {label} is invalid.")
    return normalized


def _safe_operator_name(value: Any, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    visible: list[str] = []
    for character in normalized:
        if character.isspace():
            visible.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            visible.append(character)
    collapsed = " ".join("".join(visible).split())
    if not collapsed or len(collapsed) > 200:
        return fallback
    return collapsed


class CloudflareAccessVerifier:
    """Validate Cloudflare Access application tokens at the ERP origin."""

    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        allowed_email_domain: str,
        client: httpx.Client | None = None,
        clock=time.time,
        certificate_cache_seconds: float = DEFAULT_CERTIFICATE_CACHE_SECONDS,
        clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
    ) -> None:
        normalized_team = str(team_domain or "").strip().rstrip("/")
        if "://" not in normalized_team:
            normalized_team = f"https://{normalized_team}"
        parsed = urlparse(normalized_team)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Cloudflare Access team domain is invalid.")
        normalized_audience = str(audience or "").strip()
        if not normalized_audience or len(normalized_audience) > 512:
            raise ValueError("Cloudflare Access audience is invalid.")
        normalized_domain = str(allowed_email_domain or "").strip().casefold()
        if normalized_domain.startswith("@"):
            normalized_domain = normalized_domain[1:]
        if not _EMAIL_DOMAIN_PATTERN.fullmatch(normalized_domain):
            raise ValueError("Cloudflare Access allowed email domain is invalid.")

        self.issuer = f"https://{parsed.hostname}"
        self.audience = normalized_audience
        self.allowed_email_domain = normalized_domain
        self.certificates_url = f"{self.issuer}/cdn-cgi/access/certs"
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._clock = clock
        self._cache_seconds = max(60.0, float(certificate_cache_seconds))
        self._clock_skew = max(0.0, min(300.0, float(clock_skew_seconds)))
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._keys_expires_at = 0.0
        self._lock = threading.RLock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _public_key(jwk: Mapping[str, Any]) -> tuple[str, rsa.RSAPublicKey]:
        if str(jwk.get("kty") or "") != "RSA":
            raise CloudflareAccessError("Cloudflare Access certificate is not RSA.")
        kid = _bounded_text(jwk.get("kid"), label="certificate key ID", maximum=512)
        exponent_bytes = _base64url_decode(str(jwk.get("e") or ""), label="RSA exponent")
        modulus_bytes = _base64url_decode(str(jwk.get("n") or ""), label="RSA modulus")
        exponent = int.from_bytes(exponent_bytes, "big")
        modulus = int.from_bytes(modulus_bytes, "big")
        if exponent < 3 or modulus.bit_length() < 2048:
            raise CloudflareAccessError("Cloudflare Access RSA certificate is invalid.")
        try:
            return kid, rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except ValueError as exc:
            raise CloudflareAccessError(
                "Cloudflare Access RSA certificate is invalid."
            ) from exc

    def _refresh_keys(self) -> None:
        try:
            response = self._client.get(self.certificates_url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudflareAccessError(
                "Unable to retrieve Cloudflare Access signing certificates."
            ) from exc
        raw_keys = document.get("keys") if isinstance(document, Mapping) else None
        if not isinstance(raw_keys, list):
            raise CloudflareAccessError(
                "Cloudflare Access signing certificates are malformed."
            )
        keys: dict[str, rsa.RSAPublicKey] = {}
        for item in raw_keys:
            if not isinstance(item, Mapping):
                continue
            try:
                kid, public_key = self._public_key(item)
            except CloudflareAccessError:
                continue
            keys[kid] = public_key
        if not keys:
            raise CloudflareAccessError(
                "Cloudflare Access returned no usable signing certificates."
            )
        self._keys = keys
        self._keys_expires_at = self._clock() + self._cache_seconds

    def _key_for(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            now = self._clock()
            if now >= self._keys_expires_at or kid not in self._keys:
                self._refresh_keys()
            key = self._keys.get(kid)
            if key is None:
                raise CloudflareAccessError(
                    "Cloudflare Access token uses an unknown signing certificate."
                )
            return key

    def verify(self, token: str) -> OperatorIdentity:
        normalized = str(token or "").strip()
        if not normalized or len(normalized.encode("utf-8")) > MAX_ACCESS_TOKEN_BYTES:
            raise CloudflareAccessError("Cloudflare Access login token is missing.")
        segments = normalized.split(".")
        if len(segments) != 3:
            raise CloudflareAccessError("Cloudflare Access login token is malformed.")
        header = _json_segment(segments[0], label="JWT header")
        claims = _json_segment(segments[1], label="JWT claims")
        if str(header.get("alg") or "") != "RS256":
            raise CloudflareAccessError(
                "Cloudflare Access token must use the RS256 algorithm."
            )
        kid = _bounded_text(
            header.get("kid"),
            label="JWT signing key ID",
            maximum=512,
        )
        signed = f"{segments[0]}.{segments[1]}".encode("ascii")
        signature = _base64url_decode(segments[2], label="JWT signature")
        try:
            self._key_for(kid).verify(
                signature,
                signed,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise CloudflareAccessError(
                "Cloudflare Access token signature is invalid."
            ) from exc

        issuer = _bounded_text(claims.get("iss"), label="issuer", maximum=512)
        if issuer.rstrip("/") != self.issuer:
            raise CloudflareAccessError("Cloudflare Access token issuer is invalid.")
        raw_audience = claims.get("aud")
        audiences = (
            {raw_audience}
            if isinstance(raw_audience, str)
            else {
                str(item)
                for item in raw_audience
                if isinstance(item, str)
            }
            if isinstance(raw_audience, list)
            else set()
        )
        if self.audience not in audiences:
            raise CloudflareAccessError("Cloudflare Access token audience is invalid.")

        now = float(self._clock())
        try:
            expires_at = float(claims["exp"])
            not_before = float(claims.get("nbf", 0))
            issued_at = float(claims.get("iat", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudflareAccessError(
                "Cloudflare Access token timestamps are invalid."
            ) from exc
        if not all(
            math.isfinite(value)
            for value in (expires_at, not_before, issued_at)
        ):
            raise CloudflareAccessError(
                "Cloudflare Access token timestamps are invalid."
            )
        if expires_at <= now - self._clock_skew:
            raise CloudflareAccessError("Cloudflare Access login has expired.")
        if not_before > now + self._clock_skew:
            raise CloudflareAccessError("Cloudflare Access login is not active yet.")
        if issued_at > now + self._clock_skew:
            raise CloudflareAccessError(
                "Cloudflare Access token issue time is invalid."
            )

        email = _bounded_text(
            claims.get("email"),
            label="email",
            maximum=320,
        ).casefold()
        local, separator, domain = email.rpartition("@")
        if (
            not separator
            or not _EMAIL_LOCAL_PATTERN.fullmatch(local)
            or domain.casefold() != self.allowed_email_domain
        ):
            raise CloudflareAccessError(
                f"Only @{self.allowed_email_domain} email accounts are allowed."
            )
        subject = _bounded_text(
            claims.get("sub") or email,
            label="subject",
            maximum=512,
        )
        name = _safe_operator_name(claims.get("name"), fallback=local)
        return OperatorIdentity(email=email, name=name, subject=subject)


__all__ = [
    "CloudflareAccessError",
    "CloudflareAccessVerifier",
    "OperatorIdentity",
]
