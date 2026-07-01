from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from ..constants import ORDER_MANAGEMENT_URL
from ..models import LoginConfig
from ..pages.diagnostics import save_page_diagnostics


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
    return playwright, context

async def get_first_page(context):
    """获取浏览器上下文中的第一个页面，必要时新建页面。"""
    if context.pages:
        return context.pages[0]
    return await context.new_page()

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
    raise RuntimeError(f"{message} 诊断文件：{artifacts.get('diagnostic_file')}")
