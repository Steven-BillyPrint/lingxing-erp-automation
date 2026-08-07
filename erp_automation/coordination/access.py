"""Cloudflare Access JWT validation and trusted desktop operator identities."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


MAX_ACCESS_TOKEN_BYTES = 32 * 1024
DEFAULT_CERTIFICATE_CACHE_SECONDS = 6 * 60 * 60
DEFAULT_STALE_CERTIFICATE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CLOCK_SKEW_SECONDS = 30
MAX_CERTIFICATE_CACHE_BYTES = 1024 * 1024
_EMAIL_LOCAL_PATTERN = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class CloudflareAccessError(ValueError):
    """The supplied Cloudflare Access identity could not be trusted."""


class CloudflareAccessUnavailableError(CloudflareAccessError):
    """Cloudflare signing certificates are temporarily unavailable."""


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
        stale_certificate_seconds: float = DEFAULT_STALE_CERTIFICATE_SECONDS,
        clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
        certificate_cache_path: str | Path | None = None,
        bootstrap_certificates_path: str | Path | None = None,
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
        self._stale_seconds = max(
            self._cache_seconds,
            float(stale_certificate_seconds),
        )
        self._clock_skew = max(0.0, min(300.0, float(clock_skew_seconds)))
        self._certificate_cache_path = (
            Path(certificate_cache_path).resolve()
            if certificate_cache_path is not None
            else None
        )
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._keys_expires_at = 0.0
        self._keys_stale_until = 0.0
        self._refresh_in_progress = False
        self._refresh_thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._load_initial_certificates(bootstrap_certificates_path)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            refresh_thread = self._refresh_thread
        if refresh_thread is not None:
            refresh_thread.join(timeout=1.0)
        if self._owns_client and (
            refresh_thread is None or not refresh_thread.is_alive()
        ):
            self._client.close()

    @property
    def ready(self) -> bool:
        """Return whether a bounded-age signing key set is available."""

        with self._lock:
            return bool(self._keys) and self._clock() < self._keys_stale_until

    def prepare(self) -> bool:
        """Warm an empty/expired cache before deployment health is reported."""

        if self.ready:
            return True
        try:
            self._refresh_keys()
        except CloudflareAccessUnavailableError as exc:
            logging.getLogger(__name__).error(
                "Cloudflare Access certificates are not ready: %s",
                exc,
            )
            return False
        return self.ready

    def _read_certificate_document(
        self,
        path: Path,
    ) -> Mapping[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > MAX_CERTIFICATE_CACHE_BYTES:
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return document if isinstance(document, Mapping) else None

    def _parse_certificate_document(
        self,
        document: Mapping[str, Any],
    ) -> tuple[float, dict[str, rsa.RSAPublicKey]] | None:
        try:
            version = int(document.get("version") or 0)
        except (TypeError, ValueError):
            return None
        if version != 1:
            return None
        if str(document.get("issuer") or "").rstrip("/") != self.issuer:
            return None
        try:
            fetched_at = float(document.get("fetched_at_epoch") or 0)
        except (TypeError, ValueError):
            return None
        now = float(self._clock())
        if (
            not math.isfinite(fetched_at)
            or fetched_at <= 0
            or fetched_at > now + self._clock_skew
            or now >= fetched_at + self._stale_seconds
        ):
            return None
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 16:
            return None
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
            return None
        return fetched_at, keys

    def _load_initial_certificates(
        self,
        bootstrap_certificates_path: str | Path | None,
    ) -> None:
        candidates: list[tuple[float, dict[str, rsa.RSAPublicKey]]] = []
        paths = (
            self._certificate_cache_path,
            (
                Path(bootstrap_certificates_path).resolve()
                if bootstrap_certificates_path is not None
                else None
            ),
        )
        for path in paths:
            if path is None:
                continue
            document = self._read_certificate_document(path)
            parsed = (
                self._parse_certificate_document(document)
                if document is not None
                else None
            )
            if parsed is not None:
                candidates.append(parsed)
        if not candidates:
            return
        fetched_at, keys = max(candidates, key=lambda item: item[0])
        self._keys = keys
        self._keys_expires_at = fetched_at + self._cache_seconds
        self._keys_stale_until = fetched_at + self._stale_seconds

    def _persist_certificates(
        self,
        *,
        fetched_at: float,
        raw_keys: list[Mapping[str, Any]],
    ) -> None:
        path = self._certificate_cache_path
        if path is None:
            return
        document = {
            "version": 1,
            "issuer": self.issuer,
            "fetched_at_epoch": int(fetched_at),
            "keys": [dict(item) for item in raw_keys],
        }
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            logging.getLogger(__name__).warning(
                "Unable to persist the Cloudflare Access certificate cache."
            )
        finally:
            temporary.unlink(missing_ok=True)

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
            raise CloudflareAccessUnavailableError(
                "Unable to retrieve Cloudflare Access signing certificates."
            ) from exc
        raw_keys = document.get("keys") if isinstance(document, Mapping) else None
        if not isinstance(raw_keys, list):
            raise CloudflareAccessUnavailableError(
                "Cloudflare Access signing certificates are malformed."
            )
        keys: dict[str, rsa.RSAPublicKey] = {}
        accepted: list[Mapping[str, Any]] = []
        for item in raw_keys:
            if not isinstance(item, Mapping):
                continue
            try:
                kid, public_key = self._public_key(item)
            except CloudflareAccessError:
                continue
            keys[kid] = public_key
            accepted.append(dict(item))
        if not keys:
            raise CloudflareAccessUnavailableError(
                "Cloudflare Access returned no usable signing certificates."
            )
        fetched_at = float(self._clock())
        with self._lock:
            if self._closed:
                return
            self._keys = keys
            self._keys_expires_at = fetched_at + self._cache_seconds
            self._keys_stale_until = fetched_at + self._stale_seconds
            self._persist_certificates(fetched_at=fetched_at, raw_keys=accepted)

    def _refresh_keys_in_background(self) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
            self._refresh_keys()
        except CloudflareAccessError as exc:
            logging.getLogger(__name__).warning(
                "Cloudflare Access certificate refresh deferred: %s",
                exc,
            )
        finally:
            with self._lock:
                self._refresh_in_progress = False
                self._refresh_thread = None

    def _start_background_refresh(self) -> None:
        if self._refresh_in_progress or self._closed:
            return
        self._refresh_in_progress = True
        self._refresh_thread = threading.Thread(
            target=self._refresh_keys_in_background,
            name="cloudflare-access-certificate-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _key_for(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            now = self._clock()
            key = self._keys.get(kid)
            if key is not None and now < self._keys_expires_at:
                return key
            if key is not None and now < self._keys_stale_until:
                # A bounded-age public key remains safe for already-issued,
                # short-lived JWTs. Refresh asynchronously so an origin-network
                # incident cannot prevent every desktop from opening.
                self._start_background_refresh()
                return key
            if now >= self._keys_expires_at or key is None:
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
    "CloudflareAccessUnavailableError",
    "CloudflareAccessVerifier",
    "OperatorIdentity",
]
