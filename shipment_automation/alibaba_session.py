from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Awaitable, Callable

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

ALIBABA_ACCOUNT_CHANGED_MESSAGE = (
    "配置的阿里物流查询账号已发生变化。请在物流查询专用 Chrome 中退出当前账号，"
    "使用新配置账号重新登录后，再重新查询物流。"
)
# Backward-compatible public name used by worker/reporting callers.  The
# logistics browser no longer compares Alibaba's page UI with the configured
# account; only a changed configured account can trigger this error.
ALIBABA_ACCOUNT_MISMATCH_MESSAGE = ALIBABA_ACCOUNT_CHANGED_MESSAGE
ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE = (
    "无法确认当前登录的阿里账号与配置的物流查询账号一致，已停止物流查询。"
)
_IDENTITY_ATTESTATION_KEY = "erp_automation.alibaba.logistics_query_identity.v1"
_PRIMARY_IDENTITY_COOKIE_NAMES = frozenset({"havana_lgc2_4", "xman_i"})
_FALLBACK_IDENTITY_COOKIE_NAMES = frozenset({"sgcookie", "t", "xman_status2"})


class AlibabaAccountVerificationError(RuntimeError):
    """Raised when the dedicated logistics browser must be re-authenticated."""


class AlibabaAccountMismatchError(AlibabaAccountVerificationError):
    """Raised when the configured query account changed without a fresh login."""


class AlibabaAccountUnverifiedError(AlibabaAccountVerificationError):
    """Raised when a loaded page cannot yet prove the configured account."""


class AlibabaPasswordVerificationError(AlibabaAccountVerificationError):
    """Raised before submit when the page changed the configured password."""


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
VERIFICATION_CLOSE_SELECTORS = (
    '[role="dialog"] button[aria-label="Close"]',
    '[role="dialog"] button[aria-label="close"]',
    '[role="dialog"] button[aria-label="关闭"]',
    '[role="dialog"] [class*="close"]',
    '.next-dialog-close',
    '.next-dialog-close-icon',
    '.ant-modal-close',
    '[class*="modal"] [class*="close"]',
    '[class*="dialog"] [class*="close"]',
    'button:has-text("×")',
    '[role="button"]:has-text("×")',
    'text="×"',
)
VERIFICATION_DIALOG_CLOSE_DELAY_MS = 300
POST_LOGIN_SUBMIT_DELAY_MS = 1_800
ALIBABA_DETAIL_POLL_INTERVAL_MS = 3_000
ManualLoginCallback = Callable[[str], Awaitable[bool]]


async def wait_for_alibaba_logistics_detail(
    page,
    detail_url: str,
    *,
    login_config: AlibabaLoginConfig | None,
    auto_login: bool = True,
    timeout_sec: int = 300,
    manual_login_callback: ManualLoginCallback | None = None,
) -> None:
    """Wait until an Alibaba logistics detail page is readable, logging in when possible."""

    deadline = time.monotonic() + max(timeout_sec, 1)
    auto_login_attempted = False
    auto_login_submitted = False
    login_page_observed = False
    verification_login_retried = False
    manual_login_prompted = False
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
                    fresh_configured_login=login_page_observed,
                )
            return

        login_page = await is_alibaba_login_page(page, body_text)
        needs_manual_verification = _needs_manual_verification(body_text)
        if (
            needs_manual_verification
            and not verification_login_retried
            and (auto_login_submitted or not login_page)
        ):
            # Never interact with the verification fields.  Close the first
            # challenge and submit the already-filled login form exactly once.
            # If Alibaba presents the challenge again, the operator must take
            # over in the visible browser.
            verification_login_retried = True
            print(
                "首次检测到阿里验证码或安全验证，正在关闭验证弹窗并重新点击登录。"
            )
            if await close_verification_dialog_and_retry_login(page):
                auto_login_submitted = True
                continue
            print(
                "未能安全关闭阿里验证弹窗并重新点击登录，改由人工完成登录。"
            )

        if (
            needs_manual_verification
            and verification_login_retried
            and not manual_login_prompted
        ):
            manual_login_prompted = True
            manual_message = (
                "阿里验证码或安全验证再次出现。请在已打开的 Chrome 中人工完成验证并登录"
                "阿里物流站；登录成功后回到程序确认，程序会继续读取物流订单信息。"
            )
            print(manual_message)
            if manual_login_callback is not None:
                if not await manual_login_callback(manual_message):
                    raise AlibabaAccountVerificationError(
                        "用户取消了阿里物流站人工登录，本批物流查询已停止并保留待重试。"
                    )
                print("已收到人工登录完成确认，正在继续读取阿里物流订单信息。")
                if not _is_logistics_detail_url(page.url, detail_url):
                    await page.goto(detail_url, wait_until="domcontentloaded")
                continue
            printed_manual_message = True

        if login_page:
            login_page_observed = True
            if auto_login_attempted and _has_invalid_login_error(body_text) and not printed_manual_message:
                print("阿里国际站拒绝了专用物流查询账号或密码，请检查设置中的阿里物流查询配置，或在浏览器里手动登录；脚本会自动继续。")
                printed_manual_message = True
            if should_auto_login and config.has_credentials and not auto_login_attempted:
                print("检测到阿里国际站登录页，正在使用专用物流查询账号自动登录。")
                auto_login_attempted = True
                if await try_alibaba_auto_login(page, config):
                    auto_login_submitted = True
                    await page.wait_for_timeout(POST_LOGIN_SUBMIT_DELAY_MS)
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
        expected_password = login_config.password or ""
        for attempt in range(2):
            try:
                filled_password = await password_input.input_value(timeout=2000)
            except Exception as exc:
                raise AlibabaPasswordVerificationError(
                    "无法确认阿里登录页密码框与已保存配置一致，已在点击登录前停止。"
                ) from exc
            if not hmac.compare_digest(filled_password, expected_password):
                raise AlibabaPasswordVerificationError(
                    "阿里登录页在自动填写后改写了密码框，已在点击登录前停止。"
                )
            if attempt == 0:
                await page.wait_for_timeout(150)
        clicked = await _click_login_submit(scope)
        if not clicked:
            await password_input.press("Enter", timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return True
    return False


async def close_verification_dialog_and_retry_login(page) -> bool:
    """Close one verification dialog and retry the normal login submit.

    This helper deliberately never locates or clicks verification inputs.  A
    second challenge is left untouched for the operator.
    """

    scopes: list[Any] = [page, *page.frames]
    dialog_closed = False
    for scope in scopes:
        if await _click_first_visible(scope, VERIFICATION_CLOSE_SELECTORS):
            dialog_closed = True
            break
    if not dialog_closed:
        return False

    await page.wait_for_timeout(VERIFICATION_DIALOG_CLOSE_DELAY_MS)
    for scope in scopes:
        if await _click_login_submit(scope):
            await page.wait_for_timeout(POST_LOGIN_SUBMIT_DELAY_MS)
            return True
    return False


async def verify_alibaba_logistics_account(
    page,
    expected_account: str,
    *,
    fresh_configured_login: bool = False,
) -> None:
    """Require a fresh dedicated-browser login only after config account changes.

    Normal cookie rotation, missing account UI, and an unverifiable reused
    session no longer block this read-only logistics lookup.  The profile keeps
    only its last configured query account.  When that configured value changes,
    the first attempt records the current session as the pre-login baseline and
    stops.  A login observed by the current task, or a later identity-cookie
    change, completes the rebind to the newly configured account.
    """

    expected = _normalize_account(expected_account)
    if not expected:
        raise AlibabaAccountUnverifiedError(ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE)

    attestation = await _read_identity_attestation(page)
    bound_account = _normalize_account(attestation.get("account"))
    pending_account = _normalize_account(attestation.get("pending_account"))

    if not bound_account:
        # First run after this change (or a cleared browser profile): adopt the
        # existing configured account without inspecting Alibaba's page UI.
        await _write_identity_attestation(page, expected)
        return

    if bound_account == expected:
        # A reverted configuration does not require another login.  Clear a
        # stale pending change while preserving the last bound fingerprint.
        if pending_account:
            await _write_identity_attestation(
                page,
                expected,
                fingerprint=str(attestation.get("fingerprint") or ""),
            )
        return

    if fresh_configured_login:
        await _write_identity_attestation(page, expected)
        return

    current_fingerprint = await _alibaba_identity_cookie_fingerprint(page)
    if pending_account == expected:
        pre_login_fingerprint = str(
            attestation.get("relogin_fingerprint") or ""
        )
        if (
            pre_login_fingerprint
            and current_fingerprint
            and current_fingerprint != pre_login_fingerprint
        ):
            await _write_identity_attestation(
                page,
                expected,
                fingerprint=current_fingerprint,
            )
            return
    else:
        # Record the session that must be replaced.  Do not accept a cookie
        # value already present when the configuration change is first seen.
        await _write_identity_attestation(
            page,
            bound_account,
            fingerprint=str(attestation.get("fingerprint") or ""),
            pending_account=expected,
            relogin_fingerprint=current_fingerprint,
        )

    raise AlibabaAccountMismatchError(ALIBABA_ACCOUNT_CHANGED_MESSAGE)


def _normalize_account(value: object) -> str:
    return str(value or "").strip().casefold()


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
        "pending_account": str(parsed.get("pending_account") or ""),
        "relogin_fingerprint": str(parsed.get("relogin_fingerprint") or ""),
    }


async def _write_identity_attestation(
    page,
    account: str,
    *,
    fingerprint: str | None = None,
    pending_account: str = "",
    relogin_fingerprint: str = "",
) -> None:
    identity_fingerprint = (
        await _alibaba_identity_cookie_fingerprint(page)
        if fingerprint is None
        else str(fingerprint or "")
    )
    payload = json.dumps(
        {
            "account": _normalize_account(account),
            "fingerprint": identity_fingerprint,
            "pending_account": _normalize_account(pending_account),
            "relogin_fingerprint": str(relogin_fingerprint or ""),
        },
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


def _is_logistics_detail_url(current_url: object, expected_url: str) -> bool:
    current = str(current_url or "").split("#", 1)[0]
    expected = str(expected_url or "").split("#", 1)[0]
    return bool(expected) and current == expected


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


async def _click_first_visible(scope, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                item = locator.nth(index)
                try:
                    if not await item.is_visible(timeout=500):
                        continue
                    await item.click(timeout=5000)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False
