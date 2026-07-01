from __future__ import annotations


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
                    closeButton.el.click();
                    return true;
                }
            }
            return false;
        }
        """
    )
    if closed:
        await page.wait_for_timeout(900)

async def click_system_order(page, system_order_no: str) -> None:
    """在订单列表中点击指定系统单号进入详情。"""
    try:
        await page.get_by_text(system_order_no, exact=True).first.click(timeout=3000)
        return
    except Exception:
        pass

    clicked = await page.evaluate(
        """
        (orderNo) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const nodes = Array.from(document.querySelectorAll('a,span,td,div'))
                .filter((el) => visible(el) && (el.innerText || el.textContent || '').includes(orderNo));
            const node = nodes.find((el) => el.tagName.toLowerCase() === 'a') || nodes[0];
            if (!node) return false;
            node.click();
            return true;
        }
        """,
        system_order_no,
    )
    if not clicked:
        raise RuntimeError(f"没有找到可点击的系统单号：{system_order_no}")

async def wait_for_detail(page, expected_system_order_no: str | None = None, timeout_ms: int = 22000) -> None:
    """等待订单详情弹窗加载出可读取内容。"""
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
            if (!root) return false;
            return !isLoading(root);
        }
        """,
        arg={"expectedSystemOrderNo": expected_system_order_no or ""},
        timeout=timeout_ms,
    )
    await page.wait_for_timeout(1200)
