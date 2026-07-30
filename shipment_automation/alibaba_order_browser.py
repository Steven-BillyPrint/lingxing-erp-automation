"""Playwright adapter for filling (but never submitting) an Alibaba draft."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from .alibaba_ordering import (
    AlibabaOrderRuleError,
    AlibabaRoute,
    ShippingAddress,
    TentDeclaration,
    signature_required,
)


ALIBABA_QUOTE_URL = "https://i.alibaba.com/logistics/web/shipping/query"
ALIBABA_DRAFT_HOST = "scm.alibaba.com"
ALIBABA_DRAFT_PATH = "/web/express/order.htm"
ROUTE_NAME_SELECTOR = (
    ".solution-line-container .logistics-brand-tag-title-content"
)
SIGNATURE_LABEL_PATTERN = re.compile(r"快递签收服务")
EXPEDITED_ROUTE_PATTERN = re.compile(r"(?:expedited|加急)", re.IGNORECASE)


def is_alibaba_draft_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == ALIBABA_DRAFT_HOST
        and parsed.path == ALIBABA_DRAFT_PATH
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
        browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
        if not browser.contexts:
            raise AlibabaOrderRuleError("本机 Chrome 没有可用浏览器上下文。")
        yield browser.contexts[0]
    except AlibabaOrderRuleError:
        raise
    except Exception as exc:
        raise AlibabaOrderRuleError(
            "无法连接提交电脑上的可见 Chrome，请保持阿里页面和桌面程序开启。"
        ) from exc
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

    async def open_quote_page(self) -> None:
        page = next(
            (item for item in self.context.pages if item.url == ALIBABA_QUOTE_URL),
            None,
        )
        if page is None:
            page = await self.context.new_page()
            await page.goto(ALIBABA_QUOTE_URL, wait_until="domcontentloaded")
        await page.bring_to_front()

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
        await page.locator(
            'input[id^="formData_package_"][id$="_weight"]'
        ).first.wait_for(state="visible", timeout=15000)
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
        weights = page.locator(
            'input[id^="formData_package_"][id$="_weight"]'
        )
        total = Decimal("0")
        for index in range(await weights.count()):
            weight_input = weights.nth(index)
            identifier = str(await weight_input.get_attribute("id") or "")
            match = re.fullmatch(r"formData_package_(\d+)_weight", identifier)
            if match is None:
                raise AlibabaOrderRuleError("阿里包裹重量字段结构已变化，请人工处理。")
            quantity_input = page.locator(
                f"#formData_package_{match.group(1)}_quantity"
            )
            try:
                weight = Decimal(await weight_input.input_value())
                quantity = (
                    Decimal(await quantity_input.input_value())
                    if await quantity_input.count() == 1
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
        system_order_no: str,
        address: ShippingAddress,
        declaration: TentDeclaration,
        expedited: bool,
        signature_requested: bool,
    ) -> AlibabaDraftFillResult:
        facts = await self.inspect_draft(page)
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
        if await customer_order.count() == 1:
            await customer_order.fill(str(system_order_no))

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

    async def _fill_receiver_address(
        self,
        page: Any,
        address: ShippingAddress,
    ) -> None:
        edit_buttons = page.get_by_role("button", name="编辑", exact=True)
        if await edit_buttons.count() != 2:
            raise AlibabaOrderRuleError("无法定位收货地址的“编辑”按钮。")
        await edit_buttons.nth(1).click()
        dialog = page.get_by_role("dialog", name="修改地址")
        await dialog.wait_for(state="visible")

        country_wrapper = page.locator("#address_country").locator(
            "xpath=ancestor::*[contains(@class,'ant-select')][1]"
        )
        country_text = (await country_wrapper.inner_text()).casefold()
        acceptable_country_names = {
            "US": ("united states", "美国"),
            "CA": ("canada", "加拿大"),
        }.get(address.country_code, (address.country_name.casefold(),))
        if not any(name.casefold() in country_text for name in acceptable_country_names):
            await dialog.get_by_role("button", name="取消").click()
            raise AlibabaOrderRuleError(
                "阿里草稿目的国与领星订单不一致，地址未保存。"
            )

        await page.locator("#companyNameEn").fill(address.company)
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
        await self._select_ant_option(
            page,
            "#address_address",
            address.address1,
            "详细地址",
            wait_for_suggestions=True,
        )
        await page.locator("#address_address2").fill(address.address2)
        await page.locator("#address_zip").fill(address.postal_code)
        await page.locator("#contactPerson").fill(address.recipient)
        await page.locator("#contact_phoneCode").fill(address.dial_code)
        await page.locator("#contact_mobileNo").fill(address.phone)
        await page.locator("#contact_email").fill(address.email)
        await self._verify_address_dialog_fields(page, address)
        await dialog.get_by_role("button", name="确定").click()
        await dialog.wait_for(state="hidden", timeout=10000)

        validation = page.get_by_text("收货人信息校验不通过", exact=False)
        if await validation.count() and await validation.first.is_visible():
            raise AlibabaOrderRuleError(
                "阿里页面保存地址后仍提示校验不通过，请在当前草稿中人工检查地址。"
            )
        # Reopen the saved address and read every editable field back.  This is
        # the only reliable proof that the React form retained all split lines.
        refreshed_edit_buttons = page.get_by_role("button", name="编辑", exact=True)
        if await refreshed_edit_buttons.count() != 2:
            raise AlibabaOrderRuleError("地址保存后无法稳定定位收货地址。")
        await refreshed_edit_buttons.nth(1).click()
        await dialog.wait_for(state="visible")
        try:
            await self._verify_address_dialog_fields(page, address)
        finally:
            await dialog.get_by_role("button", name="取消").click()
            await dialog.wait_for(state="hidden", timeout=10000)

    async def _verify_address_dialog_fields(
        self,
        page: Any,
        address: ShippingAddress,
    ) -> None:
        expected = {
            "#companyNameEn": address.company,
            "#address_province_name": address.province,
            "#address_city_name": address.city,
            "#address_address": address.address1,
            "#address_address2": address.address2,
            "#address_zip": address.postal_code,
            "#contactPerson": address.recipient,
            "#contact_phoneCode": address.dial_code,
            "#contact_mobileNo": address.phone,
            "#contact_email": address.email,
        }
        for selector, wanted in expected.items():
            field = page.locator(selector)
            if await field.count() != 1:
                raise AlibabaOrderRuleError(f"阿里地址字段已变化：{selector}")
            actual = re.sub(r"\s+", " ", (await field.input_value()).strip())
            normalized_wanted = re.sub(r"\s+", " ", str(wanted).strip())
            if actual != normalized_wanted:
                raise AlibabaOrderRuleError(
                    f"阿里地址字段填写后回读不一致：{selector}"
                )
        if len(await page.locator("#address_address").input_value()) > 35:
            raise AlibabaOrderRuleError("阿里地址1保存后超过 35 个字符，已停止。")

    async def _select_ant_option(
        self,
        page: Any,
        selector: str,
        value: str,
        label: str,
        *,
        wait_for_suggestions: bool = False,
    ) -> None:
        control = page.locator(selector)
        await control.fill(value)
        if wait_for_suggestions:
            await page.wait_for_timeout(800)
        await control.press("ArrowDown")
        await control.press("Enter")
        wrapper = control.locator(
            "xpath=ancestor::*[contains(@class,'ant-select')][1]"
        )
        selected_text = (await wrapper.inner_text()).strip()
        invalid = str(await control.get_attribute("aria-invalid") or "").casefold()
        if not selected_text or invalid == "true":
            raise AlibabaOrderRuleError(f"阿里地址的{label}没有从候选列表中选中。")

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
        for selector, value in values.items():
            control = page.locator(selector)
            if await control.count() != 1:
                raise AlibabaOrderRuleError(f"阿里商品字段已变化：{selector}")
            await control.fill(value)

        await self._fill_product_search_value(
            page,
            "#formData_product_0_hscode",
            declaration.china_hs_code,
            "中国 HS 编码",
        )
        destination = page.locator("#formData_product_0_destinationHscode")
        if declaration.destination_hs_code is None:
            if await destination.count() == 1:
                await destination.fill("")
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
        await product_type.click()
        await product_type.fill(declaration.logistics_attribute)
        await product_type.press("ArrowDown")
        await product_type.press("Enter")

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
            await page.wait_for_timeout(500)
            if str(await control.get_attribute("role") or "") == "combobox":
                await control.press("ArrowDown")
                await control.press("Enter")
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
        }
        for selector, value in expected.items():
            actual = (await page.locator(selector).input_value()).strip()
            if actual != value:
                raise AlibabaOrderRuleError(
                    f"阿里商品字段填写后回读不一致：{selector}"
                )
        price = Decimal(
            (await page.locator("#formData_product_0_declarationValue").input_value()).strip()
        ).quantize(Decimal("0.01"))
        if price != declaration.declared_unit_price_usd:
            raise AlibabaOrderRuleError("阿里申报单价填写后回读不一致。")
        destination = page.locator("#formData_product_0_destinationHscode")
        if await destination.count() == 1:
            actual_destination = (await destination.input_value()).strip()
            if actual_destination != (declaration.destination_hs_code or ""):
                raise AlibabaOrderRuleError("目的国 HS 编码填写后回读不一致。")
        product_type = page.locator("#formData_product_0_productType")
        product_type_wrapper = product_type.locator(
            "xpath=ancestor::*[contains(@class,'ant-select')][1]"
        )
        product_type_text = (await product_type_wrapper.inner_text()).strip()
        if declaration.logistics_attribute not in product_type_text:
            raise AlibabaOrderRuleError("物流属性填写后回读不是普货。")
