from __future__ import annotations

import re
import time


_ORDER_CLICK_READY_TIMEOUT_MS = 12000
_ORDER_CLICK_POLL_MS = 150
_monotonic = time.monotonic

_KNOWN_NOTICE_BUTTON_TEXTS = ("我知道了", "知道了", "关闭")
_ORDER_SEARCH_TRANSIENT_SELECTOR = (
    ".el-select-dropdown:visible,.el-autocomplete-suggestion:visible,"
    ".el-cascader-menus:visible,.el-picker-panel:visible"
)


async def dismiss_known_blocking_dialogs(page, *, timeout_ms: int = 5000) -> list[str]:
    """关闭已知的领星公告弹窗，并确认遮罩真正消失。

    公告弹窗使用全屏 ``.init-dialog`` wrapper。过去只按 Escape，弹窗如果没有
    响应就会继续覆盖订单列表。这里把操作限制在公告弹窗内部，只点击明确的确认/
    关闭按钮；遇到未知结构则停止，避免误点弹窗中的业务入口。
    """

    dismissed: list[str] = []
    deadline = _monotonic() + timeout_ms / 1000
    while _monotonic() < deadline:
        dialogs = page.locator(".el-dialog__wrapper.init-dialog:visible")
        count = await dialogs.count()
        if count == 0:
            return dismissed

        # Element UI 通常把最后挂载的弹窗放在最上层；每轮只处理一个，随后重新
        # 查询 DOM，避免关闭后 nth 索引变化。
        dialog = dialogs.last
        excerpt = " ".join((await dialog.inner_text()).split())[:120]
        # Playwright 的 ``has_text`` 正则匹配原始 textContent。领星按钮常见
        # ``<span> 关闭 </span>`` 这类首尾空白，带 ^/$ 的预筛选会在后面的
        # inner_text 归一化判断之前就把真实按钮排除掉。
        buttons = dialog.locator(
            "button:visible,a[role='button']:visible,[role='button']:visible"
        )

        target = None
        for index in range(await buttons.count()):
            candidate = buttons.nth(index)
            candidate_text = " ".join((await candidate.inner_text()).split())
            if candidate_text in _KNOWN_NOTICE_BUTTON_TEXTS and await candidate.is_enabled():
                target = candidate
                break
        if target is None:
            close_buttons = dialog.locator(
                ".el-dialog__headerbtn:visible,.el-dialog__close:visible,[aria-label='Close']:visible"
            )
            if await close_buttons.count():
                target = close_buttons.first

        if target is None:
            raise RuntimeError(
                "领星公告弹窗正在遮挡订单列表，但没有找到安全的关闭按钮："
                f"{excerpt or '未读取到弹窗文字'}。"
            )

        try:
            await target.click(timeout=max(250, int((deadline - _monotonic()) * 1000)))
            await dialog.wait_for(state="hidden", timeout=2000)
        except Exception as exc:
            raise RuntimeError(
                "领星公告弹窗关闭动作未生效，已停止点击底层订单："
                f"{excerpt or '未读取到弹窗文字'}。"
            ) from exc
        dismissed.append(excerpt)

    raise RuntimeError("领星公告弹窗在等待后仍未关闭，已停止点击底层订单。")


async def dismiss_order_search_overlays(page) -> list[str]:
    """收起订单搜索产生的临时弹层，避免其拦截列表点击。"""
    overlays = page.locator(_ORDER_SEARCH_TRANSIENT_SELECTOR)
    visible_classes: list[str] = []
    for index in range(await overlays.count()):
        overlay = overlays.nth(index)
        if await overlay.is_visible():
            visible_classes.append(str(await overlay.get_attribute("class") or "搜索弹层"))
    if not visible_classes:
        return []

    # Element UI 的下拉/建议框都支持 Escape 关闭。这里只处理明确属于搜索控件
    # 的临时弹层，不点击页面空白处，也不依赖弹层或订单行的屏幕位置。
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)
        if await page.locator(_ORDER_SEARCH_TRANSIENT_SELECTOR).count():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)
    except Exception:
        # 后续 Playwright click 的可操作性检查仍会阻止穿透点击；这里不改用
        # force/JS click 绕过安全检查。
        pass
    return visible_classes


async def _visible_detail_roots(page) -> list:
    """返回真正可见的订单详情根节点，忽略 Vue 留在 DOM 中的隐藏副本。"""
    roots = page.locator(".order-detail-dialog:visible")
    if await roots.count() == 0:
        roots = page.locator(
            ".el-dialog__wrapper:visible,.vxe-modal--wrapper:visible,.ant-drawer:visible,.el-drawer:visible"
        ).filter(has_text=re.compile(r"系统单号.*(?:收货信息|商品信息)", re.S))
    visible_roots = []
    for index in range(await roots.count()):
        root = roots.nth(index)
        try:
            if await root.is_visible():
                visible_roots.append(root)
        except Exception:
            continue
    return visible_roots


async def get_current_detail_identity(page) -> dict:
    """从唯一可见的订单详情根节点读取订单身份，不使用尺寸或位置阈值。"""
    visible_roots = await _visible_detail_roots(page)
    if len(visible_roots) != 1:
        return {
            "system_order_no": "",
            "system_order_nos": [],
            "platform_order_nos": [],
            "has_detail_root": False,
            "root_top": None,
            "text_excerpt": "",
            "visible_detail_count": len(visible_roots),
        }

    text = " ".join((await visible_roots[0].inner_text()).split())
    system_matches = re.findall(r"\b\d{15,24}\b", text)
    platform_matches = list(dict.fromkeys(re.findall(r"\b\d{3}-\d{7}-\d{7}\b", text)))
    header_match = re.search(r"系统单号\s*(\d{15,24})", text)
    current_system_order_no = (
        header_match.group(1)
        if header_match
        else (system_matches[0] if system_matches else "")
    )
    return {
        "system_order_no": current_system_order_no,
        "system_order_nos": list(dict.fromkeys(system_matches)),
        "platform_order_nos": platform_matches,
        "has_detail_root": True,
        "root_top": None,
        "text_excerpt": text[:600],
        "visible_detail_count": 1,
    }


async def assert_current_detail_order(
    page,
    expected_system_order_no: str,
    expected_platform_order_no: str | None = None,
    stage: str = "",
) -> dict:
    """校验当前详情弹窗是否匹配预期订单，防止写错单。"""
    identity = await get_current_detail_identity(page)
    current_system = str(identity.get("system_order_no") or "")
    platform_order_nos = [str(item) for item in identity.get("platform_order_nos") or []]
    stage_text = f"（{stage}）" if stage else ""
    if current_system != expected_system_order_no:
        raise RuntimeError(
            "订单上下文校验失败"
            f"{stage_text}：当前详情系统单号是 {current_system or '未识别'}，"
            f"期望系统单号是 {expected_system_order_no}。已停止，避免错写。"
        )
    if expected_platform_order_no and expected_platform_order_no not in platform_order_nos:
        raise RuntimeError(
            "订单上下文校验失败"
            f"{stage_text}：当前详情没有识别到目标平台单号 {expected_platform_order_no}，"
            f"识别到的平台单号为 {platform_order_nos or ['未识别']}。已停止，避免错写。"
        )
    return identity


async def close_order_detail_dialog(page) -> bool:
    """关闭唯一可见的订单详情并确认遮罩消失；返回是否已回到列表态。"""
    roots = await _visible_detail_roots(page)
    if not roots:
        return True
    if len(roots) != 1:
        return False

    root = roots[0]
    header = root.locator(
        ".el-dialog__header,.vxe-modal--header,.ant-modal-header,.el-drawer__header"
    )
    close_buttons = header.get_by_role("button", name="关闭", exact=True)
    if await close_buttons.count() == 0:
        close_buttons = root.locator(
            ".el-dialog__headerbtn:visible,.el-dialog__close:visible,.vxe-modal--close-btn:visible,"
            ".ant-modal-close:visible,.ant-drawer-close:visible,.el-drawer__close-btn:visible"
        )
    if await close_buttons.count() == 0:
        return False
    try:
        await close_buttons.first.click(timeout=2500)
        await root.wait_for(state="hidden", timeout=2500)
    except Exception:
        # 关闭事件可能已经派发，只是 Vue 在 mouseup 前替换了按钮或 wrapper。
        # 重新读取详情根节点，以最终页面状态为准；仍不使用坐标或强制点击。
        try:
            if not await _visible_detail_roots(page):
                return True
        except Exception:
            pass
        return False
    return not await _visible_detail_roots(page)


async def click_system_order(page, system_order_no: str) -> None:
    """通过可操作的 DOM 定位器点击系统单号，不依赖屏幕坐标或浏览器缩放。"""
    await dismiss_known_blocking_dialogs(page)
    await dismiss_order_search_overlays(page)
    deadline = _monotonic() + _ORDER_CLICK_READY_TIMEOUT_MS / 1000
    last_blocker = ""
    found = False

    while _monotonic() < deadline:
        # 详情 wrapper 覆盖整个列表时，后台的系统单号虽然仍在 DOM 中，却永远
        # 无法通过 Playwright 的可操作性检查。先按详情身份处理页面状态：目标
        # 详情已打开则直接复用；其他详情必须确认关闭后才允许点击底层列表。
        identity = await get_current_detail_identity(page)
        visible_detail_count = int(identity.get("visible_detail_count") or 0)
        if visible_detail_count > 1:
            raise RuntimeError(
                f"检测到 {visible_detail_count} 个可见订单详情，已停止点击系统单号 "
                f"{system_order_no}，避免在不明确的订单上下文中继续操作。"
            )
        if visible_detail_count == 1:
            current_system_order_no = str(identity.get("system_order_no") or "")
            if current_system_order_no == system_order_no:
                return
            if not await close_order_detail_dialog(page):
                raise RuntimeError(
                    "另一个订单详情正在遮挡订单列表且无法安全关闭："
                    f"当前系统单号 {current_system_order_no or '未识别'}，"
                    f"目标系统单号 {system_order_no}。"
                )
            await page.wait_for_timeout(_ORDER_CLICK_POLL_MS)
            continue

        # 领星的系统单号有时是显式链接，有时只是普通 span/td，点击监听器
        # 绑定在父级单元格或表格行。get_by_text 会优先返回精确文本节点，直接
        # 点击该节点既能触发事件冒泡，也不依赖 class、坐标、缩放或列位置。
        # 第二组定位器覆盖文本被子元素拆开的情况；每轮重新创建 locator，兼容
        # VXE/Element 虚拟表格在加载过程中替换整行 DOM。
        # 真实领星订单表把系统单号放在 ``tr[rowid=系统单号]`` 内唯一的
        # ``.ak-blue.ak-pointer`` 节点上。优先使用这个结构能避开 get_by_text
        # 同时命中 td/div/span 多层祖先节点的问题；后两组仅兼容旧页面结构。
        candidate_groups = (
            page.locator(
                f'tr[rowid="{system_order_no}"] .ak-blue.ak-pointer:visible'
            ),
            page.get_by_text(system_order_no, exact=True),
            page.locator(
                "a:visible,button:visible,[role='link']:visible,[role='button']:visible,"
                ".ak-pointer:visible,span:visible,[role='gridcell']:visible,td:visible,div:visible"
            ).filter(has_text=system_order_no),
        )
        for candidates in candidate_groups:
            count = await candidates.count()
            for index in range(count):
                if _monotonic() >= deadline:
                    break
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    if " ".join((await candidate.inner_text()).split()) != system_order_no:
                        continue
                    found = True
                    context = await candidate.evaluate(
                        """
                        (el, orderNo) => {
                            const row = el.closest(
                                'tr.vxe-body--row,tr[rowid],[role="row"],.el-table__row'
                            );
                            const rowId = row ? String(row.getAttribute('rowid') || '') : '';
                            const style = window.getComputedStyle(el);
                            return {
                                inDialog: Boolean(el.closest(
                                    '.order-detail-dialog,.el-dialog__wrapper,.el-dialog,' +
                                    '.vxe-modal--wrapper,.ant-modal,.ant-drawer,.el-drawer'
                                )),
                                explicit: el.matches(
                                    'a,button,[role="link"],[role="button"],.ak-pointer'
                                ) || style.cursor === 'pointer',
                                inOrderRow: Boolean(row),
                                wrongRow: Boolean(rowId && /^\\d{15,24}$/.test(rowId) && rowId !== orderNo),
                            };
                        }
                        """,
                        system_order_no,
                    )
                    if context["inDialog"] or context["wrongRow"]:
                        continue
                    if not context["explicit"] and not context["inOrderRow"]:
                        continue
                    remaining_ms = max(250, int((deadline - _monotonic()) * 1000))
                    await candidate.scroll_into_view_if_needed(
                        timeout=min(1500, remaining_ms)
                    )
                    await candidate.click(timeout=min(2500, remaining_ms))
                    return
                except Exception:
                    # 领星的点击处理会立即替换虚拟表格并异步挂载全屏详情。有时
                    # Playwright 因原节点在 mouseup 前被移除而报告失败，但详情
                    # 实际已经打开。每次异常后先核对详情身份，避免继续点击已被
                    # 详情覆盖的底层列表。
                    identity_after_click = await get_current_detail_identity(page)
                    if str(identity_after_click.get("system_order_no") or "") == system_order_no:
                        return
                    # 虚拟表格替换行会使当前 locator 短暂失效；下一轮会重新
                    # 查询。这里仅收集遮挡诊断，不使用 JS click 绕过安全检查。
                    try:
                        last_blocker = str(
                            await page.evaluate(
                                """
                                () => {
                                    const shown = (el) => {
                                        const style = window.getComputedStyle(el);
                                        return el.getClientRects().length > 0 &&
                                            style.visibility !== 'hidden' &&
                                            style.display !== 'none' &&
                                            style.pointerEvents !== 'none';
                                    };
                                    const overlays = Array.from(document.querySelectorAll(
                                         '.el-dialog__wrapper,.el-loading-mask,' +
                                         '.vxe-loading,.ant-spin-spinning,' +
                                         '.el-select-dropdown,.el-autocomplete-suggestion,' +
                                         '.el-cascader-menus,.el-picker-panel,' +
                                         '[class*="loading-mask"]'
                                    )).filter(shown);
                                    const top = overlays.at(-1);
                                    return top
                                        ? ((top.tagName || '') + '.' + String(top.className || '')).slice(0, 160)
                                        : '元素尚未通过 Playwright 可操作性检查';
                                }
                                """
                            )
                        )
                    except Exception:
                        last_blocker = "页面正在刷新或元素已被替换"

        # 公告可能在查询完成后延迟出现；每轮重新检查并只关闭已知公告。
        await dismiss_known_blocking_dialogs(page, timeout_ms=1000)
        await dismiss_order_search_overlays(page)
        await page.wait_for_timeout(_ORDER_CLICK_POLL_MS)

    # 最后一次 click 可能已经派发成功，只是目标节点随详情打开而被 Vue 移除，
    # 导致 Playwright 在截止时刻返回异常。抛错前再做一次订单身份校验。
    final_identity = await get_current_detail_identity(page)
    if str(final_identity.get("system_order_no") or "") == system_order_no:
        return

    if found:
        raise RuntimeError(
            f"已找到系统单号 {system_order_no}，但其文本节点在 "
            f"{_ORDER_CLICK_READY_TIMEOUT_MS // 1000} 秒内始终不可点击"
            f"（可能被页面遮挡：{last_blocker or '页面仍在加载'}）。"
        )
    raise RuntimeError(
        f"订单列表中没有找到系统单号：{system_order_no}。"
        "订单列表可能仍在刷新，或领星页面结构已经变化。"
    )


async def wait_for_detail(
    page,
    expected_system_order_no: str | None = None,
    timeout_ms: int = 22000,
) -> None:
    """等待目标订单详情语义节点稳定，不依赖弹窗尺寸、位置或页面缩放。"""
    deadline = _monotonic() + timeout_ms / 1000
    stable_signature = ""
    stable_since = 0.0

    while _monotonic() < deadline:
        await dismiss_known_blocking_dialogs(page, timeout_ms=1000)
        roots = page.locator(".order-detail-dialog:visible")
        if await roots.count() == 0:
            roots = page.locator(
                ".el-dialog__wrapper:visible,.vxe-modal--wrapper:visible,.ant-drawer:visible,.el-drawer:visible"
            ).filter(has_text=re.compile(r"系统单号.*收货信息.*(?:商品信息|交易信息)", re.S))

        matching = []
        for index in range(await roots.count()):
            root = roots.nth(index)
            if not await root.is_visible():
                continue
            content = " ".join((await root.inner_text()).split())
            headers = root.locator(
                ".el-dialog__header:visible,.vxe-modal--header:visible,"
                ".ant-modal-header:visible,.el-drawer__header:visible,"
                "header:visible"
            )
            header_texts = [
                " ".join((await headers.nth(header_index).inner_text()).split())
                for header_index in range(await headers.count())
            ]
            identity_headers = [
                text
                for text in header_texts
                if "系统单号" in text
                and (
                    not expected_system_order_no
                    or expected_system_order_no in text
                )
            ]
            if len(identity_headers) != 1:
                continue

            # 订单详情会记住用户上次停留的“操作日志/报关信息”页签。此时
            # 收货信息虽然存在于另一个 pane，却不属于 inner_text；等待逻辑
            # 不能因此把已经打开的详情误判为未加载。以稳定的页签骨架确认
            # 订单详情结构，后续写回代码会主动切回“基本信息”。
            tabs = root.locator("[role='tab']:visible,.el-tabs__item:visible")
            tab_texts = {
                " ".join((await tabs.nth(tab_index).inner_text()).split())
                for tab_index in range(await tabs.count())
            }
            has_detail_tabs = (
                "基本信息" in tab_texts and "操作日志" in tab_texts
            )
            has_visible_legacy_content = (
                "收货信息" in content
                and any(
                    label in content
                    for label in ("商品信息", "更多商品信息", "交易信息")
                )
            )
            if not has_detail_tabs and not has_visible_legacy_content:
                continue
            body = root.locator(
                ".el-dialog__body:visible,.vxe-modal--body:visible,"
                ".ant-modal-body:visible,.el-drawer__body:visible"
            )
            if has_detail_tabs and await body.count() != 1:
                continue
            matching.append(
                (
                    root,
                    f"{identity_headers[0]}|{'|'.join(sorted(tab_texts))}|{content[:800]}",
                )
            )

        if len(matching) == 1:
            root, content = matching[0]
            loading = root.locator(
                ".el-loading-mask:visible,.vxe-loading:visible,.ant-spin-spinning:visible,"
                "[class*='loading-mask']:visible"
            )
            if await loading.count() == 0:
                signature = f"{expected_system_order_no or ''}|{content[:1200]}"
                now = _monotonic()
                if signature != stable_signature:
                    stable_signature = signature
                    stable_since = now
                elif now - stable_since >= 0.25:
                    return
            else:
                stable_signature = ""
                stable_since = 0.0
        else:
            stable_signature = ""
            stable_since = 0.0
        await page.wait_for_timeout(_ORDER_CLICK_POLL_MS)

    order_text = expected_system_order_no or "目标订单"
    raise RuntimeError(
        f"已点击系统单号 {order_text}，但领星订单详情在 "
        f"{max(1, timeout_ms // 1000)} 秒内没有完成加载。"
        "请检查领星页面是否出现登录验证、接口异常或持续加载遮罩。"
    )
