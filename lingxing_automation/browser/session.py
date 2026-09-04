from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..constants import ORDER_MANAGEMENT_URL
from ..models import LoginConfig
from ..pages.diagnostics import save_page_diagnostics


class OrderPageAuthenticationRequired(RuntimeError):
    """The ERP browser session needs an interactive device verification."""


class OrderPageLoadFailed(RuntimeError):
    """The shared ERP browser session could not load the order page."""


_MODULE_PRELOAD_LINK_PATTERN = re.compile(
    r"""<link\b(?=[^>]*\brel\s*=\s*["']?modulepreload(?:["'\s/>]))[^>]*>""",
    re.IGNORECASE,
)


def _strip_modulepreload_links(html: str) -> tuple[str, int]:
    """Remove module preloads that exhaust Chromium resources on small servers."""
    return _MODULE_PRELOAD_LINK_PATTERN.subn("", html)


async def _install_headless_order_page_resource_guard(context, args: argparse.Namespace) -> None:
    """Keep Lingxing's large module-preload list from exhausting headless Chromium."""
    if not bool(getattr(args, "headless", False)):
        return

    async def filter_order_document(route) -> None:
        if route.request.resource_type != "document":
            await route.continue_()
            return
        try:
            response = await route.fetch()
            html = await response.text()
            filtered_html, removed_count = _strip_modulepreload_links(html)
            if removed_count:
                await route.fulfill(response=response, body=filtered_html)
            else:
                await route.fulfill(response=response)
        except Exception:
            # Preserve normal navigation if the optimization itself cannot run.
            await route.continue_()

    await context.route(f"{ORDER_MANAGEMENT_URL}*", filter_order_document)


def _requires_mobile_binding(url: str) -> bool:
    return "/bindMobile" in str(url or "")


def build_launch_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """构建launch kwargs。"""
    launch_kwargs: dict[str, Any] = {
        "headless": args.headless,
        "locale": "zh-CN",
        "args": ["--disable-blink-features=AutomationControlled", "--start-maximized"],
    }
    if args.headless:
        launch_kwargs["viewport"] = {"width": args.width, "height": args.height}
    else:
        launch_kwargs["no_viewport"] = True
        launch_kwargs["chromium_sandbox"] = True
    if args.browser_channel and args.browser_channel != "bundled":
        launch_kwargs["channel"] = args.browser_channel
    return launch_kwargs


def _without_chromium_sandbox(launch_kwargs: dict[str, Any]) -> dict[str, Any]:
    """从浏览器启动参数中移除 Chromium sandbox 相关参数。"""
    fallback_kwargs = dict(launch_kwargs)
    fallback_kwargs.pop("chromium_sandbox", None)
    return fallback_kwargs


async def _launch_with_sandbox_fallback(playwright, profile_dir: Path, launch_kwargs: dict[str, Any]):
    """启动浏览器上下文，并在 sandbox 不兼容时自动降级重试。"""
    try:
        return await playwright.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs), None
    except Exception as exc:
        if launch_kwargs.get("chromium_sandbox") is not True:
            return None, exc

        fallback_kwargs = _without_chromium_sandbox(launch_kwargs)
        try:
            context = await playwright.chromium.launch_persistent_context(str(profile_dir), **fallback_kwargs)
            print("启用 Chromium sandbox 启动失败，已回退为无 sandbox 模式。")
            return context, None
        except Exception as fallback_exc:
            return None, fallback_exc


async def launch_context(args: argparse.Namespace):
    """创建浏览器上下文，供领星自动化页面流程使用。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 playwright 依赖，请先运行：python -m pip install -r requirements.txt") from exc

    playwright = await async_playwright().start()
    browser_cdp_url = str(getattr(args, "browser_cdp_url", "") or "").strip()
    if browser_cdp_url:
        parsed = urlparse(browser_cdp_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
        ):
            await playwright.stop()
            raise RuntimeError("本机 Chrome 通道地址无效，拒绝连接非本机端点。")
        try:
            browser = await playwright.chromium.connect_over_cdp(
                browser_cdp_url,
                timeout=10000,
            )
            if not browser.contexts:
                raise RuntimeError("本机 Chrome 没有可用浏览器上下文。")
            return playwright, _AttachedBrowserContext(
                browser.contexts[0],
                browser,
            )
        except Exception as exc:
            await playwright.stop()
            raise OrderPageLoadFailed(
                "无法连接提交电脑上的可见 Chrome；请保持桌面程序和浏览器通道开启。"
            ) from exc

    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs = build_launch_kwargs(args)

    context, launch_error = await _launch_with_sandbox_fallback(playwright, profile_dir, launch_kwargs)
    if context is None and "channel" in launch_kwargs:
        print(f"没有成功打开 {args.browser_channel}，正在改用 Playwright 自带 Chromium。")
        bundled_kwargs = dict(launch_kwargs)
        bundled_kwargs.pop("channel", None)
        context, launch_error = await _launch_with_sandbox_fallback(playwright, profile_dir, bundled_kwargs)
    if context is None:
        await playwright.stop()
        raise launch_error
    await _install_headless_order_page_resource_guard(context, args)
    return playwright, context


class _AttachedBrowserContext:
    """Delegate to a CDP context without closing the operator's local Chrome."""

    def __init__(self, context, browser) -> None:
        self._context = context
        self._browser = browser

    def __getattr__(self, name: str):
        return getattr(self._context, name)

    async def close(self) -> None:
        # Task flows call context.close() in finally blocks.  For a desktop CDP
        # attachment, closing the default context would also close the visible
        # operator browser.  Playwright.stop() safely detaches the connection.
        return None

async def get_first_page(context):
    """获取浏览器上下文中的第一个页面，必要时新建页面。"""
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.bring_to_front()
    except Exception:
        pass
    return page

async def is_login_page(page) -> bool:
    """判断当前页面是否停留在领星登录页。"""
    if "/login" in page.url:
        return True
    try:
        account_count = await page.locator('input[name="account"]').count()
        password_count = await page.locator('input[name="pwd"]').count()
    except Exception:
        return False
    return account_count > 0 and password_count > 0

async def try_auto_login(page, login_config: LoginConfig) -> bool:
    """尝试使用配置中的账号密码完成领星自动登录。"""
    if not login_config.has_credentials:
        return False

    try:
        account_input = page.locator('input[name="account"]').first
        password_input = page.locator('input[name="pwd"]').first
        await account_input.wait_for(state="visible", timeout=5000)
        await password_input.wait_for(state="visible", timeout=5000)
        await account_input.fill(login_config.account or "")
        await password_input.fill(login_config.password or "")

        remember_checkbox = page.locator('input[name="autoLogin"]').first
        if await remember_checkbox.count():
            try:
                await remember_checkbox.set_checked(login_config.remember_login, force=True, timeout=2000)
            except Exception:
                pass

        await page.locator("button").filter(has_text="登录").first.click(timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        return True
    except Exception as exc:
        print(f"自动登录没有成功启动，请在浏览器里手动登录：{exc}")
        return False

async def wait_for_order_page(
    page,
    timeout_sec: int,
    login_config: LoginConfig,
    auto_login: bool,
    debug_dir: str | Path | None = None,
) -> None:
    """等待订单管理页面加载完成，并在需要时处理登录跳转。"""
    deadline = time.monotonic() + timeout_sec
    auto_login_attempted = False
    printed_manual_message = False
    while time.monotonic() < deadline:
        if _requires_mobile_binding(page.url):
            raise OrderPageAuthenticationRequired(
                "领星要求当前服务器完成手机绑定或设备验证；"
                "云端无头浏览器无法代替人工完成。请先完成一次服务器浏览器验证后再重新提交。"
            )
        # On the already-loaded order-management SPA, reading the entire body
        # can be very expensive over a remote CDP tunnel.  The unique visible
        # search root is the same readiness boundary used by page automation,
        # and needs only one small DOM query.
        if "mpOrderManagement" in str(page.url or ""):
            try:
                if await page.locator("#advanced-input:visible").count() == 1:
                    return
            except Exception:
                pass
        try:
            body_text = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            body_text = ""
        if "订单管理" in body_text and ("系统单号" in body_text or "平台单号" in body_text):
            return

        login_page = await is_login_page(page)
        if login_page:
            if auto_login and login_config.has_credentials and not auto_login_attempted:
                print("检测到领星登录页，正在使用 .env 中的账号密码自动登录。")
                auto_login_attempted = True
                await try_auto_login(page, login_config)
                await page.wait_for_timeout(1000)
                continue
            if not printed_manual_message:
                if auto_login and not login_config.has_credentials:
                    print("没有在 .env 中找到完整账号密码，请在浏览器里手动登录；脚本会自动继续。")
                else:
                    print("如果页面出现验证码、短信验证或登录异常，请在浏览器里手动处理；脚本会自动继续。")
                printed_manual_message = True
        elif not printed_manual_message:
            print("正在等待领星订单管理页面加载；如果浏览器需要登录，请先完成登录。")
            printed_manual_message = True

        if not login_page and "mpOrderManagement" not in page.url:
            print("当前不是领星订单管理页，正在跳转到订单管理页面。")
            await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            continue

        await page.wait_for_timeout(5000)
    message = "等待领星订单管理页面超时。请确认已经登录，并且页面能打开订单管理。"
    artifacts = await save_page_diagnostics(
        page,
        debug_dir or "debug/logs",
        "order_page_load_timeout",
        message,
        {"timeout_sec": timeout_sec, "url": page.url},
    )
    raise OrderPageLoadFailed(f"{message} 诊断文件：{artifacts.get('diagnostic_file')}")
