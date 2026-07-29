from __future__ import annotations

import time


_ORDER_CLICK_READY_TIMEOUT_MS = 12000
_ORDER_CLICK_POLL_MS = 150
_ORDER_CLICK_STABLE_MS = 150
_monotonic = time.monotonic


async def get_current_detail_identity(page) -> dict:
    """读取当前详情弹窗中的系统单号和平台单号身份信息。"""
    return dict(
        await page.evaluate(
            """
            () => {
                const systemRe = /\\b\\d{15,24}\\b/g;
                const platformRe = /\\b\\d{3}-\\d{7}-\\d{7}\\b/g;
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll(
                    '.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'
                ))
                    .filter((el) => {
                        if (el === document.body || el === document.documentElement || !visible(el)) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.top < 35 || rect.width < 500 || rect.height < 260) return false;
                        const text = textOf(el);
                        return /系统单号/.test(text) && /(收货信息|更多商品信息|交易信息|商品信息)/.test(text);
                    })
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = textOf(el);
                        return { el, rect, text, area: rect.width * rect.height };
                    })
                    .sort((a, b) => a.text.length - b.text.length || a.area - b.area);
                const root = roots[0] || null;
                const text = root ? root.text : '';
                const headerMatch = text.match(/系统单号\\s*(\\d{15,24})/);
                const systemMatches = Array.from(text.matchAll(systemRe)).map((match) => match[0]);
                const platformMatches = Array.from(new Set(Array.from(text.matchAll(platformRe)).map((match) => match[0])));
                return {
                    system_order_no: headerMatch ? headerMatch[1] : (systemMatches[0] || ''),
                    system_order_nos: Array.from(new Set(systemMatches)),
                    platform_order_nos: platformMatches,
                    has_detail_root: Boolean(root),
                    root_top: root ? Math.round(root.rect.top) : null,
                    text_excerpt: text.slice(0, 600),
                };
            }
            """
        )
    )


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


async def close_order_detail_dialog(page) -> None:
    """关闭订单详情弹窗并等待页面回到列表状态。"""
    closed = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const rootSelectors = [
                '.el-dialog__wrapper',
                '.el-dialog',
                '.vxe-modal--wrapper',
                '.vxe-modal--box',
                '.ant-modal',
                '.ant-modal-root',
                '.ant-drawer',
                '.el-drawer',
                '.order-detail-dialog',
                '[class*="dialog"]',
                '[class*="Dialog"]',
                '[class*="modal"]',
                '[class*="Modal"]',
                '[class*="drawer"]',
                '[class*="Drawer"]',
            ].join(',');
            const modalRoots = Array.from(document.querySelectorAll(rootSelectors));
            const detailRoots = Array.from(document.querySelectorAll('main,section,article,div'))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.top < 35 || rect.width < 500 || rect.height < 260) return false;
                    const text = textOf(el);
                    return /系统单号/.test(text) && /(收货信息|更多商品信息|交易信息|商品信息)/.test(text);
                });
            const roots = Array.from(new Set([...modalRoots, ...detailRoots]))
                .filter((el) => {
                    if (!visible(el)) return false;
                    const rect = el.getBoundingClientRect();
                    const text = textOf(el);
                    if (rect.top < 35) return false;
                    return /系统单号/.test(text) && /(收货信息|更多商品信息|交易信息|商品信息)/.test(text);
                })
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (ar.width * ar.height) - (br.width * br.height);
                });
            for (const root of roots) {
                const buttons = Array.from(root.querySelectorAll(
                    'button,a,.el-dialog__headerbtn,.el-dialog__close,.vxe-modal--close-btn,.ant-modal-close,.ant-drawer-close,.el-drawer__close-btn'
                ))
                    .filter((el) => visible(el))
                    .map((el) => ({ el, text: textOf(el), className: String(el.className || ''), rect: el.getBoundingClientRect() }));
                const closeButton =
                    buttons.find((item) => item.text === '关闭') ||
                    buttons.find((item) => /el-dialog__headerbtn|el-dialog__close|vxe-modal--close-btn|ant-modal-close|ant-drawer-close|el-drawer__close-btn/i.test(item.className));
                if (closeButton) {
                    window.__erpAutomationClosingDetail = root;
                    closeButton.el.click();
                    return true;
                }
            }
            return false;
        }
        """
    )
    if closed:
        try:
            await page.wait_for_function(
                """
                () => {
                    const root = window.__erpAutomationClosingDetail;
                    if (!root) return true;
                    const rect = root.getBoundingClientRect();
                    const style = window.getComputedStyle(root);
                    const closed = !root.isConnected ||
                        rect.width <= 0 || rect.height <= 0 ||
                        style.visibility === 'hidden' || style.display === 'none';
                    if (closed) window.__erpAutomationClosingDetail = null;
                    return closed;
                }
                """,
                timeout=2500,
            )
        except Exception:
            # The next identity check remains authoritative.  A slow close
            # animation must not add a fixed delay to every order.
            pass

async def click_system_order(page, system_order_no: str) -> None:
    """在订单列表中点击指定系统单号进入详情。"""
    deadline = _monotonic() + _ORDER_CLICK_READY_TIMEOUT_MS / 1000
    stable_target: tuple[int, int] | None = None
    stable_since = 0.0
    last_probe: dict = {}

    while _monotonic() < deadline:
        last_probe = dict(
            await page.evaluate(
                """
        (orderNo) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.bottom > 0 && rect.right > 0 &&
                    rect.top < window.innerHeight && rect.left < window.innerWidth &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '')
                .replace(/\\s+/g, ' ')
                .trim();
            const candidates = Array.from(document.querySelectorAll(
                'a,button,[role="link"],[role="button"],span,[class*="pointer"],td,div'
            ))
                .filter((el) => visible(el) && textOf(el) === orderNo)
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const tag = el.tagName.toLowerCase();
                    const role = String(el.getAttribute('role') || '').toLowerCase();
                    const className = String(el.className || '');
                    const explicitlyClickable =
                        tag === 'a' || tag === 'button' ||
                        role === 'link' || role === 'button' ||
                        /(?:^|\\s)(?:ak-pointer|pointer)(?:\\s|$)/i.test(className) ||
                        style.cursor === 'pointer';
                    const clickRank = explicitlyClickable ? 0 : 1000;
                    const semanticRank =
                        /(?:^|\\s)ak-pointer(?:\\s|$)/i.test(className) ? 0 :
                        (tag === 'a' || tag === 'button' || role === 'link' || role === 'button') ? 1 :
                        style.cursor === 'pointer' ? 2 : 10;
                    return {
                        el,
                        rect,
                        className,
                        tag,
                        explicitlyClickable,
                        score: clickRank + semanticRank * 100 +
                            Math.min(rect.width * rect.height, 100000) / 100000,
                    };
                })
                .sort((a, b) => a.score - b.score);
            const target = candidates[0] || null;
            if (!target) {
                return {
                    found: false,
                    ready: false,
                    candidateCount: 0,
                    blocker: '',
                };
            }

            const x = target.rect.left + target.rect.width / 2;
            const y = target.rect.top + target.rect.height / 2;
            const hit = document.elementFromPoint(x, y);
            const ready = Boolean(
                target.explicitlyClickable &&
                hit &&
                (hit === target.el || target.el.contains(hit))
            );
            return {
                found: true,
                ready,
                candidateCount: candidates.length,
                x,
                y,
                tag: target.tag,
                className: target.className,
                blocker: ready || !hit
                    ? ''
                    : `${hit.tagName || ''}.${String(hit.className || '')}`.slice(0, 160),
            };
        }
        """,
                system_order_no,
            )
        )
        now = _monotonic()
        if last_probe.get("ready"):
            target = (
                round(float(last_probe["x"])),
                round(float(last_probe["y"])),
            )
            if stable_target == target:
                if now - stable_since >= _ORDER_CLICK_STABLE_MS / 1000:
                    await page.mouse.click(*target)
                    return
            else:
                stable_target = target
                stable_since = now
        else:
            stable_target = None
            stable_since = 0.0
        await page.wait_for_timeout(_ORDER_CLICK_POLL_MS)

    if last_probe.get("found"):
        blocker = str(last_probe.get("blocker") or "页面仍在加载")
        raise RuntimeError(
            f"已找到系统单号 {system_order_no}，但其蓝色链接在 "
            f"{_ORDER_CLICK_READY_TIMEOUT_MS // 1000} 秒内始终不可点击"
            f"（可能被领星加载层遮挡：{blocker}）。"
        )
    raise RuntimeError(
        f"没有找到可点击的系统单号：{system_order_no}。"
        "订单列表可能仍在刷新，或领星页面结构已经变化。"
    )

async def wait_for_detail(page, expected_system_order_no: str | None = None, timeout_ms: int = 22000) -> None:
    """等待订单详情弹窗加载出可读取内容。"""
    try:
        await page.wait_for_function(
            """
        ({ expectedSystemOrderNo }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const isLoading = (root) => {
                const masks = Array.from(document.querySelectorAll(
                    '.el-loading-mask,.vxe-loading,.ant-spin,.ant-spin-spinning,[class*="loading"],[class*="Loading"]'
                ));
                return masks.some((el) => {
                    if (!visible(el)) return false;
                    const rect = el.getBoundingClientRect();
                    const rootRect = root.getBoundingClientRect();
                    const overlapX = Math.min(rect.right, rootRect.right) - Math.max(rect.left, rootRect.left);
                    const overlapY = Math.min(rect.bottom, rootRect.bottom) - Math.max(rect.top, rootRect.top);
                    return overlapX > 80 && overlapY > 80;
                });
            };
            const roots = Array.from(document.querySelectorAll(
                '.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'
            ))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.top < 35 || rect.width < 500 || rect.height < 260) return false;
                    const text = textOf(el);
                    if (!/系统单号/.test(text)) return false;
                    if (expectedSystemOrderNo && !text.includes(expectedSystemOrderNo)) return false;
                    return /收货信息/.test(text) && /(商品信息|更多商品信息|交易信息)/.test(text);
                })
                .sort((a, b) => textOf(a).length - textOf(b).length);
            const root = roots[0] || null;
            if (!root || isLoading(root)) return false;
            const signature = `${expectedSystemOrderNo}|${textOf(root).slice(0, 1200)}`;
            const now = Date.now();
            const previous = window.__erpAutomationDetailReady;
            if (!previous || previous.signature !== signature) {
                window.__erpAutomationDetailReady = { signature, since: now };
                return false;
            }
            return now - previous.since >= 250;
        }
        """,
            arg={"expectedSystemOrderNo": expected_system_order_no or ""},
            timeout=timeout_ms,
        )
    except Exception as exc:
        if "timeout" not in str(exc).lower():
            raise
        order_text = expected_system_order_no or "目标订单"
        raise RuntimeError(
            f"已点击系统单号 {order_text}，但领星订单详情在 "
            f"{max(1, timeout_ms // 1000)} 秒内没有完成加载。"
            "请检查领星页面是否出现登录验证、接口异常或持续加载遮罩。"
        ) from exc
