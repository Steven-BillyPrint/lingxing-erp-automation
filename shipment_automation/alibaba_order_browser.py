"""Playwright adapter for filling (but never submitting) an Alibaba draft."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from .alibaba_session import is_alibaba_login_page, try_alibaba_auto_login
from .config import AlibabaLoginConfig
from .alibaba_ordering import (
    AlibabaOrderRuleError,
    AlibabaRoute,
    ProductDeclaration,
    ShippingAddress,
    signature_required,
)


ALIBABA_QUOTE_URL = "https://i.alibaba.com/logistics/web/shipping/query"
ALIBABA_DRAFT_HOST = "scm.alibaba.com"
ALIBABA_DRAFT_PATH = "/web/express/order.htm"
ALIBABA_CDP_CONNECT_ATTEMPTS = 3
ALIBABA_CDP_CONNECT_TIMEOUT_MS = 4_000
ALIBABA_CDP_CONNECT_RETRY_DELAY_SEC = 0.35
ROUTE_NAME_SELECTOR = (
    ".solution-line-container .logistics-brand-tag-title-content"
)
SIGNATURE_LABEL_PATTERN = re.compile(r"快递签收服务")
DEFAULT_MID_CODE = "CNSHSZWH4801SHA"
MID_INPUT_ID_PATTERN = re.compile(r"formData_clearanceInfoExtList_\d+_value")
ANT_SELECT_ROOT_XPATH = (
    "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),"
    "' ant-select ')][1]"
)


def is_alibaba_draft_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == ALIBABA_DRAFT_HOST
        and parsed.path == ALIBABA_DRAFT_PATH
    )


def is_alibaba_quote_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "i.alibaba.com"
        and parsed.path.rstrip("/") == "/logistics/web/shipping/query"
    )


def choose_new_draft_url(
    current_urls: tuple[str, ...],
    baseline_urls: tuple[str, ...],
) -> str:
    candidates = tuple(
        dict.fromkeys(
            value
            for value in current_urls
            if is_alibaba_draft_url(value) and value not in set(baseline_urls)
        )
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AlibabaOrderRuleError(
            "没有找到本次查价后新打开的阿里下单草稿。请在线路右侧点击“普通下单”，"
            "等待草稿页完全打开后再重试。"
        )
    raise AlibabaOrderRuleError(
        "本次查价后打开了多个阿里下单草稿，无法安全判断目标页面。"
        "请关闭多余草稿，只保留本单页面后重新开始。"
    )


@dataclass(frozen=True)
class AlibabaDraftFacts:
    url: str
    route: AlibabaRoute
    total_weight_kg: Decimal
    signature_available: bool
    mid_input_selector: str = ""


@dataclass(frozen=True)
class AlibabaDraftFillResult:
    url: str
    route_name: str
    total_weight_kg: Decimal
    declared_unit_price_usd: Decimal
    signature_selected: bool
    signature_fee_text: str


@asynccontextmanager
async def attached_alibaba_context(
    browser_endpoint: str,
) -> AsyncIterator[Any]:
    """Attach to the submitting desktop's visible Chrome without closing it."""

    endpoint = str(browser_endpoint or "").strip()
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise AlibabaOrderRuleError("本机 Chrome 通道地址无效。")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("缺少 Playwright，无法连接阿里下单页面。") from exc

    playwright = await async_playwright().start()
    browser = None
    last_error: Exception | None = None
    for attempt in range(1, ALIBABA_CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.connect_over_cdp(
                endpoint,
                timeout=ALIBABA_CDP_CONNECT_TIMEOUT_MS,
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt == ALIBABA_CDP_CONNECT_ATTEMPTS:
                break
            # LocalChromeHost can expose /json/version a fraction of a second
            # before Playwright's CDP websocket accepts its first connection.
            # Keep the retry bounded so a real local-browser failure still
            # reaches the operator promptly and never reruns page mutations.
            await asyncio.sleep(ALIBABA_CDP_CONNECT_RETRY_DELAY_SEC)
    if browser is None:
        await playwright.stop()
        raise AlibabaOrderRuleError(
            f"无法连接提交电脑上的可见 Chrome（已自动重试 "
            f"{ALIBABA_CDP_CONNECT_ATTEMPTS} 次）。"
            "请保持阿里页面和桌面程序开启后重试。"
        ) from last_error
    if not browser.contexts:
        await playwright.stop()
        raise AlibabaOrderRuleError("本机 Chrome 没有可用浏览器上下文。")
    try:
        # Exceptions raised by page operations belong to those operations and
        # must not be mislabeled as a Chrome connection failure.
        yield browser.contexts[0]
    finally:
        await playwright.stop()


class AlibabaOrderBrowser:
    """Stable semantic operations for the researched Alibaba draft page."""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def draft_urls(self) -> tuple[str, ...]:
        return tuple(
            page.url
            for page in self.context.pages
            if is_alibaba_draft_url(page.url)
        )

    async def open_quote_page(
        self,
        *,
        login_config: AlibabaLoginConfig | None = None,
    ) -> None:
        page = await self.prepare_quote_page(login_config=login_config)
        await page.bring_to_front()

    async def prepare_quote_page(
        self,
        *,
        login_config: AlibabaLoginConfig | None = None,
    ) -> Any:
        page = next(
            (item for item in self.context.pages if is_alibaba_quote_url(item.url)),
            None,
        )
        if page is None:
            page = await self.context.new_page()
            try:
                await page.goto(ALIBABA_QUOTE_URL, wait_until="domcontentloaded")
            except Exception as exc:
                raise AlibabaOrderRuleError(
                    "阿里查价页打开失败，请检查网络后重试。"
                ) from exc
        await self._ensure_quote_login(page, login_config)
        if not is_alibaba_quote_url(page.url):
            raise AlibabaOrderRuleError(
                "未进入阿里查价页，可能需要先在本机 Chrome 完成阿里国际站登录或验证。"
            )
        return page

    @staticmethod
    async def _ensure_quote_login(
        page: Any,
        login_config: AlibabaLoginConfig | None,
    ) -> None:
        """Log in only when Alibaba redirected the quote page to its login UI."""

        await AlibabaOrderBrowser.ensure_logged_in(
            page,
            login_config,
            return_url=ALIBABA_QUOTE_URL,
            page_label="阿里查价页",
        )

    @staticmethod
    async def ensure_logged_in(
        page: Any,
        login_config: AlibabaLoginConfig | None,
        *,
        return_url: str,
        page_label: str = "阿里物流页面",
    ) -> None:
        """Detect an expired Alibaba session, fill credentials and return safely."""

        if not await is_alibaba_login_page(page):
            return

        config = login_config or AlibabaLoginConfig()
        if not config.auto_login:
            raise AlibabaOrderRuleError(
                f"{page_label}需要登录，但设置中已关闭阿里网页自动登录。"
                "请开启自动登录或在当前 Chrome 手动登录后重试。"
            )
        if not config.has_credentials:
            raise AlibabaOrderRuleError(
                f"{page_label}需要登录，但设置中没有完整填写阿里国际站账号和密码。"
            )
        try:
            submitted = await try_alibaba_auto_login(page, config)
        except Exception as exc:
            raise AlibabaOrderRuleError(
                f"{page_label}自动登录失败，请检查账号、密码或页面验证要求。"
            ) from exc
        if not submitted:
            raise AlibabaOrderRuleError(
                f"{page_label}需要登录，但未能识别当前登录表单。"
                "请在当前 Chrome 手动登录后重试。"
            )

        # Alibaba may complete sign-in without a full navigation event. Poll the
        # visible login state, then explicitly return to the exact quote page.
        for _ in range(20):
            await page.wait_for_timeout(500)
            if not await is_alibaba_login_page(page):
                break
        if await is_alibaba_login_page(page):
            raise AlibabaOrderRuleError(
                "阿里自动登录后仍停留在登录页，可能需要验证码、安全验证，"
                "或账号密码已失效。请在当前 Chrome 完成验证后重试。"
            )
        if str(page.url or "") != str(return_url or ""):
            try:
                await page.goto(return_url, wait_until="domcontentloaded")
            except Exception as exc:
                raise AlibabaOrderRuleError(
                    f"阿里登录完成，但返回{page_label}失败，请检查网络后重试。"
                ) from exc
        if await is_alibaba_login_page(page):
            raise AlibabaOrderRuleError(
                "阿里登录状态没有生效，请在当前 Chrome 完成登录或安全验证后重试。"
            )

    async def page_for_url(self, target_url: str) -> Any:
        pages = [page for page in self.context.pages if page.url == target_url]
        if len(pages) != 1:
            raise AlibabaOrderRuleError(
                "阿里草稿页面已关闭或出现重复页面，请重新从查价页打开本单草稿。"
            )
        page = pages[0]
        await page.bring_to_front()
        await page.wait_for_load_state("domcontentloaded")
        return page

    async def inspect_draft(self, page: Any) -> AlibabaDraftFacts:
        product_dialog = page.get_by_role("dialog", name="选择商品", exact=True)
        if (
            await product_dialog.count() == 1
            and await product_dialog.is_visible()
        ):
            raise AlibabaOrderRuleError(
                "阿里页面仍打开“选择商品”窗口。请先关闭窗口并保留当前商品行，"
                "再重新填写草稿。"
            )
        try:
            await page.locator(
                'input[id^="formData_package_"][id$="_weight"]'
            ).first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "阿里下单草稿未在 15 秒内完整加载，请确认登录状态和页面内容后重试。"
            ) from exc
        route_locator = page.locator(ROUTE_NAME_SELECTOR)
        if await route_locator.count() != 1:
            raise AlibabaOrderRuleError(
                "无法稳定读取当前物流方案名称，请确认页面已完整打开且只显示一个当前方案。"
            )
        route_name = (await route_locator.inner_text()).strip()
        if not route_name:
            raise AlibabaOrderRuleError("当前物流方案名称为空，请重新选择线路。")
        weight = await self._total_weight(page)
        signature_locator = page.get_by_role(
            "checkbox",
            name=SIGNATURE_LABEL_PATTERN,
        )
        mid_input_selector = await self._visible_mid_input_selector(page)
        return AlibabaDraftFacts(
            url=page.url,
            route=AlibabaRoute(route_name),
            total_weight_kg=weight,
            signature_available=await signature_locator.count() == 1,
            mid_input_selector=mid_input_selector,
        )

    @staticmethod
    async def _visible_mid_input_selector(page: Any) -> str:
        """Locate the conditional MID field through its real label/for DOM link."""

        identifiers = await page.evaluate(
            r"""
            () => Array.from(document.querySelectorAll("label[for]"))
                .filter(label => (
                    label.getAttribute("title") === "MID代码"
                    || String(label.textContent || "")
                        .replace(/\s+/g, "").startsWith("MID代码")
                ))
                .map(label => document.getElementById(label.htmlFor))
                .filter(element => {
                    if (!(element instanceof HTMLInputElement)) return false;
                    const style = window.getComputedStyle(element);
                    return element.getClientRects().length > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                })
                .map(element => element.id)
            """
        )
        if not isinstance(identifiers, list):
            raise AlibabaOrderRuleError("阿里 MID 代码字段检测结果无效。")
        if len(identifiers) > 1:
            raise AlibabaOrderRuleError("阿里页面出现多个 MID 代码字段，无法安全填写。")
        if not identifiers:
            return ""
        identifier = str(identifiers[0] or "").strip()
        if MID_INPUT_ID_PATTERN.fullmatch(identifier) is None:
            raise AlibabaOrderRuleError("阿里 MID 代码字段结构已变化，请人工处理。")
        return f'[id="{identifier}"]'

    async def _total_weight(self, page: Any) -> Decimal:
        rows = await page.evaluate(
            r"""
            () => Array.from(
                document.querySelectorAll(
                    'input[id^="formData_package_"][id$="_weight"]'
                )
            ).map(weight => {
                const match = /^formData_package_(\d+)_weight$/.exec(weight.id);
                const quantity = match
                    ? document.querySelector(
                        `#formData_package_${match[1]}_quantity`
                    )
                    : null;
                return {
                    id: weight.id,
                    weight: weight.value,
                    quantityCount: quantity ? 1 : 0,
                    quantity: quantity ? quantity.value : "1",
                };
            })
            """
        )
        total = Decimal("0")
        for row in rows:
            identifier = str(row.get("id") or "")
            match = re.fullmatch(r"formData_package_(\d+)_weight", identifier)
            if match is None:
                raise AlibabaOrderRuleError("阿里包裹重量字段结构已变化，请人工处理。")
            try:
                weight = Decimal(str(row.get("weight") or ""))
                quantity = (
                    Decimal(str(row.get("quantity") or ""))
                    if int(row.get("quantityCount") or 0) == 1
                    else Decimal("1")
                )
            except (InvalidOperation, ValueError) as exc:
                raise AlibabaOrderRuleError("阿里包裹重量或件数不是有效数字。") from exc
            if weight <= 0 or quantity <= 0:
                raise AlibabaOrderRuleError("阿里包裹重量和件数必须大于零。")
            total += weight * quantity
        if total <= 0:
            raise AlibabaOrderRuleError("阿里页面没有有效包裹重量。")
        return total

    async def fill_draft(
        self,
        page: Any,
        *,
        customer_order_no: str,
        address: ShippingAddress,
        declaration: ProductDeclaration,
        expedited: bool,
        signature_requested: bool,
        facts: AlibabaDraftFacts | None = None,
    ) -> AlibabaDraftFillResult:
        facts = facts or await self.inspect_draft(page)
        need_signature = signature_required(
            expedited=expedited,
            requested=signature_requested,
        )
        if need_signature and not facts.signature_available:
            raise AlibabaOrderRuleError(
                "本单已勾选需要签收服务，但当前线路没有显示“快递签收服务”选项。"
                "请更换支持签收的线路。"
            )

        product_rows = page.locator(
            'input[id^="formData_product_"][id$="_nameCn"]'
        )
        if await product_rows.count() != 1:
            raise AlibabaOrderRuleError(
                "当前草稿不是单一商品行，自动填写无法证明申报完整性，请人工处理。"
            )

        customer_order = page.get_by_role("textbox", name="客户订单号")
        if await customer_order.count() != 1:
            raise AlibabaOrderRuleError("无法唯一定位阿里草稿的客户订单号字段。")
        expected_customer_order_no = str(customer_order_no or "").strip()
        if not expected_customer_order_no:
            raise AlibabaOrderRuleError("客户订单号不能为空。")
        await customer_order.fill(expected_customer_order_no)
        if (await customer_order.input_value()).strip() != expected_customer_order_no:
            raise AlibabaOrderRuleError("客户订单号填写后回读不一致，已停止。")

        # The address modal and the plain declaration inputs are independent
        # React subtrees.  Fill them concurrently, then make one cheap
        # idempotent pass over the declaration inputs after the modal save in
        # case Alibaba replaced a controlled input during its address render.
        address_task = asyncio.create_task(
            self._fill_receiver_address(page, address),
            name="alibaba-draft-receiver-address",
        )
        product_inputs_task = asyncio.create_task(
            self._fill_product_inputs(
                page,
                declaration,
                mid_input_selector=facts.mid_input_selector,
            ),
            name="alibaba-draft-product-inputs",
        )
        parallel_tasks = (address_task, product_inputs_task)
        try:
            _, product_inputs_marker = await asyncio.gather(*parallel_tasks)
        finally:
            for task in parallel_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*parallel_tasks, return_exceptions=True)
        if await self._product_inputs_need_refill(
            page,
            declaration,
            product_inputs_marker,
            mid_input_selector=facts.mid_input_selector,
        ):
            product_inputs_marker = await self._fill_product_inputs(
                page,
                declaration,
                mid_input_selector=facts.mid_input_selector,
            )
        await self._fill_product_selectors(page, declaration)

        signature_selected = False
        signature_fee_text = ""
        signature = page.get_by_role("checkbox", name=SIGNATURE_LABEL_PATTERN)
        if await signature.count() == 1:
            if need_signature:
                await signature.check()
            else:
                await signature.uncheck()
            signature_selected = await signature.is_checked()
            signature_fee_text = (
                await signature.locator("xpath=ancestor::label[1]").inner_text()
                if await signature.locator("xpath=ancestor::label[1]").count() == 1
                else ""
            ).strip()
        if signature_selected != need_signature:
            raise AlibabaOrderRuleError("签收服务勾选后的回读状态不一致，已停止。")

        # Repeat scalar entry only when React replaced the product controls or
        # a committed value no longer matches.  Normal drafts now avoid the two
        # unconditional refill passes while retaining the rerender safeguard.
        if await self._product_inputs_need_refill(
            page,
            declaration,
            product_inputs_marker,
            mid_input_selector=facts.mid_input_selector,
        ):
            await self._fill_product_inputs(
                page,
                declaration,
                mid_input_selector=facts.mid_input_selector,
            )
        await self._verify_product(
            page,
            declaration,
            mid_input_selector=facts.mid_input_selector,
        )
        # Safety boundary: this adapter never locates or clicks the final order
        # submission button.  The operator reviews the visible draft and submits.
        return AlibabaDraftFillResult(
            url=page.url,
            route_name=facts.route.name,
            total_weight_kg=facts.total_weight_kg,
            declared_unit_price_usd=declaration.declared_unit_price_usd,
            signature_selected=signature_selected,
            signature_fee_text=signature_fee_text,
        )

    @staticmethod
    async def _fill_input_values(
        page: Any,
        values: dict[str, str],
        *,
        field_group: str,
    ) -> None:
        """Commit independent fields quickly without bypassing Ant/React state.

        Alibaba listens to the browser's editing pipeline rather than merely
        reading ``input.value``.  Assigning values and dispatching synthetic
        events can therefore look correct for one render and then be reverted
        by React.  ``execCommand('insertText')`` goes through the same native
        editing path as text insertion.  A ``MessageChannel`` yields to a new
        browser task between controls so each controlled-field update commits
        before the next one.  Unlike animation-frame waits, this remains fast
        when Chrome is behind the desktop window, and the whole batch still
        costs only one Playwright round trip.
        """

        entries = [
            {"selector": selector, "value": str(value)}
            for selector, value in values.items()
        ]
        results = await page.evaluate(
            r"""
            async entries => {
                const inspect = entry => {
                    const nodes = document.querySelectorAll(entry.selector);
                    if (nodes.length !== 1) {
                        return {count: nodes.length, value: ""};
                    }
                    const element = nodes[0];
                    const supported = element instanceof HTMLInputElement
                        || element instanceof HTMLTextAreaElement;
                    const style = supported
                        ? window.getComputedStyle(element)
                        : null;
                    const visible = supported
                        && element.getClientRects().length > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                    return {
                        count: 1,
                        value: supported ? element.value : null,
                        editable: Boolean(
                            supported
                            && visible
                            && !element.disabled
                            && !element.readOnly
                        ),
                        numeric: Boolean(
                            supported
                            && (
                                element.type === "number"
                                || element.getAttribute("role") === "spinbutton"
                            )
                        ),
                    };
                };
                const before = entries.map(inspect);
                if (before.some(result => (
                    result.count !== 1 || result.editable !== true
                ))) {
                    return before;
                }

                const waitForCommit = () => new Promise(resolve => {
                    const channel = new MessageChannel();
                    channel.port1.onmessage = () => {
                        channel.port1.close();
                        channel.port2.close();
                        resolve();
                    };
                    channel.port2.postMessage(null);
                });
                const results = [];
                for (const entry of entries) {
                    // React may replace a controlled input after any commit.
                    // Always resolve the current node instead of keeping an
                    // element captured before the preceding render.
                    const element = document.querySelector(entry.selector);
                    const current = inspect(entry);
                    const unchanged = current.numeric === true
                        ? Number(current.value) === Number(entry.value)
                        : current.value === entry.value;
                    if (unchanged) {
                        results.push(current);
                        continue;
                    }
                    element.focus({preventScroll: true});
                    element.select();
                    if (entry.value === "") {
                        document.execCommand("delete", false);
                    } else {
                        document.execCommand("insertText", false, entry.value);
                    }
                    element.blur();
                    await waitForCommit();
                    results.push(inspect(entry));
                }
                return results;
            }
            """,
            entries,
        )
        if not isinstance(results, list) or len(results) != len(entries):
            raise AlibabaOrderRuleError(f"阿里{field_group}字段批量填写返回无效结果。")
        for entry, result in zip(entries, results, strict=True):
            if not isinstance(result, dict) or int(result.get("count") or 0) != 1:
                raise AlibabaOrderRuleError(
                    f"阿里{field_group}字段已变化：{entry['selector']}"
                )
            if result.get("editable") is not True:
                raise AlibabaOrderRuleError(
                    f"阿里{field_group}字段当前不可见或不可编辑：{entry['selector']}"
                )
            actual = str(result.get("value") or "")
            matches = actual == entry["value"]
            if result.get("numeric") is True:
                try:
                    matches = Decimal(actual) == Decimal(entry["value"])
                except InvalidOperation:
                    matches = False
            if not matches:
                raise AlibabaOrderRuleError(
                    f"阿里{field_group}字段填写后回读不一致：{entry['selector']}"
                )

    @staticmethod
    async def _read_input_values(
        page: Any,
        selectors: tuple[str, ...],
        *,
        field_group: str,
    ) -> dict[str, str]:
        rows = await page.evaluate(
            """
            selectors => selectors.map(selector => {
                const nodes = document.querySelectorAll(selector);
                return {
                    selector,
                    count: nodes.length,
                    value: nodes.length === 1 ? nodes[0].value : "",
                };
            })
            """,
            list(selectors),
        )
        values: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict) or int(row.get("count") or 0) != 1:
                selector = str(row.get("selector") or "") if isinstance(row, dict) else ""
                raise AlibabaOrderRuleError(
                    f"阿里{field_group}字段已变化：{selector or 'unknown'}"
                )
            values[str(row["selector"])] = str(row.get("value") or "")
        if len(values) != len(selectors):
            raise AlibabaOrderRuleError(f"阿里{field_group}字段批量回读不完整。")
        return values

    async def _fill_receiver_address(
        self,
        page: Any,
        address: ShippingAddress,
    ) -> None:
        edit_buttons = await self._receiver_edit_buttons(page)
        await edit_buttons.nth(1).click()
        dialog = await self._receiver_address_dialog(page)
        await dialog.wait_for(state="visible")

        country_control = page.locator("#address_country")
        if await country_control.count() != 1:
            raise AlibabaOrderRuleError(
                "阿里地址的目的国字段已变化；已保留修改地址弹窗。"
            )
        acceptable_country_names = {
            "US": ("united states", "美国"),
            "CA": ("canada", "加拿大"),
        }.get(address.country_code, (address.country_name.casefold(),))
        country_code = address.country_code.strip().casefold()
        # The modal can become visible before React hydrates the disabled
        # country Select.  A MutationObserver continues as soon as the value is
        # rendered instead of paying for twenty browser round trips.
        country_values = await self._wait_for_ant_values(
            country_control,
            (country_code,),
            timeout_ms=2000,
            contains=acceptable_country_names,
        )
        country_matches = any(
            value == country_code
            or any(
                name.casefold() in value
                for name in acceptable_country_names
                if str(name or "").strip()
            )
            for value in country_values
        )
        if not country_matches:
            displayed_country = " / ".join(country_values) or "未读取到"
            raise AlibabaOrderRuleError(
                "阿里草稿目的国与领星订单不一致或无法读取："
                f"页面为“{displayed_country}”，订单为“{address.country_code}”。"
                "地址尚未填写，修改地址弹窗已保留。"
            )

        await self._select_ant_option(
            page,
            "#address_province",
            address.province,
            "州/省",
        )
        await page.locator("#address_city").wait_for(state="visible")
        await self._select_ant_option(
            page,
            "#address_city",
            address.city,
            "城市",
        )
        # Alibaba accepts a free-form street address.  Suggestions are only
        # optional normalization hints and can be ambiguous or even point to
        # another city with the same street number.  Preserve the verified ERP
        # address verbatim and prove it survives the subsequent form save.
        await self._fill_input_values(
            page,
            {
                "#companyNameEn": address.company,
                "#address_address": address.address1,
                "#address_address2": address.address2,
                "#address_zip": address.postal_code,
                "#contactPerson": address.recipient,
                "#contact_phoneCode": address.dial_code,
                "#contact_mobileNo": address.phone,
                "#contact_email": address.email,
            },
            field_group="地址",
        )
        await self._verify_address_dialog_fields(page, address)
        confirm_button = await self._address_dialog_action(dialog, "确定", 1)
        await self._wait_for_address_save_ready(confirm_button)
        await confirm_button.click()
        await dialog.wait_for(state="hidden", timeout=10000)

        validation = page.get_by_text("收货人信息校验不通过", exact=False)
        if await validation.count() and await validation.first.is_visible():
            raise AlibabaOrderRuleError(
                "阿里页面保存地址后仍提示校验不通过，请在当前草稿中人工检查地址。"
            )
        # Do not reopen the modal after a successful save.  Reopening caused a
        # visible close/open bounce and could race Alibaba's form reset.  The
        # receiver card is the authoritative post-save rendering, so wait for
        # its address summary to contain the verified ERP values instead.
        refreshed_edit_buttons = await self._receiver_edit_buttons(page)
        await self._wait_for_saved_receiver_summary(
            refreshed_edit_buttons.nth(1),
            address,
        )

    @staticmethod
    async def _wait_for_address_save_ready(confirm_button: Any) -> None:
        """Wait until Ant has committed validation and the save button is idle."""

        await confirm_button.evaluate(
            r"""
            button => new Promise(resolve => {
                const visible = element => {
                    if (!(element instanceof Element)) return false;
                    const style = window.getComputedStyle(element);
                    return element.getClientRects().length > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                };
                const dialog = button.closest('[role="dialog"], .ant-modal')
                    || button.parentElement;
                const ready = () => {
                    const disabled = button.disabled
                        || button.getAttribute("aria-disabled") === "true"
                        || button.classList.contains("ant-btn-loading");
                    const loading = dialog && Array.from(dialog.querySelectorAll(
                        ".ant-btn-loading-icon, .ant-spin-spinning, .anticon-loading"
                    )).some(visible);
                    return !disabled && !loading;
                };
                if (ready()) {
                    resolve();
                    return;
                }
                const observer = new MutationObserver(() => {
                    if (!ready()) return;
                    observer.disconnect();
                    resolve();
                });
                observer.observe(dialog || button, {
                    attributes: true,
                    childList: true,
                    subtree: true,
                });
            })
            """
        )

    @staticmethod
    async def _wait_for_saved_receiver_summary(
        receiver_edit_button: Any,
        address: ShippingAddress,
        *,
        timeout_ms: int = 5000,
    ) -> None:
        tokens = tuple(
            token
            for token in (
                address.address1,
                address.city,
                address.postal_code,
            )
            if str(token or "").strip()
        )
        if len(tokens) < 3:
            raise AlibabaOrderRuleError(
                "领星订单缺少地址保存回读所需的街道、城市或邮编。"
            )
        saved = await receiver_edit_button.evaluate(
            r"""
            (button, payload) => new Promise(resolve => {
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ")
                    .trim()
                    .toLowerCase();
                const tokens = payload.tokens.map(normalize);
                const visible = element => {
                    const style = window.getComputedStyle(element);
                    return element.getClientRects().length > 0
                        && style.display !== "none"
                        && style.visibility !== "hidden";
                };
                const matches = () => {
                    let current = button;
                    for (let depth = 0; current && depth < 9; depth += 1) {
                        if (
                            current.getAttribute
                            && current.getAttribute("role") === "dialog"
                        ) return false;
                        if (visible(current)) {
                            const text = normalize(current.innerText);
                            if (tokens.every(token => text.includes(token))) {
                                return true;
                            }
                        }
                        current = current.parentElement;
                    }
                    return false;
                };
                if (matches()) {
                    resolve(true);
                    return;
                }
                let finished = false;
                const finish = value => {
                    if (finished) return;
                    finished = true;
                    observer.disconnect();
                    clearTimeout(timer);
                    resolve(value);
                };
                const observer = new MutationObserver(() => {
                    if (matches()) finish(true);
                });
                observer.observe(document.body, {
                    subtree: true,
                    childList: true,
                    characterData: true,
                });
                const timer = setTimeout(
                    () => finish(matches()),
                    payload.timeoutMs,
                );
            })
            """,
            {
                "tokens": list(tokens),
                "timeoutMs": max(1, int(timeout_ms)),
            },
        )
        if saved is not True:
            raise AlibabaOrderRuleError(
                "阿里地址弹窗已保存关闭，但收货地址卡片未在 5 秒内完成回读更新。"
            )

    @staticmethod
    async def _receiver_edit_buttons(page: Any) -> Any:
        named = page.get_by_role("button", name="编辑", exact=True)
        if await named.count() == 2:
            return named
        # Alibaba currently font-maps this label in the DOM on some sessions;
        # the two address edit buttons retain this stable structural class.
        structural = page.locator("button.icon-margin-right:visible")
        if await structural.count() == 2:
            return structural
        raise AlibabaOrderRuleError("无法定位收货地址的“编辑”按钮。")

    @staticmethod
    async def _receiver_address_dialog(page: Any) -> Any:
        named = page.get_by_role("dialog", name="修改地址")
        if await named.count() == 1:
            return named
        structural = page.locator(".ant-modal.custom-address-dialog")
        try:
            await structural.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise AlibabaOrderRuleError("无法定位阿里“修改地址”弹窗。") from exc
        if await structural.count() != 1:
            raise AlibabaOrderRuleError("阿里“修改地址”弹窗数量异常。")
        return structural

    @staticmethod
    async def _address_dialog_action(dialog: Any, name: str, index: int) -> Any:
        named = dialog.get_by_role("button", name=name, exact=True)
        if await named.count() == 1:
            return named
        footer_buttons = dialog.locator(".ant-modal-footer button:visible")
        if await footer_buttons.count() != 2:
            raise AlibabaOrderRuleError(f"无法定位修改地址弹窗的“{name}”按钮。")
        return footer_buttons.nth(index)

    @staticmethod
    async def _ant_selected_values(control: Any) -> tuple[str, ...]:
        """Read an Ant Select from both its input value and rendered label.

        Alibaba can expose the receiver country through either the readonly
        input or a separately rendered selection item.  The surrounding element
        may briefly have an empty ``inner_text`` while the modal is hydrating.
        """

        raw_values = await control.evaluate(
            r"""
            element => {
                const wrapper = element.closest(".ant-select");
                const values = [element.value || ""];
                if (wrapper) {
                    wrapper.querySelectorAll(".ant-select-selection-item")
                        .forEach(item => {
                            values.push(item.getAttribute("title") || "");
                            values.push(item.textContent || "");
                        });
                    values.push(wrapper.textContent || "");
                }
                return values;
            }
            """
        )
        return tuple(
            dict.fromkeys(
                normalized
                for value in (
                    raw_values if isinstance(raw_values, list) else ()
                )
                if (
                    normalized := re.sub(
                        r"\s+",
                        " ",
                        str(value or "").strip(),
                    ).casefold()
                )
            )
        )

    @classmethod
    async def _wait_for_ant_values(
        cls,
        control: Any,
        accepted: tuple[str, ...],
        *,
        timeout_ms: int = 2000,
        contains: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        expected = tuple(
            cls._normalized_select_text(value)
            for value in accepted
            if str(value or "").strip()
        )
        contained = tuple(
            cls._normalized_select_text(value)
            for value in contains
            if str(value or "").strip()
        )
        raw_values = await control.evaluate(
            r"""
            (element, payload) => new Promise(resolve => {
                const wrapper = element.closest(".ant-select") || element;
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ")
                    .trim()
                    .toLowerCase();
                const read = () => {
                    const values = [element.value || ""];
                    wrapper.querySelectorAll(".ant-select-selection-item")
                        .forEach(item => {
                            values.push(item.getAttribute("title") || "");
                            values.push(item.textContent || "");
                        });
                    values.push(wrapper.textContent || "");
                    return Array.from(new Set(values.map(normalize).filter(Boolean)));
                };
                const matches = values => {
                    if (String(element.getAttribute("aria-invalid") || "")
                        .toLowerCase() === "true") {
                        return false;
                    }
                    return payload.accepted.some(expected =>
                        values.some(value => value === expected)
                    ) || payload.contained.some(expected =>
                        values.some(value => value.includes(expected))
                    );
                };
                const immediate = read();
                if (matches(immediate)) {
                    resolve(immediate);
                    return;
                }
                let finished = false;
                let timer = null;
                const observer = new MutationObserver(() => finishIfReady());
                const cleanup = () => {
                    observer.disconnect();
                    element.removeEventListener("input", finishIfReady);
                    element.removeEventListener("change", finishIfReady);
                    if (timer !== null) clearTimeout(timer);
                };
                const finish = values => {
                    if (finished) return;
                    finished = true;
                    cleanup();
                    resolve(values);
                };
                const finishIfReady = () => {
                    const values = read();
                    if (matches(values)) finish(values);
                };
                observer.observe(wrapper, {
                    subtree: true,
                    childList: true,
                    characterData: true,
                    attributes: true,
                    attributeFilter: ["value", "title", "aria-invalid"],
                });
                element.addEventListener("input", finishIfReady);
                element.addEventListener("change", finishIfReady);
                timer = setTimeout(() => finish(read()), payload.timeoutMs);
            })
            """,
            {
                "accepted": list(expected),
                "contained": list(contained),
                "timeoutMs": max(1, int(timeout_ms)),
            },
        )
        return tuple(
            dict.fromkeys(
                cls._normalized_select_text(value)
                for value in (
                    raw_values if isinstance(raw_values, list) else ()
                )
                if str(value or "").strip()
            )
        )

    @staticmethod
    async def _ant_option_records(options: Any) -> tuple[tuple[str, str], ...]:
        records = await options.evaluate_all(
            """
            elements => elements.map(element => ({
                title: element.getAttribute("title") || "",
                text: element.innerText || element.textContent || "",
            }))
            """
        )
        if not isinstance(records, list):
            return ()
        return tuple(
            (
                str(record.get("title") or ""),
                str(record.get("text") or ""),
            )
            for record in records
            if isinstance(record, dict)
        )

    @staticmethod
    async def _wait_for_option_commit(option: Any, timeout_ms: int = 1500) -> bool:
        return bool(
            await option.evaluate(
                """
                (element, timeoutMs) => new Promise(resolve => {
                    const visible = () => {
                        if (!element.isConnected) return false;
                        const style = window.getComputedStyle(element);
                        return element.getClientRects().length > 0
                            && style.display !== "none"
                            && style.visibility !== "hidden";
                    };
                    const committed = () => (
                        !visible()
                        || element.getAttribute("aria-selected") === "true"
                        || element.classList.contains(
                            "ant-select-item-option-selected"
                        )
                    );
                    if (committed()) {
                        resolve(true);
                        return;
                    }
                    let finished = false;
                    const finish = result => {
                        if (finished) return;
                        finished = true;
                        observer.disconnect();
                        clearTimeout(timer);
                        resolve(result);
                    };
                    const observer = new MutationObserver(() => {
                        if (committed()) finish(true);
                    });
                    observer.observe(document.documentElement, {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: [
                            "aria-selected",
                            "aria-hidden",
                            "class",
                            "style",
                        ],
                    });
                    const timer = setTimeout(
                        () => finish(committed()),
                        timeoutMs
                    );
                })
                """,
                max(1, int(timeout_ms)),
            )
        )

    async def _verify_address_dialog_fields(
        self,
        page: Any,
        address: ShippingAddress,
    ) -> None:
        expected = {
            "#companyNameEn": address.company,
            "#address_address": address.address1,
            "#address_address2": address.address2,
            "#address_zip": address.postal_code,
            "#contactPerson": address.recipient,
            "#contact_phoneCode": address.dial_code,
            "#contact_mobileNo": address.phone,
            "#contact_email": address.email,
        }
        actual_values = await self._read_input_values(
            page,
            tuple(expected),
            field_group="地址",
        )
        for selector, wanted in expected.items():
            normalized_wanted = re.sub(r"\s+", " ", str(wanted).strip())
            actual = re.sub(r"\s+", " ", actual_values[selector].strip())
            if actual != normalized_wanted:
                raise AlibabaOrderRuleError(
                    f"阿里地址字段填写后回读不一致：{selector}"
                )

        for control_selector, wanted, label in (
            ("#address_province", address.province, "州/省"),
            ("#address_city", address.city, "城市"),
        ):
            control = page.locator(control_selector)
            if await control.count() != 1:
                raise AlibabaOrderRuleError(f"阿里地址的{label}字段已变化。")
            selected_values = await self._wait_for_ant_values(
                control,
                (wanted,),
                timeout_ms=1200,
            )
            invalid = str(
                await control.get_attribute("aria-invalid") or ""
            ).casefold()
            if (
                self._normalized_select_text(wanted) not in selected_values
                or invalid == "true"
            ):
                raise AlibabaOrderRuleError(
                    f"阿里地址的{label}填写后回读不一致，已保留弹窗。"
                )

        if len(actual_values["#address_address"]) > 35:
            raise AlibabaOrderRuleError("阿里地址1保存后超过 35 个字符，已停止。")

    async def _select_ant_option(
        self,
        page: Any,
        selector: str,
        value: str,
        label: str,
    ) -> None:
        prepared = await self._prepare_ant_combobox(
            page,
            selector,
            value,
            exact=True,
        )
        if int(prepared.get("count") or 0) != 1:
            raise AlibabaOrderRuleError(f"阿里地址的{label}字段已变化。")
        if prepared.get("done") is True:
            return
        control = page.locator(selector)
        await control.press("ArrowDown")
        committed = await self._commit_ant_popup_option(
            page,
            selector,
            value,
            exact=True,
        )
        if committed.get("status") == "association_changed":
            raise AlibabaOrderRuleError(f"阿里{label}候选列表的关联标识已变化。")
        if committed.get("status") == "listbox_changed":
            raise AlibabaOrderRuleError(f"阿里{label}候选列表无法唯一定位。")
        if committed.get("status") == "options_missing":
            raise AlibabaOrderRuleError(f"阿里{label}候选列表没有显示。")
        if committed.get("status") == "ambiguous":
            raise AlibabaOrderRuleError(
                f"阿里地址的{label}候选项无法唯一精确匹配“{value}”。"
            )
        if committed.get("status") != "committed":
            raise AlibabaOrderRuleError(f"阿里地址的{label}没有从候选列表中选中。")

    @classmethod
    async def _prepare_ant_combobox(
        cls,
        page: Any,
        selector: str,
        value: str,
        *,
        exact: bool,
    ) -> dict[str, Any]:
        """Inspect and edit an Ant combobox in one page-side operation."""

        result = await page.evaluate(
            r"""
            payload => {
                const nodes = document.querySelectorAll(payload.selector);
                if (nodes.length !== 1) return {count: nodes.length};
                const element = nodes[0];
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ").trim().toLowerCase();
                const expected = normalize(payload.value);
                const codeMatches = candidate => {
                    const normalized = normalize(candidate);
                    if (normalized === expected) return true;
                    if (!/^\d{6,12}$/.test(expected)) return false;
                    const tokens = normalized.match(
                        /(?<!\d)(?:\d[\s.\-/]*){5,11}\d(?!\d)/g
                    ) || [];
                    return tokens.some(token => token.replace(/\D/g, "") === expected);
                };
                const wrapper = element.closest(".ant-select") || element;
                const selected = [element.value || ""];
                wrapper.querySelectorAll(".ant-select-selection-item")
                    .forEach(item => {
                        selected.push(item.getAttribute("title") || "");
                        selected.push(item.textContent || "");
                    });
                const selectedMatch = selected.some(candidate => (
                    payload.exact
                        ? normalize(candidate) === expected
                        : codeMatches(candidate)
                ));
                const expanded = String(
                    element.getAttribute("aria-expanded") || ""
                ).toLowerCase();
                const invalid = String(
                    element.getAttribute("aria-invalid") || ""
                ).toLowerCase();
                const role = String(element.getAttribute("role") || "");
                const needsCommit = payload.exact
                    ? !selectedMatch
                    : role === "combobox" && (
                        !selectedMatch || expanded === "true" || invalid === "true"
                    );
                if (selectedMatch && !needsCommit) {
                    return {count: 1, done: true, role, expanded};
                }
                if (!selectedMatch) {
                    element.focus({preventScroll: true});
                    if (typeof element.select === "function") element.select();
                    if (payload.value === "") {
                        document.execCommand("delete", false);
                    } else {
                        document.execCommand("insertText", false, payload.value);
                    }
                }
                return {
                    count: 1,
                    done: false,
                    role,
                    value: element.value || "",
                    expanded: String(
                        element.getAttribute("aria-expanded") || expanded
                    ).toLowerCase(),
                    listId: String(element.getAttribute("aria-controls") || ""),
                };
            }
            """,
            {
                "selector": selector,
                "value": str(value),
                "exact": bool(exact),
            },
        )
        return dict(result) if isinstance(result, dict) else {}

    @staticmethod
    async def _commit_ant_popup_option(
        page: Any,
        selector: str,
        value: str,
        *,
        exact: bool,
    ) -> dict[str, Any]:
        """Find, click and verify one Ant candidate in one page operation."""

        result = await page.evaluate(
            r"""
            payload => new Promise(resolve => {
                const controls = document.querySelectorAll(payload.selector);
                if (controls.length !== 1) {
                    resolve({status: "control_changed"});
                    return;
                }
                const control = controls[0];
                const listId = String(control.getAttribute("aria-controls") || "");
                if (!/^[A-Za-z0-9_:-]+$/.test(listId)) {
                    resolve({status: "association_changed"});
                    return;
                }
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ").trim().toLowerCase();
                const expected = normalize(payload.value);
                const searchMatches = candidate => {
                    const normalized = normalize(candidate);
                    if (normalized === expected) return true;
                    if (!/^\d{6,12}$/.test(expected)) return false;
                    const tokens = normalized.match(
                        /(?<!\d)(?:\d[\s.\-/]*){5,11}\d(?!\d)/g
                    ) || [];
                    return tokens.some(token => token.replace(/\D/g, "") === expected);
                };
                const matches = option => {
                    const title = option.getAttribute("title") || "";
                    const text = option.textContent || "";
                    return payload.exact
                        ? [title, text].some(candidate => normalize(candidate) === expected)
                        : [title, text].some(searchMatches);
                };
                const selected = () => {
                    if (String(control.getAttribute("aria-invalid") || "")
                        .toLowerCase() === "true") return false;
                    const wrapper = control.closest(".ant-select") || control;
                    const values = [control.value || ""];
                    wrapper.querySelectorAll(".ant-select-selection-item")
                        .forEach(item => {
                            values.push(item.getAttribute("title") || "");
                            values.push(item.textContent || "");
                        });
                    return values.some(candidate => payload.exact
                        ? normalize(candidate) === expected
                        : searchMatches(candidate));
                };
                let timer = null;
                let observer = null;
                const finish = status => {
                    if (observer) observer.disconnect();
                    if (timer !== null) clearTimeout(timer);
                    resolve({status});
                };
                const commitIfReady = () => {
                    const listboxes = document.querySelectorAll(`[id="${CSS.escape(listId)}"]`);
                    if (listboxes.length !== 1) return false;
                    const options = Array.from(
                        listboxes[0].parentElement.querySelectorAll(
                            ".ant-select-item-option"
                        )
                    );
                    if (!options.length) return false;
                    const matching = options.filter(matches);
                    if (matching.length !== 1) {
                        finish("ambiguous");
                        return true;
                    }
                    matching[0].click();
                    if (selected()) {
                        finish("committed");
                        return true;
                    }
                    const commitObserver = new MutationObserver(() => {
                        if (selected()) finish("committed");
                    });
                    observer.disconnect();
                    observer = commitObserver;
                    commitObserver.observe(
                        control.closest(".ant-select") || control,
                        {subtree: true, childList: true, characterData: true, attributes: true}
                    );
                    control.addEventListener("input", () => {
                        if (selected()) finish("committed");
                    }, {once: true});
                    control.addEventListener("change", () => {
                        if (selected()) finish("committed");
                    }, {once: true});
                    return true;
                };
                const listboxes = document.querySelectorAll(`[id="${CSS.escape(listId)}"]`);
                if (listboxes.length > 1) {
                    resolve({status: "listbox_changed"});
                    return;
                }
                observer = new MutationObserver(commitIfReady);
                observer.observe(document.body, {subtree: true, childList: true});
                timer = setTimeout(() => finish("options_missing"), 3000);
                commitIfReady();
            })
            """,
            {
                "selector": selector,
                "value": str(value),
                "exact": bool(exact),
            },
        )
        return dict(result) if isinstance(result, dict) else {}

    @staticmethod
    def _normalized_select_text(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    @staticmethod
    async def _ant_popup_options(page: Any, control: Any, label: str) -> Any:
        list_id = str(await control.get_attribute("aria-controls") or "").strip()
        if not list_id or not re.fullmatch(r"[A-Za-z0-9_:-]+", list_id):
            raise AlibabaOrderRuleError(f"阿里{label}候选列表的关联标识已变化。")
        listbox = page.locator(f'[id="{list_id}"]')
        if await listbox.count() != 1:
            raise AlibabaOrderRuleError(f"阿里{label}候选列表无法唯一定位。")
        options = listbox.locator("xpath=..").locator(".ant-select-item-option")
        try:
            await options.first.wait_for(state="visible", timeout=3000)
        except Exception as exc:
            raise AlibabaOrderRuleError(f"阿里{label}候选列表没有显示。") from exc
        return options

    async def _fill_product(
        self,
        page: Any,
        declaration: ProductDeclaration,
    ) -> None:
        await self._fill_product_inputs(page, declaration)
        await self._fill_product_selectors(page, declaration)

    @staticmethod
    def _product_input_values(declaration: ProductDeclaration) -> dict[str, str]:
        return {
            "#formData_product_0_nameCn": declaration.name_cn,
            "#formData_product_0_nameEn": declaration.name_en,
            "#formData_product_0_material": declaration.material,
            "#formData_product_0_purpose": declaration.purpose,
            "#formData_product_0_quantity": str(declaration.quantity),
            "#formData_product_0_declarationValue": format(
                declaration.declared_unit_price_usd,
                "f",
            ),
        }

    @classmethod
    def _draft_input_values(
        cls,
        declaration: ProductDeclaration,
        *,
        mid_input_selector: str = "",
    ) -> dict[str, str]:
        values = cls._product_input_values(declaration)
        if mid_input_selector:
            values[mid_input_selector] = DEFAULT_MID_CODE
        return values

    async def _fill_product_inputs(
        self,
        page: Any,
        declaration: ProductDeclaration,
        *,
        mid_input_selector: str = "",
    ) -> str:
        values = self._draft_input_values(
            declaration,
            mid_input_selector=mid_input_selector,
        )
        await self._fill_input_values(
            page,
            values,
            field_group="商品",
        )
        marker = await page.evaluate(
            r"""
            selectors => {
                const first = document.querySelector(selectors[0]);
                if (!first) return "";
                const state = window.__erpAlibabaProductInputState ||=
                    {markers: new WeakMap(), sequence: 0};
                let marker = state.markers.get(first);
                if (!marker) {
                    marker = `product-row-${++state.sequence}`;
                    state.markers.set(first, marker);
                }
                return marker;
            }
            """,
            list(values),
        )
        return str(marker or "")

    async def _product_inputs_need_refill(
        self,
        page: Any,
        declaration: ProductDeclaration,
        marker: str | None,
        *,
        mid_input_selector: str = "",
    ) -> bool:
        if not marker:
            return True
        values = self._draft_input_values(
            declaration,
            mid_input_selector=mid_input_selector,
        )
        result = await page.evaluate(
            r"""
            payload => {
                const entries = Object.entries(payload.values);
                const first = document.querySelector(entries[0][0]);
                const state = window.__erpAlibabaProductInputState;
                const sameNode = Boolean(
                    first && state && state.markers.get(first) === payload.marker
                );
                const valuesMatch = entries.every(([selector, expected]) => {
                    const nodes = document.querySelectorAll(selector);
                    if (nodes.length !== 1) return false;
                    const element = nodes[0];
                    const numeric = element.type === "number"
                        || element.getAttribute("role") === "spinbutton";
                    return numeric
                        ? Number(element.value) === Number(expected)
                        : element.value === expected;
                });
                return {sameNode, valuesMatch};
            }
            """,
            {"marker": marker, "values": values},
        )
        return not (
            isinstance(result, dict)
            and result.get("sameNode") is True
            and result.get("valuesMatch") is True
        )

    async def _fill_product_selectors(
        self,
        page: Any,
        declaration: ProductDeclaration,
    ) -> None:
        await self._fill_product_search_value(
            page,
            "#formData_product_0_hscode",
            declaration.china_hs_code,
            "中国 HS 编码",
        )
        destination = page.locator("#formData_product_0_destinationHscode")
        if declaration.destination_hs_code is None:
            if await destination.count() == 1:
                await self._fill_input_values(
                    page,
                    {"#formData_product_0_destinationHscode": ""},
                    field_group="商品",
                )
        else:
            if await destination.count() != 1:
                raise AlibabaOrderRuleError("阿里页面缺少目的国 HS 编码字段。")
            await self._fill_product_search_value(
                page,
                "#formData_product_0_destinationHscode",
                declaration.destination_hs_code,
                "目的国 HS 编码",
            )

        product_type = page.locator("#formData_product_0_productType")
        if await product_type.count() != 1:
            raise AlibabaOrderRuleError("阿里页面缺少物流属性字段。")
        await self._select_product_type(
            page,
            product_type,
            declaration.logistics_attribute,
        )

    async def _select_product_type(
        self,
        page: Any,
        control: Any,
        value: str,
    ) -> None:
        prepared = await page.evaluate(
            r"""
            payload => {
                const controls = document.querySelectorAll(payload.selector);
                if (controls.length !== 1) return {controlCount: controls.length};
                const control = controls[0];
                const wrapper = control.closest(".ant-select");
                if (!wrapper) return {controlCount: 1, wrapperCount: 0};
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ").trim().toLowerCase();
                const expected = normalize(payload.value);
                const values = [control.value || ""];
                wrapper.querySelectorAll(".ant-select-selection-item")
                    .forEach(item => {
                        values.push(item.getAttribute("title") || "");
                        values.push(item.textContent || "");
                    });
                return {
                    controlCount: 1,
                    wrapperCount: 1,
                    done: values.some(item => normalize(item) === expected),
                };
            }
            """,
            {
                "selector": "#formData_product_0_productType",
                "value": str(value),
            },
        )
        prepared = dict(prepared) if isinstance(prepared, dict) else {}
        if int(prepared.get("controlCount") or 0) != 1:
            raise AlibabaOrderRuleError("阿里页面缺少物流属性字段。")
        if int(prepared.get("wrapperCount") or 0) != 1:
            raise AlibabaOrderRuleError("阿里物流属性控件结构已变化。")
        if prepared.get("done") is True:
            return
        wrapper = control.locator(ANT_SELECT_ROOT_XPATH)
        await wrapper.click()
        status = await page.evaluate(
            r"""
            payload => new Promise(resolve => {
                const normalize = value => String(value || "")
                    .replace(/\s+/g, " ").trim().toLowerCase();
                const expected = normalize(payload.value);
                const control = document.querySelector(payload.selector);
                const selected = () => {
                    const wrapper = control && control.closest(".ant-select");
                    return Boolean(wrapper && Array.from(wrapper.querySelectorAll(
                        ".ant-select-selection-item"
                    )).some(item => [
                        item.getAttribute("title") || "",
                        item.textContent || "",
                    ].some(value => normalize(value) === expected)));
                };
                let observer = null;
                let timer = null;
                const finish = status => {
                    if (observer) observer.disconnect();
                    if (timer !== null) clearTimeout(timer);
                    resolve(status);
                };
                const selectIfReady = () => {
                    const options = Array.from(document.querySelectorAll(
                        ".product-type-dropdown "
                        + ".ant-cascader-menu-item[role='menuitemcheckbox']"
                    )).filter(option => {
                        const style = window.getComputedStyle(option);
                        return option.getClientRects().length > 0
                            && style.display !== "none"
                            && style.visibility !== "hidden";
                    });
                    if (!options.length) return false;
                    const matching = options.filter(option => [
                        option.getAttribute("title") || "",
                        option.textContent || "",
                    ].some(value => normalize(value) === expected));
                    if (matching.length !== 1) {
                        finish("ambiguous");
                        return true;
                    }
                    matching[0].click();
                    if (selected()) {
                        finish("committed");
                        return true;
                    }
                    observer.disconnect();
                    observer = new MutationObserver(() => {
                        if (selected()) finish("committed");
                    });
                    observer.observe(control.closest(".ant-select"), {
                        subtree: true,
                        childList: true,
                        characterData: true,
                        attributes: true,
                    });
                    return true;
                };
                observer = new MutationObserver(selectIfReady);
                observer.observe(document.body, {subtree: true, childList: true});
                timer = setTimeout(() => finish("missing"), 3000);
                selectIfReady();
            })
            """,
            {
                "selector": "#formData_product_0_productType",
                "value": str(value),
            },
        )
        if status == "ambiguous":
            raise AlibabaOrderRuleError(
                f"阿里物流属性候选项无法唯一精确匹配“{value}”。"
            )
        if status == "missing":
            raise AlibabaOrderRuleError("阿里物流属性候选列表没有显示。")
        if status != "committed":
            raise AlibabaOrderRuleError("阿里物流属性没有从候选列表中正确选中。")

    async def _fill_product_search_value(
        self,
        page: Any,
        selector: str,
        value: str,
        label: str,
    ) -> None:
        prepared = await self._prepare_ant_combobox(
            page,
            selector,
            value,
            exact=False,
        )
        if int(prepared.get("count") or 0) != 1:
            raise AlibabaOrderRuleError(f"阿里页面缺少{label}字段。")
        if prepared.get("done") is True:
            return
        control = page.locator(selector)
        role = str(prepared.get("role") or "")
        if role != "combobox":
            if not self._search_value_matches(value, prepared.get("value")):
                raise AlibabaOrderRuleError(f"{label}没有从阿里候选项中正确选中。")
            return
        list_id = str(prepared.get("listId") or "").strip()
        if list_id and re.fullmatch(r"[A-Za-z0-9_:-]+", list_id):
            if str(prepared.get("expanded") or "").casefold() != "true":
                await control.press("ArrowDown")
            committed = await self._commit_ant_popup_option(
                page,
                selector,
                value,
                exact=False,
            )
            if committed.get("status") == "ambiguous":
                raise AlibabaOrderRuleError(
                    f"{label}候选项无法唯一匹配“{value}”。"
                )
            if committed.get("status") == "options_missing":
                raise AlibabaOrderRuleError(f"阿里{label}候选列表没有显示。")
            if committed.get("status") != "committed":
                raise AlibabaOrderRuleError(
                    f"{label}候选项点击后没有提交选中状态。"
                )
            return
        # Compatibility for the uncommon plain combobox variant which does not
        # expose an associated listbox.
        if str(prepared.get("expanded") or "").casefold() != "true":
            await control.press("ArrowDown")
        await control.press("Enter")
        await control.press("Tab")
        if not self._search_value_matches(value, await control.input_value()):
            raise AlibabaOrderRuleError(f"{label}没有从阿里候选项中正确选中。")

    @classmethod
    def _search_value_matches(cls, expected: object, candidate: object) -> bool:
        normalized_expected = cls._normalized_select_text(expected)
        normalized_candidate = cls._normalized_select_text(candidate)
        if normalized_expected == normalized_candidate:
            return True
        # Alibaba renders HS codes in several equivalent forms, including
        # "中国 3926909090" and "3926 9090 90".  Compare the canonical digit
        # sequence while keeping non-numeric select fields on strict matching.
        if re.fullmatch(r"\d{6,12}", normalized_expected):
            if re.sub(r"\D", "", normalized_candidate) == normalized_expected:
                return True
            # Destination-code options include prose which can itself contain
            # unrelated heading numbers, for example:
            # "3926 9099 89 Other ... headings 3901 to 3914".  Compare each
            # standalone 6-12 digit code token instead of concatenating every
            # digit from the entire description.
            code_tokens = re.findall(
                r"(?<!\d)(?:\d[\s.\-/]*){5,11}\d(?!\d)",
                normalized_candidate,
            )
            return any(
                re.sub(r"\D", "", token) == normalized_expected
                for token in code_tokens
            )
        return False

    async def _verify_product(
        self,
        page: Any,
        declaration: ProductDeclaration,
        *,
        mid_input_selector: str = "",
    ) -> None:
        expected = {
            "#formData_product_0_nameCn": declaration.name_cn,
            "#formData_product_0_nameEn": declaration.name_en,
            "#formData_product_0_material": declaration.material,
            "#formData_product_0_purpose": declaration.purpose,
            "#formData_product_0_hscode": declaration.china_hs_code,
            "#formData_product_0_quantity": str(declaration.quantity),
            "#formData_product_0_declarationValue": format(
                declaration.declared_unit_price_usd,
                "f",
            ),
        }
        if mid_input_selector:
            expected[mid_input_selector] = DEFAULT_MID_CODE
        destination = page.locator("#formData_product_0_destinationHscode")
        destination_count = await destination.count()
        if destination_count == 1:
            expected["#formData_product_0_destinationHscode"] = (
                declaration.destination_hs_code or ""
            )
        elif declaration.destination_hs_code is not None:
            raise AlibabaOrderRuleError("阿里页面缺少目的国 HS 编码字段。")
        actual_values = await self._read_input_values(
            page,
            tuple(expected),
            field_group="商品",
        )
        for selector, value in expected.items():
            actual = actual_values[selector].strip()
            if selector == mid_input_selector:
                if actual != value:
                    raise AlibabaOrderRuleError("MID 代码填写后回读不一致。")
                continue
            if selector.casefold().endswith("hscode") and self._search_value_matches(
                value,
                actual,
            ):
                continue
            if selector == "#formData_product_0_declarationValue":
                try:
                    if Decimal(actual).quantize(
                        Decimal("0.01")
                    ) == declaration.declared_unit_price_usd:
                        continue
                except InvalidOperation:
                    pass
                raise AlibabaOrderRuleError("阿里申报单价填写后回读不一致。")
            if actual != value:
                raise AlibabaOrderRuleError(
                    f"阿里商品字段填写后回读不一致：{selector}"
                )
        product_type = page.locator("#formData_product_0_productType")
        expected_product_type = self._normalized_select_text(
            declaration.logistics_attribute
        )
        if expected_product_type not in await self._ant_selected_values(product_type):
            raise AlibabaOrderRuleError("物流属性填写后回读不是普货。")
