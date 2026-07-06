from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from lingxing_automation.pages.order_detail_extraction import (
    _clean_detail_recipient_name,
    read_detail_recipient_name,
)


def test_clean_detail_recipient_name_rejects_numeric_range():
    """金额/数量范围不能被当作文件夹收件人。"""

    assert _clean_detail_recipient_name("4 - 100") is None
    assert _clean_detail_recipient_name("55376-9430") is None
    assert _clean_detail_recipient_name("cory") == "cory"


def test_read_detail_recipient_name_prefers_receive_info_wrapper():
    """按 ERP 详情页真实收货信息 DOM 读取收件人，避开表格表头和金额噪音。"""

    html = """
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          .receive-info { width: 640px; }
          .receive-info-title { height: 18px; }
          .receive-info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 20px; }
          .info-wrapper { display: flex; height: 18px; }
          .label { display: inline-block; width: 56px; }
          .value, .ak-width-100p { display: inline-block; min-width: 80px; }
          .table-noise { position: absolute; left: 900px; top: 30px; }
        </style>
      </head>
      <body>
        <div class="order-detail-dialog">
          <div>系统单号 103718781866524953</div>
          <table><thead><tr><th>买家姓名</th></tr></thead></table>
          <div class="table-noise">4 - 100</div>
          <div class="receive-info">
            <div class="receive-info-title">收货信息</div>
            <div class="info-content receive-info-grid">
              <div class="info-wrapper">
                <span class="label">收件人</span>
                <div class="value oneLine">cory</div>
              </div>
              <div class="info-wrapper">
                <span class="label label-right">买家姓名</span>
                <div class="ak-width-100p">Cory Johannes</div>
              </div>
              <div class="info-wrapper">
                <span class="label">电话</span>
                <div class="value oneLine">7634824145</div>
              </div>
              <div class="info-wrapper">
                <span class="label label-right">邮编</span>
                <div class="value oneLine">55376-9430</div>
              </div>
            </div>
          </div>
        </div>
      </body>
    </html>
    """

    async def run() -> str | None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 800})
                await page.set_content(html)
                return await read_detail_recipient_name(page)
            finally:
                await browser.close()

    assert asyncio.run(run()) == "cory"
