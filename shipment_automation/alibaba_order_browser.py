"""Playwright adapter for filling (but never submitting) an Alibaba draft."""

from __future__ import annotations

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
    ShippingAddress,
    TentDeclaration,
    signature_required,
)


ALIBABA_QUOTE_URL = "https://i.alibaba.com/logistics/web/shipping/query"
ALIBABA_QUOTE_ORIGIN_COUNTRY = "中国大陆"
ALIBABA_QUOTE_ORIGIN_CITY = "佛山市"
ALIBABA_QUOTE_ORIGIN_CITY_OPTION = "广东省 / 佛山市"
ALIBABA_DRAFT_HOST = "scm.alibaba.com"
ALIBABA_DRAFT_PATH = "/web/express/order.htm"
ROUTE_NAME_SELECTOR = (
    ".solution-line-container .logistics-brand-tag-title-content"
)
SIGNATURE_LABEL_PATTERN = re.compile(r"快递签收服务")
EXPEDITED_ROUTE_PATTERN = re.compile(r"(?:expedited|加急)", re.IGNORECASE)
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
    route_is_expedited: bool
    signature_available: bool


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
    try:
        browser = await playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=10000,
        )
        if not browser.contexts:
            raise AlibabaOrderRuleError("本机 Chrome 没有可用浏览器上下文。")
    except AlibabaOrderRuleError:
        await playwright.stop()
        raise
    except Exception as exc:
        await playwright.stop()
        raise AlibabaOrderRuleError(
            "无法连接提交电脑上的可见 Chrome，请保持阿里页面和桌面程序开启。"
        ) from exc
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
        address: ShippingAddress | None = None,
        login_config: AlibabaLoginConfig | None = None,
    ) -> None:
        page = await self.prepare_quote_page(login_config=login_config)
        if address is not None:
            await self.fill_quote_page(page, address)
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

    async def fill_quote_page(self, page: Any, address: ShippingAddress) -> None:
        await self._fill_quote_route(page, address)
        await page.bring_to_front()

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

    async def _fill_quote_route(
        self,
        page: Any,
        address: ShippingAddress,
    ) -> None:
        """Prefill every route field exposed by Alibaba without querying.

        The current quote form exposes origin country/city plus destination
        country/postal code.  It has no independent destination-city control;
        Alibaba resolves that city from the destination postal code when the
        operator later clicks Query.
        """

        postal = page.locator("#destination_zipCode")
        try:
            await postal.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "阿里查价页未在 15 秒内显示目的地邮编输入框，请确认登录状态后重试。"
            ) from exc

        controls = page.locator('input[role="combobox"]:visible')
        if await controls.count() != 4:
            raise AlibabaOrderRuleError(
                "阿里查价页的国家或城市控件结构已变化，已停止自动填写以避免填错。"
            )

        await self._select_quote_option(
            page,
            controls.nth(0),
            ALIBABA_QUOTE_ORIGIN_COUNTRY,
            "发货国家",
            accepted=(ALIBABA_QUOTE_ORIGIN_COUNTRY,),
        )
        await self._select_quote_city(
            page,
            controls.nth(1),
        )

        country_values = {
            "US": ("United States", ("美国(US)", "United States(US)")),
            "CA": ("Canada", ("加拿大(CA)", "Canada(CA)")),
        }
        destination_value, accepted_names = country_values.get(
            address.country_code,
            (address.country_name, (address.country_name,)),
        )
        if not str(destination_value or "").strip():
            raise AlibabaOrderRuleError("领星订单缺少阿里查价所需的目的国家。")
        await self._select_quote_option(
            page,
            controls.nth(2),
            destination_value,
            "目的国家",
            accepted=accepted_names,
        )

        await postal.fill(address.postal_code)
        await postal.press("Tab")
        if (await postal.input_value()).strip() != address.postal_code:
            raise AlibabaOrderRuleError("阿里查价页目的邮编填写后回读不一致，已停止。")

    @staticmethod
    async def _select_quote_city(page: Any, control: Any) -> None:
        """Select Foshan from Alibaba's city Cascader, not a normal Select."""

        wrapper = control.locator(
            "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),"
            "' ant-select ')][1]"
        )

        async def selected_text() -> str:
            selected_item = wrapper.locator(".ant-select-selection-item")
            if await selected_item.count() == 1:
                value = (
                    await selected_item.get_attribute("title")
                    or await selected_item.inner_text()
                )
            else:
                value = await wrapper.inner_text()
            return re.sub(r"\s+", " ", str(value or "").strip())

        accepted = {
            ALIBABA_QUOTE_ORIGIN_CITY.casefold(),
            "佛山".casefold(),
            ALIBABA_QUOTE_ORIGIN_CITY_OPTION.casefold(),
        }
        if (await selected_text()).casefold() in accepted:
            return

        await wrapper.click()
        if await control.get_attribute("readonly") is None:
            await control.fill(ALIBABA_QUOTE_ORIGIN_CITY)
        options = page.locator(
            ".origin-city-dropdown:visible "
            "li.ant-cascader-menu-item[role='menuitemcheckbox']:visible"
        )
        try:
            await options.first.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "阿里查价页的发货城市候选列表没有显示。"
            ) from exc

        expected = ALIBABA_QUOTE_ORIGIN_CITY_OPTION.casefold()
        records = await AlibabaOrderBrowser._ant_option_records(options)
        matching = [
            index
            for index, (_title, text) in enumerate(records)
            if re.sub(r"\s+", " ", text.strip()).casefold() == expected
        ]
        if len(matching) != 1:
            raise AlibabaOrderRuleError(
                "阿里查价页的发货城市候选项无法唯一匹配“广东省 / 佛山市”。"
            )
        await options.nth(matching[0]).click()
        selected_values = await AlibabaOrderBrowser._wait_for_ant_values(
            control,
            tuple(accepted),
            timeout_ms=1500,
        )
        chosen = (await selected_text()).casefold()
        invalid = str(await control.get_attribute("aria-invalid") or "").casefold()
        if (
            invalid == "true"
            or chosen not in accepted
            or not any(value in accepted for value in selected_values)
        ):
            raise AlibabaOrderRuleError(
                "阿里查价页的发货城市没有从候选列表中正确选中。"
            )

    @staticmethod
    async def _select_quote_option(
        page: Any,
        control: Any,
        value: str,
        label: str,
        *,
        accepted: tuple[str, ...],
    ) -> None:
        wrapper = control.locator(
            "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),"
            "' ant-select ')][1]"
        )

        async def selected_text() -> str:
            selected_item = wrapper.locator(".ant-select-selection-item")
            if await selected_item.count() == 1:
                value = (
                    await selected_item.get_attribute("title")
                    or await selected_item.inner_text()
                )
            else:
                value = await wrapper.inner_text()
            return re.sub(r"\s+", " ", str(value or "").strip())

        accepted_normalized = tuple(
            re.sub(r"\s+", " ", item.strip()).casefold()
            for item in accepted
            if str(item or "").strip()
        )
        current = (await selected_text()).casefold()
        if current in accepted_normalized:
            return

        await wrapper.click()
        if await control.get_attribute("readonly") is None:
            await control.fill(value)
        options = page.locator(
            ".ant-select-dropdown:visible .ant-select-item-option:visible"
        )
        try:
            await options.first.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise AlibabaOrderRuleError(
                f"阿里查价页的{label}候选列表没有显示。"
            ) from exc
        records = await AlibabaOrderBrowser._ant_option_records(options)
        matching = [
            index
            for index, (_title, text) in enumerate(records)
            if re.sub(r"\s+", " ", text.strip()).casefold()
            in accepted_normalized
        ]
        if len(matching) != 1:
            raise AlibabaOrderRuleError(
                f"阿里查价页的{label}候选项无法唯一匹配。"
            )
        await options.nth(matching[0]).click()
        selected_values = await AlibabaOrderBrowser._wait_for_ant_values(
            control,
            accepted_normalized,
            timeout_ms=1500,
        )
        chosen = (await selected_text()).casefold()
        invalid = str(await control.get_attribute("aria-invalid") or "").casefold()
        if (
            invalid == "true"
            or chosen not in accepted_normalized
            or not any(value in accepted_normalized for value in selected_values)
        ):
            raise AlibabaOrderRuleError(
                f"阿里查价页的{label}没有从候选列表中正确选中。"
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
        return AlibabaDraftFacts(
            url=page.url,
            route=AlibabaRoute(route_name),
            total_weight_kg=weight,
            route_is_expedited=bool(EXPEDITED_ROUTE_PATTERN.search(route_name)),
            signature_available=await signature_locator.count() == 1,
        )

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
        declaration: TentDeclaration,
        expedited: bool,
        signature_requested: bool,
        facts: AlibabaDraftFacts | None = None,
    ) -> AlibabaDraftFillResult:
        facts = facts or await self.inspect_draft(page)
        if expedited and not facts.route_is_expedited:
            raise AlibabaOrderRuleError(
                "已勾选“加急订单”，但当前线路名称不含 Expedited/加急。"
                "请返回查价页选择加急线路后重试。"
            )
        if facts.route_is_expedited and not expedited:
            raise AlibabaOrderRuleError(
                "当前线路属于 Expedited/加急线路，但本单没有勾选“加急订单”。"
                "请返回软件勾选加急订单后重试。"
            )
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

        await self._fill_receiver_address(page, address)
        await self._fill_product(page, declaration)
        customer_order = page.get_by_role("textbox", name="客户订单号")
        if await customer_order.count() != 1:
            raise AlibabaOrderRuleError("无法唯一定位阿里草稿的客户订单号字段。")
        expected_customer_order_no = str(customer_order_no or "").strip()
        if not expected_customer_order_no:
            raise AlibabaOrderRuleError("客户订单号不能为空。")
        await customer_order.fill(expected_customer_order_no)
        if (await customer_order.input_value()).strip() != expected_customer_order_no:
            raise AlibabaOrderRuleError("客户订单号填写后回读不一致，已停止。")

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

        await self._verify_product(page, declaration)
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
        """Fill independent inputs in one browser round trip.

        Playwright's ``fill`` is intentionally action-oriented, but calling it
        once per field makes a visible remote Chrome draft crawl.  Alibaba's
        form is React-controlled, so use the native value setter and dispatch
        the same bubbling input/change events that React consumes.  Focusing
        each field also preserves the blur order of normal sequential entry.
        """

        entries = [
            {"selector": selector, "value": str(value)}
            for selector, value in values.items()
        ]
        results = await page.evaluate(
            """
            entries => entries.map(entry => {
                const nodes = document.querySelectorAll(entry.selector);
                if (nodes.length !== 1) {
                    return {count: nodes.length, value: ""};
                }
                const element = nodes[0];
                const supported = element instanceof HTMLInputElement
                    || element instanceof HTMLTextAreaElement;
                const style = supported ? window.getComputedStyle(element) : null;
                const visible = supported
                    && element.getClientRects().length > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden";
                if (
                    !supported
                    || !visible
                    || element.disabled
                    || element.readOnly
                ) {
                    return {
                        count: 1,
                        value: element.value,
                        editable: false,
                    };
                }
                element.focus({preventScroll: true});
                const prototype = element instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(
                    prototype,
                    "value"
                );
                if (!descriptor || typeof descriptor.set !== "function") {
                    return {count: 1, value: null, editable: false};
                }
                descriptor.set.call(element, entry.value);
                element.dispatchEvent(new Event("input", {
                    bubbles: true,
                    composed: true,
                }));
                element.dispatchEvent(new Event("change", {
                    bubbles: true,
                    composed: true,
                }));
                return {count: 1, value: element.value, editable: true};
            })
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
            if str(result.get("value") or "") != entry["value"]:
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
        await confirm_button.click()
        await dialog.wait_for(state="hidden", timeout=10000)

        validation = page.get_by_text("收货人信息校验不通过", exact=False)
        if await validation.count() and await validation.first.is_visible():
            raise AlibabaOrderRuleError(
                "阿里页面保存地址后仍提示校验不通过，请在当前草稿中人工检查地址。"
            )
        # Reopen the saved address and read every editable field back.  This is
        # the only reliable proof that the React form retained all split lines.
        refreshed_edit_buttons = await self._receiver_edit_buttons(page)
        await refreshed_edit_buttons.nth(1).click()
        await dialog.wait_for(state="visible")
        try:
            await self._verify_address_dialog_fields(page, address)
        finally:
            cancel_button = await self._address_dialog_action(dialog, "取消", 0)
            await cancel_button.click()
            await dialog.wait_for(state="hidden", timeout=10000)

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
        control = page.locator(selector)
        if await control.count() != 1:
            raise AlibabaOrderRuleError(f"阿里地址的{label}字段已变化。")
        await control.fill(value)
        await control.press("ArrowDown")
        options = await self._ant_popup_options(page, control, label)
        expected = self._normalized_select_text(value)
        records = await self._ant_option_records(options)
        matching: list[int] = []
        for index, (raw_title, raw_text) in enumerate(records):
            title = self._normalized_select_text(raw_title)
            text = self._normalized_select_text(raw_text)
            if expected in {title, text}:
                matching.append(index)
        if len(matching) != 1:
            raise AlibabaOrderRuleError(
                f"阿里地址的{label}候选项无法唯一精确匹配“{value}”。"
            )
        await options.nth(matching[0]).click()
        selected_values = await self._wait_for_ant_values(
            control,
            (value,),
            timeout_ms=1500,
        )
        invalid = str(await control.get_attribute("aria-invalid") or "").casefold()
        if expected not in selected_values or invalid == "true":
            raise AlibabaOrderRuleError(f"阿里地址的{label}没有从候选列表中选中。")

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
        declaration: TentDeclaration,
    ) -> None:
        values = {
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
        await self._fill_input_values(
            page,
            values,
            field_group="商品",
        )

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
        wrapper = control.locator(ANT_SELECT_ROOT_XPATH)
        if await wrapper.count() != 1:
            raise AlibabaOrderRuleError("阿里物流属性控件结构已变化。")
        expected = self._normalized_select_text(value)
        if expected in await self._ant_selected_values(control):
            return
        await wrapper.click()
        options = page.locator(
            ".product-type-dropdown "
            ".ant-cascader-menu-item[role='menuitemcheckbox']"
        )
        try:
            await options.first.wait_for(state="visible", timeout=3000)
        except Exception as exc:
            raise AlibabaOrderRuleError("阿里物流属性候选列表没有显示。") from exc
        records = await self._ant_option_records(options)
        matching: list[int] = []
        for index, (raw_title, raw_text) in enumerate(records):
            title = self._normalized_select_text(raw_title)
            text = self._normalized_select_text(raw_text)
            if expected in {title, text}:
                matching.append(index)
        if len(matching) != 1:
            raise AlibabaOrderRuleError(
                f"阿里物流属性候选项无法唯一精确匹配“{value}”。"
            )
        semantic_option = page.get_by_role(
            "menuitemcheckbox",
            name=value,
            exact=True,
        )
        target = (
            semantic_option
            if await semantic_option.count() == 1
            else options.nth(matching[0])
        )
        await target.click()
        await control.press("Escape")
        if expected not in await self._wait_for_ant_values(
            control,
            (value,),
            timeout_ms=1500,
        ):
            raise AlibabaOrderRuleError("阿里物流属性没有从候选列表中正确选中。")

    async def _fill_product_search_value(
        self,
        page: Any,
        selector: str,
        value: str,
        label: str,
    ) -> None:
        control = page.locator(selector)
        if await control.count() != 1:
            raise AlibabaOrderRuleError(f"阿里页面缺少{label}字段。")
        if (await control.input_value()).strip() != value:
            await control.fill(value)
            if str(await control.get_attribute("role") or "") == "combobox":
                list_id = str(
                    await control.get_attribute("aria-controls") or ""
                ).strip()
                if list_id and re.fullmatch(r"[A-Za-z0-9_:-]+", list_id):
                    options = await self._ant_popup_options(page, control, label)
                    expected = self._normalized_select_text(value)
                    records = await self._ant_option_records(options)
                    token = re.compile(
                        rf"(?<![a-z0-9]){re.escape(expected)}(?![a-z0-9])"
                    )
                    matching = [
                        index
                        for index, (title, text) in enumerate(records)
                        if expected
                        in {
                            self._normalized_select_text(title),
                            self._normalized_select_text(text),
                        }
                        or token.search(self._normalized_select_text(title))
                        or token.search(self._normalized_select_text(text))
                    ]
                    if len(matching) != 1:
                        raise AlibabaOrderRuleError(
                            f"{label}候选项无法唯一匹配“{value}”。"
                        )
                    option = options.nth(matching[0])
                    option_handle = await option.element_handle()
                    if option_handle is None:
                        raise AlibabaOrderRuleError(f"{label}候选项已失效。")
                    try:
                        await option.click()
                        if not await self._wait_for_option_commit(option_handle):
                            raise AlibabaOrderRuleError(
                                f"{label}候选项点击后没有提交选中状态。"
                            )
                    finally:
                        await option_handle.dispose()
                else:
                    # Some Alibaba sessions expose a plain combobox without an
                    # associated listbox.  Keyboard commit remains immediate;
                    # do not impose the old unconditional 500 ms delay.
                    await control.press("ArrowDown")
                    await control.press("Enter")
                await control.press("Tab")
        if (await control.input_value()).strip() != value:
            raise AlibabaOrderRuleError(f"{label}没有从阿里候选项中正确选中。")

    async def _verify_product(
        self,
        page: Any,
        declaration: TentDeclaration,
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
