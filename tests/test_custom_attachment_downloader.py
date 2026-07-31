from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.cli import build_parser
from lingxing_automation.models import BatchOrderItem, CustomZipDownloadResult
from lingxing_automation.services import custom_attachment_downloader
from lingxing_automation.services.custom_attachment_downloader import (
    CUSTOM_ZIP_DOWNLOADED,
    choose_zip_entry_from_popover_entries,
    build_item_match_payload,
    normalize_item_match_text,
    sanitize_zip_filename,
    unique_zip_target_path,
)
from lingxing_automation.services.custom_zip_downloader import _filter_interactable_zip_targets


def _item() -> BatchOrderItem:
    """构造定制化附件下载测试所需的订单条目。"""
    return BatchOrderItem(
        system_order_no="103709545124988424",
        platform_order_no="113-0987796-6853040",
        row_text="",
        asin="B0D5134SJ3",
        sku="canopytents",
    )


def test_sanitize_zip_filename_keeps_zip_suffix_and_replaces_windows_invalid_chars():
    """验证定制化附件下载中的清洗zip文件名保留zip后缀并替换Windows无效字符场景。"""
    assert sanitize_zip_filename('bad<>:"/\\|?* name.zip') == "bad_ name.zip"
    assert sanitize_zip_filename("") == "customization_images.zip"
    assert sanitize_zip_filename("B0D5134SJ3_19_Customized") == "B0D5134SJ3_19_Customized.zip"


def test_unique_zip_target_path_stays_inside_order_folder_and_does_not_overwrite(tmp_path):
    """验证定制化附件下载中的唯一zip目标路径保持内部 订单文件夹 并 不会 覆盖场景。"""
    existing = tmp_path / "B0D5134SJ3_19_Customized.zip"
    existing.write_bytes(b"old")

    target = unique_zip_target_path(tmp_path, "B0D5134SJ3_19_Customized.zip")

    assert target == tmp_path / "B0D5134SJ3_19_Customized (2).zip"
    assert target.parent == tmp_path


def test_build_item_match_payload_strips_list_quantity_suffix_from_sku():
    """验证定制化附件下载中的生成条目匹配载荷去除列表数量后缀来自SKU场景。"""
    item = _item()
    item.sku = "canopytents 共1"

    assert normalize_item_match_text("canopytents 共1") == "canopytents"
    assert build_item_match_payload(item)["sku"] == "canopytents"


def test_choose_zip_entry_prefers_explicit_zip_even_if_not_bottom():
    """验证定制化附件下载中的选择zip条目优先使用明确zip即使如果不底部场景。"""
    entries = [
        {"entry_id": "png", "text": "logo.png", "top": 100, "index": 0},
        {"entry_id": "zip", "text": "B0D5134SJ3_19_Customized.zip", "top": 140, "index": 1},
        {"entry_id": "pdf", "text": "proof.pdf", "top": 180, "index": 2},
    ]

    chosen = choose_zip_entry_from_popover_entries(entries)

    assert chosen is not None
    assert chosen["entry_id"] == "zip"


def test_choose_zip_entry_uses_bottom_non_media_when_suffix_is_hidden():
    """验证定制化附件下载中的选择zip条目使用底部非媒体当后缀为隐藏场景。"""
    entries = [
        {"entry_id": "img1", "text": "8047f539-8375-26bc-2955-logo.png", "top": 120, "index": 0},
        {"entry_id": "img2", "text": "e4ab9b01-09bc-1637-e788-proof.pdf", "top": 150, "index": 1},
        {"entry_id": "zip", "text": "B0CRRGTPFH_90_Customize...", "top": 190, "index": 2},
    ]

    chosen = choose_zip_entry_from_popover_entries(entries)

    assert chosen is not None
    assert chosen["entry_id"] == "zip"


def test_filter_interactable_zip_targets_keeps_offscreen_product_rows():
    """验证定制化附件下载中的过滤可交互zip目标保留屏幕外产品行场景。"""
    targets = [
        {
            "row_index": 1,
            "asin": "B0CRKYV7C9",
            "trigger_id": "covered-list-row",
            "trigger_text": "共2",
            "trigger_is_interactable": False,
        },
        {
            "row_index": 2,
            "asin": "B0CRKYV7C9",
            "trigger_id": "detail-dialog-row",
            "trigger_text": "共2",
            "trigger_is_interactable": True,
        },
    ]

    filtered = _filter_interactable_zip_targets(targets)

    assert [target["trigger_id"] for target in filtered] == ["covered-list-row", "detail-dialog-row"]
    assert [target["row_index"] for target in filtered] == [1, 2]


def test_filter_interactable_zip_targets_keeps_latest_marker_for_same_button():
    """验证定制化附件下载中的过滤可交互zip目标保留最新标记用于相同按钮场景。"""
    targets = [
        {
            "row_index": 1,
            "asin": "B0CQLN5GNL",
            "trigger_id": "stale-marker",
            "trigger_text": "共4",
            "trigger_is_interactable": True,
            "trigger_top": 800,
            "trigger_left": 1320,
        },
        {
            "row_index": 2,
            "asin": "B0CQLN5GNL",
            "trigger_id": "live-marker",
            "trigger_text": "共4",
            "trigger_is_interactable": True,
            "trigger_top": 801,
            "trigger_left": 1321,
        },
        {
            "row_index": 3,
            "asin": "B0CQLN5GNL",
            "trigger_id": "next-row-marker",
            "trigger_text": "共5",
            "trigger_is_interactable": True,
            "trigger_top": 938,
            "trigger_left": 1320,
        },
    ]

    filtered = _filter_interactable_zip_targets(targets)

    assert [target["trigger_id"] for target in filtered] == ["live-marker", "next-row-marker"]
    assert [target["row_index"] for target in filtered] == [1, 2]


def test_custom_zip_download_result_log_shape_for_dom_download():
    """验证定制化附件下载中的定制化 zip下载结果 log 结构用于DOM下载场景。"""
    result = CustomZipDownloadResult(
        status=CUSTOM_ZIP_DOWNLOADED,
        zip_filename="B0D5134SJ3_19_Customized.zip",
        zip_path="Z:/order/B0D5134SJ3_19_Customized.zip",
        zip_candidates=["B0D5134SJ3_19_Customized.zip"],
        platform_order_no="113-0987796-6853040",
        asin="B0D5134SJ3",
        sku="canopytents",
        product_row_match="dom:closest-row",
        open_method="hover",
        diagnostics={"dom_open_method": "hover", "attachment_entry_count": 5},
    )

    assert result.to_log_dict() == {
        "custom_zip_status": CUSTOM_ZIP_DOWNLOADED,
        "custom_zip_filename": "B0D5134SJ3_19_Customized.zip",
        "custom_zip_path": "Z:/order/B0D5134SJ3_19_Customized.zip",
        "custom_zip_trigger_text": None,
        "custom_zip_candidates": ["B0D5134SJ3_19_Customized.zip"],
        "custom_zip_error": None,
        "custom_zip_warnings": [],
        "custom_zip_platform_order_no": "113-0987796-6853040",
        "custom_zip_asin": "B0D5134SJ3",
        "custom_zip_sku": "canopytents",
        "custom_zip_product_row_match": "dom:closest-row",
        "custom_zip_diagnostics": {"dom_open_method": "hover", "attachment_entry_count": 5},
        "custom_zip_open_method": "hover",
        "custom_zip_prepared_before_writeback": False,
        "custom_zip_candidate_entries": [],
    }


def test_cli_has_no_download_custom_zip_switch():
    """验证定制化附件下载中的命令行包含无下载 定制化 zip开关场景。"""
    args = build_parser().parse_args(["--batch", "--no-download-custom-zip"])

    assert args.no_download_custom_zip is True


class _FakeLocator:
    def __init__(self, page: "_FakePage") -> None:
        """构造可记录 hover/click 的 Playwright locator 替身。"""
        self.page = page

    @property
    def first(self):
        return self

    async def hover(self, **kwargs):
        self.page.actions.append(("hover", bool(kwargs.get("force"))))

    async def click(self, **kwargs):
        self.page.actions.append(("click", bool(kwargs.get("force"))))

    async def bounding_box(self):
        return {"x": 920, "y": 860, "width": 42, "height": 18}


class _FakeDownloadInfo:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @property
    def value(self):
        async def _value():
            return "downloaded"

        return _value()


class _FakePage:
    def __init__(self, *, hit_ok: bool = True) -> None:
        """构造附件下载测试所需的 page 替身。"""
        self.actions: list[tuple[str, bool]] = []
        self.waits: list[int] = []
        self.download_timeouts: list[int | None] = []
        self.hit_ok = hit_ok
        self.wait_calls = 0

    def locator(self, _selector):
        return _FakeLocator(self)

    async def evaluate(self, script, arg=None):
        if "elementFromPoint" in script and "marked_element_missing" in script:
            if self.hit_ok:
                return {
                    "ok": True,
                    "reason": "hit",
                    "x": 941,
                    "y": 869,
                    "top_tag": "SPAN",
                    "top_text": "共4",
                    "rect": {"left": 920, "top": 860, "width": 42, "height": 18},
                }
            return {
                "ok": False,
                "reason": "covered_by_other_element",
                "x": 941,
                "y": 869,
                "top_tag": "DIV",
                "top_text": "消息中心",
                "rect": {"left": 920, "top": 860, "width": 42, "height": 18},
            }
        return None

    async def wait_for_timeout(self, timeout_ms):
        self.waits.append(timeout_ms)

    def expect_download(self, **kwargs):
        self.download_timeouts.append(kwargs.get("timeout"))
        return _FakeDownloadInfo()


def test_open_attachment_popover_uses_hover_when_zip_entries_appear(monkeypatch):
    """附件浮层 hover 成功时，不继续尝试点击附件入口。"""
    page = _FakePage()

    async def fake_wait(_page, trigger_rect=None):
        return (
            [{"entry_id": "zip-entry", "text": "B0D5134SJ3_CustomizedInfo.zip"}],
            {"entry_id": "zip-entry", "text": "B0D5134SJ3_CustomizedInfo.zip"},
        )

    monkeypatch.setattr(custom_attachment_downloader, "_wait_for_zip_entries", fake_wait)

    entries, chosen, method = asyncio.run(custom_attachment_downloader._open_attachment_popover(page, "trigger"))

    assert method == "hover"
    assert chosen == {"entry_id": "zip-entry", "text": "B0D5134SJ3_CustomizedInfo.zip"}
    assert entries
    assert page.actions == [("hover", False)]


def test_open_attachment_popover_skips_click_when_trigger_is_covered(monkeypatch):
    """附件入口被顶部栏/消息按钮覆盖时，不执行 click/force click。"""
    page = _FakePage(hit_ok=False)

    async def fake_wait(_page, trigger_rect=None):
        return [], None

    monkeypatch.setattr(custom_attachment_downloader, "_wait_for_zip_entries", fake_wait)

    entries, chosen, method = asyncio.run(custom_attachment_downloader._open_attachment_popover(page, "trigger"))

    assert entries == []
    assert chosen is None
    assert method.startswith("safe_click_skipped:")
    assert ("click", True) not in page.actions
    assert not any(action == "click" for action, _force in page.actions)


def test_open_attachment_popover_click_fallback_requires_hit_test(monkeypatch):
    """hover/DOM 事件都失败后，只在命中检测通过时执行普通 click。"""
    page = _FakePage(hit_ok=True)

    async def fake_wait(_page, trigger_rect=None):
        page.wait_calls += 1
        if page.wait_calls >= 4:
            return (
                [{"entry_id": "zip-entry", "text": "B0D5134SJ3_CustomizedInfo.zip"}],
                {"entry_id": "zip-entry", "text": "B0D5134SJ3_CustomizedInfo.zip"},
            )
        return [], None

    monkeypatch.setattr(custom_attachment_downloader, "_wait_for_zip_entries", fake_wait)

    _entries, chosen, method = asyncio.run(custom_attachment_downloader._open_attachment_popover(page, "trigger"))

    assert method == "click"
    assert chosen is not None
    assert ("click", False) in page.actions
    assert ("click", True) not in page.actions


def test_click_entry_and_wait_for_download_uses_normal_click_after_hit_test():
    """点击 zip 条目前先命中检测，且不使用 force click。"""
    page = _FakePage(hit_ok=True)

    result = asyncio.run(custom_attachment_downloader._click_entry_and_wait_for_download(page, "entry"))

    assert result == "downloaded"
    assert page.actions == [("click", False)]
    assert page.download_timeouts == [custom_attachment_downloader.CUSTOM_ZIP_DOWNLOAD_TIMEOUT_MS]
    assert page.download_timeouts == [10000]


def test_click_entry_and_wait_for_download_skips_covered_entry():
    """zip 条目被遮挡时直接报错，避免误点其它浮层。"""
    page = _FakePage(hit_ok=False)

    with pytest.raises(RuntimeError, match="zip 附件条目点击前命中检测失败"):
        asyncio.run(custom_attachment_downloader._click_entry_and_wait_for_download(page, "entry"))

    assert page.actions == []
