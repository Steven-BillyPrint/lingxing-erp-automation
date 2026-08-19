from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from .config import AlibabaLoginConfig


ALIBABA_LOGIN_URL_MARKERS = (
    "login.alibaba.com",
    "passport.alibaba.com",
    "login.aliexpress.com",
)
DETAIL_READY_MARKERS = ("物流订单详情", "订单状态", "物流订单号")
DETAIL_ERROR_MARKERS = ("无权限", "没有权限", "无数据", "暂无数据", "页面不可访问", "访问受限", "订单不存在")
MANUAL_VERIFY_MARKERS = ("验证码", "滑块", "验证", "二次验证", "安全验证")
INVALID_LOGIN_MARKERS = (
    "账号名或登录密码不正确",
    "账号或登录密码不正确",
    "账号或密码错误",
    "登录密码不正确",
    "密码不正确",
    "incorrect",
)

ALIBABA_ACCOUNT_MISMATCH_MESSAGE = (
    "当前登录的阿里账号与配置的物流查询账号不一致，已停止物流查询。"
)
ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE = (
    "无法确认当前登录的阿里账号与配置的物流查询账号一致，已停止物流查询。"
)
_IDENTITY_ATTESTATION_KEY = "erp_automation.alibaba.logistics_query_identity.v1"
_PRIMARY_IDENTITY_COOKIE_NAMES = frozenset({"havana_lgc2_4", "xman_i"})
_FALLBACK_IDENTITY_COOKIE_NAMES = frozenset({"sgcookie", "t", "xman_status2"})
_ACCOUNT_EMAIL_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)
_ACCOUNT_IDENTITY_SELECTORS = (
    '[data-testid*="account"]',
    '[data-role*="account"]',
    '[data-spm*="account"]',
    '[class*="account-name"]',
    '[class*="accountName"]',
    '[class*="user-name"]',
    '[class*="userName"]',
    '[class*="login-id"]',
    '[class*="loginId"]',
    'header [title*="@"]',
    'nav [title*="@"]',
)


class AlibabaAccountVerificationError(RuntimeError):
    """Raised when the logistics browser cannot prove the configured identity."""


class AlibabaAccountMismatchError(AlibabaAccountVerificationError):
    """Raised when Alibaba explicitly exposes a different signed-in account."""


class AlibabaAccountUnverifiedError(AlibabaAccountVerificationError):
    """Raised when a loaded page cannot yet prove the configured account."""


ACCOUNT_SELECTORS = (
    'input[name="loginId"]',
    'input[name="account"]',
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[type="text"]',
)
PASSWORD_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
)
SUBMIT_SELECTORS = (
    "button.sif_form-submit",
    "#fm-login-submit",
    ".fm-submit",
    ".login-submit",
    ".password-login",
    'button:has-text("登录")',
    'button[type="submit"]',
    'input[type="submit"]',
    ".fm-button",
    ".next-btn-primary",
)
TRANSIENT_VERIFICATION_RETRY_DELAY_MS = 5_000
ALIBABA_DETAIL_POLL_INTERVAL_MS = 3_000
ALIBABA_ACCOUNT_VERIFICATION_RETRY_DELAYS_MS = (600, 1_200)


async def wait_for_alibaba_logistics_detail(
    page,
    detail_url: str,
    *,
    login_config: AlibabaLoginConfig | None,
    auto_login: bool = True,
    timeout_sec: int = 300,
) -> None:
    """Wait until an Alibaba logistics detail page is readable, logging in when possible."""

    deadline = time.monotonic() + max(timeout_sec, 1)
    auto_login_attempted = False
    auto_login_submitted = False
    pre_login_fingerprint = ""
    transient_verification_retried = False
    printed_manual_message = False
    config = login_config or AlibabaLoginConfig()
    should_auto_login = auto_login and config.auto_login

    while time.monotonic() < deadline:
        body_text = await _safe_body_text(page)
        if _is_logistics_detail_ready(page.url, body_text):
            if config.account:
                await verify_alibaba_logistics_account(
                    page,
                    config.account,
                    fresh_configured_login=auto_login_submitted,
                    previous_fingerprint=pre_login_fingerprint,
                )
            return

        login_page = await is_alibaba_login_page(page, body_text)
        needs_manual_verification = _needs_manual_verification(body_text)
        if (
            needs_manual_verification
            and not transient_verification_retried
            and (auto_login_submitted or not login_page)
        ):
            # Alibaba can briefly show a verification interstitial on the
            # first deep-link request while still establishing a valid device
            # session.  A second request in the same browser session is often
            # accepted without operator input.  Reproduce that safe retry once
            # before interrupting the operator; never loop around a real
            # persistent challenge.
            transient_verification_retried = True
            print(
                "首次检测到阿里验证码或安全验证，正在等待会话稳定后自动重试一次。"
            )
            await page.wait_for_timeout(TRANSIENT_VERIFICATION_RETRY_DELAY_MS)
            await page.goto(detail_url, wait_until="domcontentloaded")
            continue

        if login_page:
            if auto_login_attempted and _has_invalid_login_error(body_text) and not printed_manual_message:
                print("阿里国际站拒绝了专用物流查询账号或密码，请检查设置中的阿里物流查询配置，或在浏览器里手动登录；脚本会自动继续。")
                printed_manual_message = True
            if should_auto_login and config.has_credentials and not auto_login_attempted:
                print("检测到阿里国际站登录页，正在使用专用物流查询账号自动登录。")
                auto_login_attempted = True
                pre_login_fingerprint = (
                    await _alibaba_identity_cookie_fingerprint(page)
                )
                if await try_alibaba_auto_login(page, config):
                    auto_login_submitted = True
                    await page.wait_for_timeout(1800)
                    if "detail.htm" not in page.url:
                        await page.goto(detail_url, wait_until="domcontentloaded")
                    continue
                print("阿里自动登录未能完成，请在浏览器里手动登录或处理验证；脚本会自动继续。")
                printed_manual_message = True
            elif not printed_manual_message:
                if needs_manual_verification:
                    print("阿里页面重试后仍需要验证码或安全验证，请在浏览器里手动处理；脚本会自动继续。")
                elif should_auto_login and not config.has_credentials:
                    print("阿里物流查询账号未配置。")
                else:
                    print("请在浏览器里完成阿里国际站登录；脚本会自动继续。")
                printed_manual_message = True
        elif needs_manual_verification and not printed_manual_message:
            print("阿里页面重试后仍需要验证码或安全验证，请在浏览器里手动处理；脚本会自动继续。")
            printed_manual_message = True

        await page.wait_for_timeout(ALIBABA_DETAIL_POLL_INTERVAL_MS)

    raise RuntimeError("等待阿里国际站物流详情页加载或登录完成超时。")


async def is_alibaba_login_page(page, body_text: str | None = None) -> bool:
    url = str(page.url or "").lower()
    if any(marker in url for marker in ALIBABA_LOGIN_URL_MARKERS):
        return True
    if "/login" in url and "alibaba" in url:
        return True
    if await _has_visible_password_input(page):
        text = body_text if body_text is not None else await _safe_body_text(page)
        return "登录" in text or "Sign in" in text or "Password" in text
    return False


async def try_alibaba_auto_login(page, login_config: AlibabaLoginConfig) -> bool:
    if not login_config.has_credentials:
        return False

    scopes: list[Any] = [page, *page.frames]
    for scope in scopes:
        account_input = await _first_visible_locator(scope, ACCOUNT_SELECTORS)
        password_input = await _first_visible_locator(scope, PASSWORD_SELECTORS)
        if not account_input or not password_input:
            continue
        await account_input.fill(login_config.account or "")
        await password_input.fill(login_config.password or "")
        try:
            filled_account = await account_input.input_value(timeout=2000)
        except Exception:
            filled_account = login_config.account or ""
        if _normalize_account(filled_account) != _normalize_account(
            login_config.account
        ):
            raise AlibabaAccountVerificationError(
                "阿里登录页填写的账号与配置的物流查询账号不一致，已停止物流查询。"
            )
        clicked = await _click_login_submit(scope)
        if not clicked:
            await password_input.press("Enter", timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return True
    return False


async def verify_alibaba_logistics_account(
    page,
    expected_account: str,
    *,
    fresh_configured_login: bool = False,
    previous_fingerprint: str = "",
) -> None:
    """Fail closed unless the current Alibaba session is tied to the query account.

    A fresh login submitted from the configured account establishes a new
    attestation bound to Alibaba's identity/session cookies.  Reused sessions
    must either expose the same account in Alibaba's account UI or match that
    attestation.  A different or unverifiable session never reaches parsing.
    """

    expected = _normalize_account(expected_account)
    if not expected:
        raise AlibabaAccountUnverifiedError(ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE)

    retry_delays = tuple(ALIBABA_ACCOUNT_VERIFICATION_RETRY_DELAYS_MS)
    for attempt in range(len(retry_delays) + 1):
        try:
            await _verify_alibaba_logistics_account_once(
                page,
                expected,
                fresh_configured_login=fresh_configured_login,
                previous_fingerprint=previous_fingerprint,
            )
            return
        except AlibabaAccountUnverifiedError:
            if attempt >= len(retry_delays):
                raise
            await _wait_for_account_verification_retry(
                page,
                retry_delays[attempt],
            )


async def _verify_alibaba_logistics_account_once(
    page,
    expected: str,
    *,
    fresh_configured_login: bool,
    previous_fingerprint: str,
) -> None:
    observed_accounts = await _observed_alibaba_accounts(page)
    if observed_accounts == {expected}:
        await _write_identity_attestation(page, expected)
        return
    if observed_accounts:
        raise AlibabaAccountMismatchError(ALIBABA_ACCOUNT_MISMATCH_MESSAGE)

    fingerprint = await _alibaba_identity_cookie_fingerprint(page)
    if fresh_configured_login:
        if not fingerprint or (
            previous_fingerprint and previous_fingerprint == fingerprint
        ):
            raise AlibabaAccountUnverifiedError(
                ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE
            )
        await _write_identity_attestation(
            page,
            expected,
            fingerprint=fingerprint,
        )
        return

    attestation = await _read_identity_attestation(page)
    attested_account = _normalize_account(attestation.get("account"))
    if attested_account and attested_account != expected:
        raise AlibabaAccountMismatchError(ALIBABA_ACCOUNT_MISMATCH_MESSAGE)
    if (
        attested_account == expected
        and fingerprint
        and str(attestation.get("fingerprint") or "") == fingerprint
    ):
        return
    raise AlibabaAccountUnverifiedError(ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE)


async def _wait_for_account_verification_retry(page, delay_ms: int) -> None:
    """Pause on the current detail page so its account UI/cookies can settle."""

    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        await waiter(max(0, int(delay_ms)))
        return
    # Test doubles and defensive non-Playwright callers should still yield to
    # the event loop without turning a unit test into a real-time wait.
    await asyncio.sleep(0)


def _normalize_account(value: object) -> str:
    return str(value or "").strip().casefold()


async def _observed_alibaba_accounts(page) -> set[str]:
    candidates: set[str] = set()
    for selector in _ACCOUNT_IDENTITY_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 12)):
            item = locator.nth(index)
            values: list[str] = []
            try:
                if not await item.is_visible(timeout=300):
                    continue
            except Exception:
                continue
            for getter in (
                lambda: item.inner_text(timeout=500),
                lambda: item.get_attribute("title", timeout=500),
                lambda: item.get_attribute("data-account", timeout=500),
                lambda: item.get_attribute("aria-label", timeout=500),
            ):
                try:
                    value = await getter()
                except Exception:
                    continue
                if value:
                    values.append(str(value))
            for value in values:
                candidates.update(
                    _normalize_account(match.group(0))
                    for match in _ACCOUNT_EMAIL_PATTERN.finditer(value)
                )
    return candidates


async def _alibaba_identity_cookie_fingerprint(page) -> str:
    try:
        cookies = await page.context.cookies()
    except Exception:
        return ""
    relevant = _identity_cookie_values(cookies, _PRIMARY_IDENTITY_COOKIE_NAMES)
    if not relevant:
        relevant = _identity_cookie_values(cookies, _FALLBACK_IDENTITY_COOKIE_NAMES)
    if not relevant:
        return ""
    payload = json.dumps(relevant, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_cookie_values(
    cookies: object,
    allowed_names: frozenset[str],
) -> list[tuple[str, str, str]]:
    if not isinstance(cookies, list):
        return []
    values: list[tuple[str, str, str]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "").casefold()
        value = str(cookie.get("value") or "")
        if name in allowed_names and domain.endswith("alibaba.com") and value:
            values.append((domain, name, value))
    return sorted(values)


async def _read_identity_attestation(page) -> dict[str, str]:
    try:
        raw = await page.evaluate(
            "key => window.localStorage.getItem(key)",
            _IDENTITY_ATTESTATION_KEY,
        )
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "account": str(parsed.get("account") or ""),
        "fingerprint": str(parsed.get("fingerprint") or ""),
    }


async def _write_identity_attestation(
    page,
    account: str,
    *,
    fingerprint: str | None = None,
) -> None:
    identity_fingerprint = fingerprint or await _alibaba_identity_cookie_fingerprint(page)
    if not identity_fingerprint:
        return
    payload = json.dumps(
        {"account": _normalize_account(account), "fingerprint": identity_fingerprint},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        await page.evaluate(
            "([key, value]) => window.localStorage.setItem(key, value)",
            [_IDENTITY_ATTESTATION_KEY, payload],
        )
    except Exception:
        return


async def _safe_body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=1500)
    except Exception:
        return ""


def _is_logistics_detail_ready(url: str, body_text: str) -> bool:
    if "scm.alibaba.com/luyou/express/detail.htm" not in str(url):
        return False
    return any(marker in body_text for marker in DETAIL_READY_MARKERS + DETAIL_ERROR_MARKERS)


def _needs_manual_verification(body_text: str) -> bool:
    return any(marker in body_text for marker in MANUAL_VERIFY_MARKERS)


def _has_invalid_login_error(body_text: str) -> bool:
    normalized = str(body_text or "").lower()
    return any(marker.lower() in normalized for marker in INVALID_LOGIN_MARKERS)


async def _has_visible_password_input(page) -> bool:
    for scope in [page, *page.frames]:
        locator = await _first_visible_locator(scope, PASSWORD_SELECTORS)
        if locator:
            return True
    return False


async def _first_visible_locator(scope, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            count = await locator.count()
            for index in range(min(count, 5)):
                item = locator.nth(index)
                try:
                    if await item.is_visible(timeout=500):
                        return item
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _click_login_submit(scope) -> bool:
    for selector in SUBMIT_SELECTORS:
        try:
            locator = scope.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                item = locator.nth(index)
                try:
                    if not await item.is_visible(timeout=500):
                        continue
                    text = (await item.inner_text(timeout=500)).strip()
                    if selector == 'button:has-text("登录")' and text != "登录":
                        continue
                    await item.click(timeout=5000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False
