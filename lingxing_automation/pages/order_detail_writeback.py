from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..models import ContactInfo
from ..parsers.contact import normalize_phone
from .order_detail_navigation import (
    assert_current_detail_order,
    close_order_detail_dialog,
    click_system_order,
    wait_for_detail,
)

WriteConfirmCallback = Callable[[dict[str, Any]], Awaitable[bool]]
_CONTACT_SAVE_STATE_TIMEOUT_MS = 60_000


async def _visible_locator_items(locator, *, editable_only: bool = False) -> list[Any]:
    """返回当前真正可见的定位器；可选地要求控件可编辑。"""
    items: list[Any] = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        try:
            if not await item.is_visible():
                continue
            if editable_only and not await item.is_editable():
                continue
        except Exception:
            continue
        items.append(item)
    return items


async def _locator_relation_to_root(locator, root) -> tuple[bool, bool]:
    """返回定位器是否位于 root 内，以及它是否就是 root。"""
    locator_handle = await locator.element_handle()
    root_handle = await root.element_handle()
    if locator_handle is None or root_handle is None:
        if locator_handle is not None:
            await locator_handle.dispose()
        if root_handle is not None:
            await root_handle.dispose()
        return False, False
    try:
        is_within = bool(
            await locator_handle.evaluate(
                "(node, targetRoot) => node === targetRoot || targetRoot.contains(node)",
                root_handle,
            )
        )
        is_root = bool(
            await locator_handle.evaluate(
                "(node, targetRoot) => node === targetRoot",
                root_handle,
            )
        )
        return is_within, is_root
    finally:
        await locator_handle.dispose()
        await root_handle.dispose()


async def _detail_root_locator(page):
    roots = await _visible_locator_items(page.locator(".order-detail-dialog:visible"))
    if not roots:
        roots = await _visible_locator_items(
            page.locator(
                ".el-dialog__wrapper:visible,.vxe-modal--wrapper:visible,.ant-drawer:visible,.el-drawer:visible"
            ).filter(has_text=re.compile(r"系统单号.*(?:收货信息|商品信息)", re.S))
        )
    if len(roots) > 1:
        raise RuntimeError(f"检测到 {len(roots)} 个可见订单详情，已停止以避免写错订单。")
    return roots[0] if roots else None


async def _ensure_basic_info_tab(page, *, timeout_ms: int = 5000) -> bool:
    """确保订单详情显示基本信息页签，避免复用上次停留的操作日志状态。"""
    detail = await _detail_root_locator(page)
    if detail is None:
        return False

    tabs = detail.locator(
        "[role='tab']:visible,.el-tabs__item:visible"
    )
    basic_tabs: list[Any] = []
    for tab in await _visible_locator_items(tabs):
        if " ".join((await tab.inner_text()).split()) == "基本信息":
            basic_tabs.append(tab)
    if len(basic_tabs) != 1:
        raise RuntimeError(
            f"没有唯一定位到订单详情“基本信息”页签（找到 {len(basic_tabs)} 个）。"
        )

    basic_tab = basic_tabs[0]

    async def is_active() -> bool:
        class_name = str(await basic_tab.get_attribute("class") or "")
        return (
            "is-active" in class_name.split()
            or await basic_tab.get_attribute("aria-selected") == "true"
        )

    if await is_active():
        return True
    click_error: Exception | None = None
    try:
        await basic_tab.click(timeout=min(5000, timeout_ms))
    except Exception as exc:
        # Vue 可能在派发页签点击后立刻替换节点；以最终激活状态为准。
        click_error = exc

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if await is_active():
                return True
        except Exception:
            # 页签节点被替换后重新按语义查询。
            tabs = detail.locator("[role='tab']:visible,.el-tabs__item:visible")
            refreshed = []
            for tab in await _visible_locator_items(tabs):
                if " ".join((await tab.inner_text()).split()) == "基本信息":
                    refreshed.append(tab)
            if len(refreshed) == 1:
                basic_tab = refreshed[0]
        await page.wait_for_timeout(100)
    if click_error is not None:
        raise RuntimeError("订单详情“基本信息”页签不可点击或被遮挡。") from click_error
    raise RuntimeError("点击“基本信息”后页签没有进入激活状态。")


async def _shipping_root_locator(page):
    await _ensure_basic_info_tab(page)
    detail = await _detail_root_locator(page)
    if detail is None:
        return None

    preferred = []
    for item in await _visible_locator_items(detail.locator(".receive-info:visible")):
        content = " ".join((await item.inner_text()).split())
        if all(label in content for label in ("收货信息", "电话", "买家邮箱")):
            preferred.append(item)
    if len(preferred) == 1:
        return preferred[0]
    if len(preferred) > 1:
        raise RuntimeError(f"检测到 {len(preferred)} 个收货信息区域，已停止以避免写错字段。")

    # 兼容领星只调整 class 的情况：仍只按“收货信息/电话/买家邮箱”的 DOM
    # 包含关系选择最小语义区域，不使用任何 top/left/宽高阈值。
    regions = detail.locator("section:visible,article:visible,div:visible").filter(
        has_text="收货信息"
    )
    candidates: list[tuple[int, Any]] = []
    for item in await _visible_locator_items(regions):
        content = " ".join((await item.inner_text()).split())
        if all(label in content for label in ("收货信息", "电话", "买家邮箱")):
            candidates.append((len(content), item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


async def _detail_header_action_group(page):
    """返回详情头部唯一动作组；编辑、保存和取消都属于该组。"""
    detail = await _detail_root_locator(page)
    if detail is None:
        return None
    headers = detail.locator(
        ".el-dialog__header:visible,.vxe-modal--header:visible,"
        ".ant-modal-header:visible,.el-drawer__header:visible"
    )
    matching_headers: list[Any] = []
    for header in await _visible_locator_items(headers):
        content = " ".join((await header.inner_text()).split())
        if "系统单号" in content:
            matching_headers.append(header)
    if len(matching_headers) > 1:
        raise RuntimeError(f"检测到 {len(matching_headers)} 个订单详情头部，已停止以避免误点。")
    if not matching_headers:
        return None
    action_groups = await _visible_locator_items(
        matching_headers[0].locator(".header-operate:visible")
    )
    if len(action_groups) > 1:
        raise RuntimeError(f"检测到 {len(action_groups)} 个详情头部按钮组，已停止以避免误点。")
    return action_groups[0] if action_groups else matching_headers[0]


async def _exact_interactive_actions(container, labels: tuple[str, ...]) -> list[Any]:
    if container is None:
        return []
    candidates = container.locator(
        "button:visible,a:visible,[role='button']:visible"
    )
    actions: list[Any] = []
    for candidate in await _visible_locator_items(candidates):
        text = " ".join((await candidate.inner_text()).split())
        if text not in labels:
            continue
        if await candidate.get_attribute("aria-disabled") == "true":
            continue
        try:
            if not await candidate.is_enabled():
                continue
        except Exception:
            continue
        actions.append(candidate)
    return actions


async def _exact_contact_labels(root, label_text: str) -> list[Any]:
    """按渲染后的归一化文字找联系方式标签，兼容领星模板首尾空白。"""
    labels: list[Any] = []
    candidates = root.locator("label:visible,span:visible,div:visible,p:visible")
    for candidate in await _visible_locator_items(candidates):
        text = " ".join((await candidate.inner_text()).split())
        if text == label_text or text == f"{label_text}*":
            labels.append(candidate)
    return labels


async def _structured_contact_row(root, label_text: str):
    """Return the unique Lingxing contact row without scanning every descendant.

    The current order detail renders each receiver field as an ``.info-wrapper``
    whose direct ``.label`` child names the field.  Keeping this as the primary
    path matters when Playwright reaches the browser through the production SSH
    tunnel: walking hundreds of ``div/span`` nodes one RPC at a time can turn a
    simple phone lookup into several minutes.  The selector remains scoped to
    the verified ``.receive-info`` root and requires one exact, unique label, so
    it is independent of zoom/layout without weakening wrong-field protection.
    """

    label_pattern = re.compile(rf"^\s*{re.escape(label_text)}\*?\s*$")
    labels = root.locator(".info-wrapper:visible > .label:visible").filter(
        has_text=label_pattern
    )
    count = await labels.count()
    if count > 1:
        raise RuntimeError(
            f"检测到 {count} 个“{label_text}”联系方式字段，已停止以避免写错字段。"
        )
    if count == 0:
        return None
    row = labels.first.locator("xpath=parent::*")
    return row if await row.count() == 1 else None


async def _contact_field_locator(page, field: str, *, editable_only: bool):
    if field not in {"phone", "email"}:
        raise ValueError(f"未知联系方式字段：{field}")
    root = await _shipping_root_locator(page)
    if root is None:
        return None

    label_text = "电话" if field == "phone" else "买家邮箱"
    controls_selector = (
        "input:not([type='hidden']):visible,textarea:visible,[contenteditable='true']:visible"
    )

    # Fast path for the real Lingxing DOM.  This resolves the field with a
    # handful of browser-side selector operations instead of sequentially
    # interrogating every descendant over the remote browser tunnel.
    structured_row = await _structured_contact_row(root, label_text)
    if structured_row is not None:
        controls = await _visible_locator_items(
            structured_row.locator(controls_selector),
            editable_only=editable_only,
        )
        if len(controls) == 1:
            return controls[0]
        if len(controls) > 1:
            raise RuntimeError(
                f"“{label_text}”联系方式行包含 {len(controls)} 个可编辑控件，"
                "已停止以避免写错字段。"
            )
        # A structured row with no matching control is the normal read-only
        # presentation.  Falling through would reintroduce the expensive broad
        # descendant scan and cannot discover a safer control than this exact
        # row already did.
        return None

    # Legacy fallback for older detail templates that do not expose
    # ``.info-wrapper > .label``.
    labels = await _exact_contact_labels(root, label_text)

    for label in labels:
        label_for = await label.get_attribute("for")
        if label_for:
            escaped_label_for = label_for.replace("\\", "\\\\").replace('"', '\\"')
            linked = root.locator(f'[id="{escaped_label_for}"]')
            linked_items = await _visible_locator_items(linked, editable_only=editable_only)
            if len(linked_items) == 1:
                return linked_items[0]

        container = label
        for _depth in range(7):
            is_within_root, is_root = await _locator_relation_to_root(container, root)
            if not is_within_root:
                break
            controls = await _visible_locator_items(
                container.locator(controls_selector),
                editable_only=editable_only,
            )
            if len(controls) == 1:
                return controls[0]
            if is_root:
                break
            container = container.locator("xpath=parent::*")
            if await container.count() == 0:
                break

    attribute_selectors = (
        (
            "input[name*='phone' i]:visible,input[id*='phone' i]:visible,"
            "input[placeholder*='电话']:visible,input[aria-label*='电话']:visible"
        )
        if field == "phone"
        else (
            "input[type='email']:visible,input[name*='email' i]:visible,input[id*='email' i]:visible,"
            "input[placeholder*='邮箱']:visible,input[aria-label*='邮箱']:visible"
        )
    )
    fallbacks = await _visible_locator_items(
        root.locator(attribute_selectors),
        editable_only=editable_only,
    )
    return fallbacks[0] if len(fallbacks) == 1 else None


async def has_editable_contact_controls(page) -> bool:
    """判断收货信息语义区域内是否存在可编辑电话或邮箱控件。"""
    if await _contact_field_locator(page, "phone", editable_only=True) is not None:
        return True
    return await _contact_field_locator(page, "email", editable_only=True) is not None


async def _wait_for_contact_edit_state(page, *, editable: bool, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        current_state = await has_editable_contact_controls(page)
        if current_state == editable:
            return True
        if time.monotonic() >= deadline:
            return False
        await page.wait_for_timeout(200)


async def try_open_edit_mode(page) -> None:
    """切到基本信息并点击详情头部真实编辑按钮，等待联系方式进入编辑态。"""
    await _ensure_basic_info_tab(page)
    if await has_editable_contact_controls(page):
        return
    action_group = await _detail_header_action_group(page)
    actions = await _exact_interactive_actions(action_group, ("编辑", "修改"))
    if len(actions) != 1:
        raise RuntimeError(
            "没有唯一定位到订单详情头部的编辑按钮"
            f"（找到 {len(actions)} 个），已停止以避免误点其他编辑入口。"
        )
    click_error: Exception | None = None
    try:
        await actions[0].click(timeout=5000)
    except Exception as exc:
        click_error = exc
    await _ensure_basic_info_tab(page)
    if not await _wait_for_contact_edit_state(page, editable=True, timeout_ms=8000):
        if click_error is not None:
            raise RuntimeError("联系方式编辑按钮不可点击或被页面遮挡。") from click_error
        raise RuntimeError("点击联系方式编辑按钮后，收货信息区域没有进入可编辑状态。")


async def fill_shipping_contact_field(page, field: str, value: str) -> bool:
    """按字段标签与表单包含关系填写联系方式，不依赖位置或缩放。"""
    control = await _contact_field_locator(page, field, editable_only=True)
    if control is None:
        return False
    try:
        await control.fill(value, timeout=5000)
        await control.press("Tab")
        await page.wait_for_timeout(100)
    except Exception:
        # 输入成功后 Vue 可能替换当前 input，导致 Tab 或后续等待报错；重新按
        # 字段标签读取最终控件值，不把已完成的填写误判为失败。
        pass
    control = await _contact_field_locator(page, field, editable_only=True)
    if control is None:
        return False
    actual = str(
        await control.evaluate(
            "(el) => ('value' in el ? String(el.value || '') : String(el.textContent || '')).trim()"
        )
    )
    return actual == value


async def _contact_action_diagnostics(page) -> str:
    """生成不含客户数据的页面动作诊断，供失败消息和截图日志配套使用。"""
    try:
        detail = await _detail_root_locator(page)
        if detail is None:
            return "订单详情已不可见"
        facts = dict(
            await detail.evaluate(
                """
                (root) => {
                    const shown = (el) => {
                        const style = window.getComputedStyle(el);
                        return el.getClientRects().length > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '')
                        .replace(/\\s+/g, ' ').trim();
                    const actions = Array.from(root.querySelectorAll('button,a,[role="button"]'))
                        .filter(shown)
                        .map((el) => ({
                            tag: el.tagName,
                            text: textOf(el).slice(0, 32),
                            disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
                            className: String(el.className || '').slice(0, 80),
                        }))
                        .filter((item) => /^(编辑|修改|保存|取消)$/.test(item.text));
                    const overlays = Array.from(document.querySelectorAll(
                        '.el-dialog__wrapper.init-dialog,.el-loading-mask,[class*="loading-mask"]'
                    ))
                        .filter(shown)
                        .map((el) => String(el.className || '').slice(0, 100));
                    return {
                        devicePixelRatio: window.devicePixelRatio,
                        innerWidth: window.innerWidth,
                        innerHeight: window.innerHeight,
                        actions,
                        overlays,
                    };
                }
                """
            )
        )
        return (
            f"缩放指标={facts.get('devicePixelRatio')}，"
            f"视口={facts.get('innerWidth')}x{facts.get('innerHeight')}，"
            f"动作按钮={facts.get('actions') or []}，"
            f"遮罩={facts.get('overlays') or []}"
        )
    except Exception as exc:
        return f"页面诊断读取失败：{type(exc).__name__}"


async def click_save_button(
    page,
    *,
    state_timeout_ms: int = _CONTACT_SAVE_STATE_TIMEOUT_MS,
) -> bool:
    """点击唯一的联系方式保存按钮，并确认表单确实退出编辑态。"""
    action_group = await _detail_header_action_group(page)
    actions = await _exact_interactive_actions(action_group, ("保存",))
    if not actions:
        return False
    if len(actions) != 1:
        raise RuntimeError(f"检测到 {len(actions)} 个联系方式保存按钮，已停止以避免误点。")
    if not await has_editable_contact_controls(page):
        raise RuntimeError("找到保存按钮，但收货信息区域不在编辑状态，已停止。")
    click_error: Exception | None = None
    try:
        await actions[0].click(timeout=5000)
    except Exception as exc:
        click_error = exc

    if not await _wait_for_contact_edit_state(
        page,
        editable=False,
        timeout_ms=state_timeout_ms,
    ):
        diagnostics = await _contact_action_diagnostics(page)
        if click_error is not None:
            raise RuntimeError(
                f"联系方式保存按钮不可点击或被遮挡；{diagnostics}"
            ) from click_error
        raise RuntimeError(
            "保存按钮点击后未生效：联系方式表单仍处于编辑状态；"
            f"{diagnostics}"
        )

    action_group = await _detail_header_action_group(page)
    save_actions = await _exact_interactive_actions(action_group, ("保存",))
    edit_actions = await _exact_interactive_actions(action_group, ("编辑", "修改"))
    if save_actions or len(edit_actions) != 1:
        diagnostics = await _contact_action_diagnostics(page)
        raise RuntimeError(
            "联系方式输入框已退出编辑态，但保存后的按钮状态不明确，已停止关闭重开；"
            f"{diagnostics}"
        )
    return True


async def click_cancel_edit_button(page) -> bool:
    """只点击联系方式操作栏中的取消按钮，并确认退出编辑态。"""
    action_group = await _detail_header_action_group(page)
    actions = await _exact_interactive_actions(action_group, ("取消",))
    if len(actions) != 1:
        return False
    try:
        await actions[0].click(timeout=5000)
    except Exception:
        # 与保存相同，点击派发后 Vue 替换按钮并不代表取消没有生效。
        pass
    return await _wait_for_contact_edit_state(page, editable=False, timeout_ms=5000)


def _extract_contact_value(field: str, text: str) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    if field == "email":
        match = re.search(
            r'[^\s/@<>()\[\]";,:：]+@[A-Z0-9][A-Z0-9.\-]*\.[A-Z]{2,}',
            compact,
            flags=re.IGNORECASE,
        )
        return match.group(0) if match else ""
    after_label = re.sub(r"^.*?电话\*?\s*", " ", compact)
    match = re.search(r"\+?\d[\d\s().\-]{5,34}\d", after_label)
    return "".join(match.group(0).split()) if match else ""


async def _read_shipping_contact_value(page, field: str) -> str:
    control = await _contact_field_locator(page, field, editable_only=False)
    if control is not None:
        raw = str(
            await control.evaluate(
                "(el) => ('value' in el ? String(el.value || '') : String(el.textContent || '')).trim()"
            )
        )
        return _extract_contact_value(field, raw) or raw

    root = await _shipping_root_locator(page)
    if root is None:
        return ""
    label_text = "电话" if field == "phone" else "买家邮箱"

    structured_row = await _structured_contact_row(root, label_text)
    if structured_row is not None:
        row_text = " ".join((await structured_row.inner_text()).split())
        extracted = _extract_contact_value(field, row_text)
        if extracted:
            return extracted

    labels = await _exact_contact_labels(root, label_text)
    for label in labels:
        container = label.locator("xpath=parent::*")
        for _depth in range(6):
            if await container.count() == 0:
                break
            is_within_root, is_root = await _locator_relation_to_root(container, root)
            if not is_within_root:
                break
            row_text = " ".join((await container.inner_text()).split())
            if label_text in row_text and len(row_text) <= 320:
                extracted = _extract_contact_value(field, row_text)
                if extracted:
                    return extracted
            if is_root:
                break
            container = container.locator("xpath=parent::*")
    return ""


async def read_shipping_contact_values(page) -> dict[str, str]:
    """读取收货信息语义区域中的电话和邮箱，不以屏幕位置配对。"""
    return {
        "phone": await _read_shipping_contact_value(page, "phone"),
        "email": await _read_shipping_contact_value(page, "email"),
    }


async def fill_contact_fields(page, contact: ContactInfo) -> tuple[bool, str]:
    """把提取到的电话和邮箱写入详情页对应字段。"""
    await try_open_edit_mode(page)
    changed: list[str] = []

    if contact.phone:
        filled_phone = await fill_shipping_contact_field(page, "phone", contact.phone)
        if filled_phone:
            changed.append("电话")

    if contact.email:
        filled_email = await fill_shipping_contact_field(page, "email", contact.email)
        if filled_email:
            changed.append("买家邮箱")

    if not changed:
        return False, "没有在详情页“基本信息-收货信息”区域找到可编辑的电话/买家邮箱输入框。"

    try:
        saved = await click_save_button(page)
    except RuntimeError as exc:
        return False, str(exc)
    if not saved:
        return False, f"已填入 {'、'.join(changed)}，但没有找到保存按钮，请在浏览器里检查后手动保存。"
    return True, f"已填入并点击保存：{'、'.join(changed)}。"


def verify_saved_contact_values(contact: ContactInfo, saved_values: dict[str, str]) -> str | None:
    """保存后复核页面值；失败时返回错误说明，成功时返回 None。"""
    if contact.phone:
        expected_phone = normalize_phone(contact.phone)
        actual_phone = normalize_phone(saved_values.get("phone") or "")
        if not actual_phone:
            return "保存后没有重新读取到电话，已停止标记成功。"
        if expected_phone != actual_phone:
            return f"保存后电话校验失败：期望 {contact.phone}，页面为 {saved_values.get('phone') or '-'}。"

    if contact.email:
        expected_email = contact.email.strip().lower()
        actual_email = (saved_values.get("email") or "").strip().lower()
        if not actual_email:
            return "保存后没有重新读取到买家邮箱，已停止标记成功。"
        if expected_email != actual_email:
            return f"保存后买家邮箱校验失败：期望 {contact.email}，页面为 {saved_values.get('email') or '-'}。"

    return None

async def wait_for_saved_contact_values(
    page,
    contact: ContactInfo,
    *,
    timeout_ms: int = 10000,
    interval_ms: int = 500,
) -> tuple[dict[str, str], str | None]:
    """保存后轮询收货信息，等待 ERP 把新值刷新到详情页。

    领星保存后有时会先短暂显示旧值，如果只读一次会把已经成功保存的订单误判失败。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last_values: dict[str, str] = {}
    last_error: str | None = None
    while True:
        values = await read_shipping_contact_values(page)
        error = verify_saved_contact_values(contact, values)
        if error is None:
            return values, None
        last_values = values
        last_error = error
        if time.monotonic() >= deadline:
            return last_values, f"{last_error}（已等待 {timeout_ms // 1000} 秒刷新）"
        await page.wait_for_timeout(interval_ms)


async def _update_current_detail_contact_impl(
    page,
    contact: ContactInfo,
    *,
    expected_system_order_no: str,
    expected_platform_order_no: str | None = None,
    source_system_order_no: str | None = None,
    confirm_callback: WriteConfirmCallback | None = None,
) -> tuple[bool, str]:
    """更新当前详情弹窗中的联系方式并返回写回结果。"""
    before_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "写入前",
    )
    await try_open_edit_mode(page)
    edit_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "进入编辑后/写入前",
    )
    before_values = await read_shipping_contact_values(page)

    if confirm_callback is None:
        return False, "缺少保存前 CMD 二次确认回调，已停止写入。"

    changed: list[str] = []
    if contact.phone:
        filled_phone = await fill_shipping_contact_field(page, "phone", contact.phone)
        if filled_phone:
            changed.append("电话")
    if contact.email:
        filled_email = await fill_shipping_contact_field(page, "email", contact.email)
        if filled_email:
            changed.append("买家邮箱")

    if not changed:
        return False, "没有在详情页“基本信息-收货信息”区域找到可编辑的电话/买家邮箱输入框。"

    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "已填入/保存前",
    )
    after_fill_values = await read_shipping_contact_values(page)
    # 保存按钮前先校验填入后的页面值；如果页面读回值和待写入值不一致，
    # 必须取消编辑，避免用户确认后保存错误联系方式。
    fill_verify_message = verify_saved_contact_values(contact, after_fill_values)
    if fill_verify_message:
        canceled = await click_cancel_edit_button(page)
        cancel_message = "已点击取消，未保存。" if canceled else "未找到取消按钮，请在浏览器里手动取消或保存。"
        return False, f"填入后页面值校验失败，已停止保存：{fill_verify_message}{cancel_message}"
    confirmed = await confirm_callback(
        {
            "expected_system_order_no": expected_system_order_no,
            "expected_platform_order_no": expected_platform_order_no,
            "current_identity": edit_identity,
            "source_system_order_no": source_system_order_no or expected_system_order_no,
            "phone": contact.phone,
            "email": contact.email,
            "before_values": before_values,
            "after_fill_values": after_fill_values,
            "source_excerpt": contact.source_excerpt,
        }
    )
    if not confirmed:
        canceled = await click_cancel_edit_button(page)
        cancel_message = "已点击取消，未保存。" if canceled else "未找到取消按钮，请在浏览器里手动取消或保存。"
        return False, f"用户未在 CMD 中确认保存，已跳过。{cancel_message}"
    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "用户确认后/保存前",
    )
    try:
        saved = await click_save_button(page)
    except RuntimeError as exc:
        return False, str(exc)
    if not saved:
        return False, f"已填入 {'、'.join(changed)}，但没有找到保存按钮，请在浏览器里检查后手动保存。"
    await page.wait_for_timeout(800)
    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "保存后/重新打开校验前",
    )
    # 不能继续相信刚才编辑表单里的内存值。领星偶尔会让输入框保留新值，
    # 但后端实际没有保存。关闭并重新打开详情页，强制从服务器重新加载后
    # 再校验，只有持久化值一致才允许记录联系方式完成。
    if not await close_order_detail_dialog(page):
        return (
            False,
            "保存后无法确认订单详情已经关闭，已停止重新打开和持久化校验，"
            "避免把页面内存值误判为服务器保存结果。",
        )
    await click_system_order(page, expected_system_order_no)
    await wait_for_detail(page, expected_system_order_no)
    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "保存后/重新打开详情",
    )
    after_save_values, verify_message = await wait_for_saved_contact_values(page, contact)
    if verify_message:
        return False, f"重新打开订单后持久化校验失败：{verify_message}"
    after_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "保存后",
    )
    return (
        True,
        "已校验订单上下文并保存："
        f"{'、'.join(changed)}。"
        f" 写入前值={before_values}，填入后值={after_fill_values}，保存后值={after_save_values}，"
        f"重新打开后系统单号={after_identity.get('system_order_no')}。",
    )


async def update_current_detail_contact(
    page,
    contact: ContactInfo,
    *,
    expected_system_order_no: str,
    expected_platform_order_no: str | None = None,
    source_system_order_no: str | None = None,
    confirm_callback: WriteConfirmCallback | None = None,
) -> tuple[bool, str]:
    """Update a verified detail and always close it before returning or failing."""

    try:
        return await _update_current_detail_contact_impl(
            page,
            contact,
            expected_system_order_no=expected_system_order_no,
            expected_platform_order_no=expected_platform_order_no,
            source_system_order_no=source_system_order_no,
            confirm_callback=confirm_callback,
        )
    finally:
        await close_order_detail_dialog(page)


async def update_contact_for_system_orders(
    page,
    system_order_nos: list[str],
    contact: ContactInfo,
    *,
    expected_platform_order_no: str | None = None,
    source_system_order_no: str | None = None,
    confirm_callback: WriteConfirmCallback | None = None,
) -> tuple[list[str], list[str]]:
    """遍历系统订单列表并为匹配订单写回联系方式。"""
    updated: list[str] = []
    messages: list[str] = []
    for system_order_no in system_order_nos:
        await close_order_detail_dialog(page)
        await click_system_order(page, system_order_no)
        await wait_for_detail(page, system_order_no)
        saved, message = await update_current_detail_contact(
            page,
            contact,
            expected_system_order_no=system_order_no,
            expected_platform_order_no=expected_platform_order_no,
            source_system_order_no=source_system_order_no,
            confirm_callback=confirm_callback,
        )
        messages.append(f"{system_order_no}: {message}")
        if saved:
            updated.append(system_order_no)
    return updated, messages
