from __future__ import annotations

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
    printed_manual_message = False
    config = login_config or AlibabaLoginConfig()
    should_auto_login = auto_login and config.auto_login

    while time.monotonic() < deadline:
        body_text = await _safe_body_text(page)
        if _is_logistics_detail_ready(page.url, body_text):
            return

        if await is_alibaba_login_page(page, body_text):
            if auto_login_attempted and _has_invalid_login_error(body_text) and not printed_manual_message:
                print("阿里国际站拒绝了 .env 中的账号或密码，请检查 ALIBABA_ACCOUNT/ALIBABA_PASSWORD，或在浏览器里手动登录；脚本会自动继续。")
                printed_manual_message = True
            if should_auto_login and config.has_credentials and not auto_login_attempted:
                print("检测到阿里国际站登录页，正在使用 .env 中的账号密码自动登录。")
                auto_login_attempted = True
                if await try_alibaba_auto_login(page, config):
                    await page.wait_for_timeout(1800)
                    if "detail.htm" not in page.url:
                        await page.goto(detail_url, wait_until="domcontentloaded")
                    continue
                print("阿里自动登录未能完成，请在浏览器里手动登录或处理验证；脚本会自动继续。")
                printed_manual_message = True
            elif not printed_manual_message:
                if should_auto_login and not config.has_credentials:
                    print("没有在 .env 中找到 ALIBABA_ACCOUNT/ALIBABA_PASSWORD，请在浏览器里手动登录阿里国际站；脚本会自动继续。")
                else:
                    print("请在浏览器里完成阿里国际站登录；脚本会自动继续。")
                printed_manual_message = True
        elif _needs_manual_verification(body_text) and not printed_manual_message:
            print("阿里页面需要验证码或安全验证，请在浏览器里手动处理；脚本会自动继续。")
            printed_manual_message = True

        await page.wait_for_timeout(3000)

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
        clicked = await _click_login_submit(scope)
        if not clicked:
            await password_input.press("Enter", timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return True
    return False


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
