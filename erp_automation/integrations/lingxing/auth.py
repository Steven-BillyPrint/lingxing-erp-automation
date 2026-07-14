from __future__ import annotations

import asyncio
import inspect
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol, runtime_checkable

from .errors import (
    LingxingAuthError,
    LingxingConfigurationError,
    LingxingTransportError,
)


@dataclass(frozen=True)
class LingxingCredentials:
    """App credentials supplied by an external secret provider."""

    app_id: str
    app_secret: str = field(repr=False)

    def validate(self) -> None:
        if not self.app_id:
            raise LingxingConfigurationError("Lingxing AppID is empty")
        if not self.app_secret:
            raise LingxingConfigurationError("Lingxing AppSecret is empty")


@runtime_checkable
class CredentialProvider(Protocol):
    def get_credentials(self) -> LingxingCredentials | Awaitable[LingxingCredentials]: ...


class StaticCredentialProvider:
    """Simple injector for credentials already loaded by the application.

    It intentionally does not read environment files. Production callers can
    implement ``CredentialProvider`` with Windows Credential Manager, DPAPI, or
    another secret manager.
    """

    def __init__(self, credentials: LingxingCredentials) -> None:
        credentials.validate()
        self._credentials = credentials

    def get_credentials(self) -> LingxingCredentials:
        return self._credentials


@dataclass(frozen=True)
class IssuedToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class TokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    issued_at: float
    expires_at: float
    refresh_expires_at: float
    generation: int = 1

    def access_is_valid(self, now: float, *, leeway: float = 0) -> bool:
        return bool(self.access_token) and self.expires_at - now > max(0.0, leeway)

    def refresh_is_valid(self, now: float, *, leeway: float = 0) -> bool:
        return bool(self.refresh_token) and self.refresh_expires_at - now > max(0.0, leeway)


@runtime_checkable
class TokenStore(Protocol):
    """Atomic token persistence abstraction.

    Persistent implementations must encrypt access and refresh tokens at rest
    and make ``save`` an atomic replacement because refresh tokens rotate once.
    """

    async def load(self) -> TokenBundle | None: ...

    async def save(self, token: TokenBundle) -> None: ...

    async def clear(self) -> None: ...


class MemoryTokenStore:
    """In-process store suitable for tests and short-lived commands."""

    def __init__(self, initial: TokenBundle | None = None) -> None:
        self._token = initial
        self._lock = asyncio.Lock()

    async def load(self) -> TokenBundle | None:
        async with self._lock:
            return self._token

    async def save(self, token: TokenBundle) -> None:
        async with self._lock:
            self._token = token

    async def clear(self) -> None:
        async with self._lock:
            self._token = None


@runtime_checkable
class InterProcessLock(Protocol):
    async def __aenter__(self) -> "InterProcessLock": ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...


class NullInterProcessLock:
    """No-op implementation for a guaranteed single-process caller."""

    async def __aenter__(self) -> "NullInterProcessLock":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FileInterProcessLock:
    """Advisory cross-process lock implemented with the host OS file lock."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._handle = None

    async def __aenter__(self) -> "FileInterProcessLock":
        await asyncio.to_thread(self._acquire_sync)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await asyncio.to_thread(self._release_sync)

    def _acquire_sync(self) -> None:
        if self._handle is not None:
            raise RuntimeError("FileInterProcessLock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on non-Windows hosts
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"Timed out acquiring token lock: {self.path}")
                time.sleep(self.poll_interval)

    def _release_sync(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows hosts
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@runtime_checkable
class TokenEndpoint(Protocol):
    async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken: ...

    async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken: ...


class TokenManager:
    """Single authority for access-token issuance and one-time refresh rotation."""

    REFRESH_INVALID_CODES = frozenset({"2001008", "2001009"})

    def __init__(
        self,
        credential_provider: CredentialProvider,
        token_store: TokenStore,
        interprocess_lock: InterProcessLock,
        token_endpoint: TokenEndpoint,
        *,
        refresh_skew_seconds: float = 600.0,
        refresh_token_lifetime_seconds: float = 7200.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential_provider = credential_provider
        self._token_store = token_store
        self._interprocess_lock = interprocess_lock
        self._token_endpoint = token_endpoint
        self._refresh_skew = max(0.0, float(refresh_skew_seconds))
        self._refresh_lifetime = max(1.0, float(refresh_token_lifetime_seconds))
        self._clock = clock
        self._local_lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        return (await self.get_token()).access_token

    async def get_token(
        self,
        *,
        force_refresh: bool = False,
        stale_access_token: str | None = None,
    ) -> TokenBundle:
        current = await self._token_store.load()
        if self._can_use(current, force_refresh=force_refresh, stale_access_token=stale_access_token):
            return current  # type: ignore[return-value]

        async with self._local_lock:
            async with self._interprocess_lock:
                current = await self._token_store.load()
                if self._can_use(
                    current,
                    force_refresh=force_refresh,
                    stale_access_token=stale_access_token,
                ):
                    return current  # type: ignore[return-value]
                return await self._rotate(current)

    def _can_use(
        self,
        token: TokenBundle | None,
        *,
        force_refresh: bool,
        stale_access_token: str | None,
    ) -> bool:
        if token is None or not token.access_is_valid(self._clock(), leeway=self._refresh_skew):
            return False
        if not force_refresh:
            return True
        # Another process may already have replaced the rejected token while
        # this caller waited on the cross-process lock.
        return bool(stale_access_token and token.access_token != stale_access_token)

    async def _credentials(self) -> LingxingCredentials:
        value = self._credential_provider.get_credentials()
        credentials = await value if inspect.isawaitable(value) else value
        if not isinstance(credentials, LingxingCredentials):
            raise LingxingConfigurationError("CredentialProvider returned an invalid object")
        credentials.validate()
        return credentials

    async def _rotate(self, previous: TokenBundle | None) -> TokenBundle:
        credentials = await self._credentials()
        now = self._clock()
        issued: IssuedToken | None = None

        if previous is not None and previous.refresh_is_valid(now):
            try:
                issued = await self._token_endpoint.refresh_token(
                    credentials.app_id,
                    previous.refresh_token,
                )
            except LingxingAuthError as exc:
                if exc.code not in self.REFRESH_INVALID_CODES:
                    raise
            except LingxingTransportError:
                # The refresh token is one-use. A lost refresh response makes
                # its state ambiguous, so never submit that token a second time;
                # recover by obtaining a new pair with the AppSecret instead.
                issued = None

        if issued is None:
            issued = await self._token_endpoint.issue_token(credentials)

        if not issued.access_token or not issued.refresh_token or issued.expires_in <= 0:
            raise LingxingConfigurationError("Lingxing token endpoint returned incomplete token data")

        issued_at = self._clock()
        bundle = TokenBundle(
            access_token=issued.access_token,
            refresh_token=issued.refresh_token,
            issued_at=issued_at,
            expires_at=issued_at + int(issued.expires_in),
            refresh_expires_at=issued_at + self._refresh_lifetime,
            generation=(previous.generation + 1) if previous else 1,
        )
        # Persistent stores must atomically replace both rotated tokens.
        await self._token_store.save(bundle)
        return bundle
