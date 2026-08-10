from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from lingxing_automation.services import custom_zip_downloader


class _Download:
    suggested_filename = "B0CRKYV7C9_84_CustomizedInfo.zip"

    def __init__(self, suggested_filename: str | None = None, order_item_id: str | None = None) -> None:
        """初始化下载测试替身的内部状态。"""
        if suggested_filename is not None:
            self.suggested_filename = suggested_filename
        self.order_item_id = order_item_id

    async def save_as(self, path: str) -> None:
        """模拟 Playwright 下载文件保存。"""
        if not self.order_item_id:
            Path(path).write_bytes(b"zip")
            return
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                f"{self.order_item_id}.json",
                json.dumps({"orderItemId": self.order_item_id, "asin": "B0DRCWYC98"}),
            )


def _write_downloads_zip(path: Path, order_item_id: str, asin: str = "B0D5134SJ3") -> None:
    """写入模拟浏览器默认下载目录中的定制 zip。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{order_item_id}.json",
            json.dumps({"orderItemId": order_item_id, "asin": asin}),
        )


def test_download_bundle_stops_after_expected_zip_count(monkeypatch, tmp_path):
    """验证定制化 zip 下载中的下载整单包停止之后预期zip数量场景。"""
    targets = [
        {"row_index": 1, "asin": "B0CRKYV7C9", "trigger_id": "duplicate", "trigger_text": "共2", "trigger_is_interactable": True},
        {"row_index": 2, "asin": "B0CRKYV7C9", "trigger_id": "real", "trigger_text": "共2", "trigger_is_interactable": True},
        {"row_index": 3, "asin": "B0CRKYV7C9", "trigger_id": "extra", "trigger_text": "共2", "trigger_is_interactable": True},
    ]
    opened: list[str] = []
    clicked: list[str] = []

    async def fake_find_targets(page, system_order_no):
        """模拟查找目标行为，隔离测试中的外部依赖。"""
        return targets

    async def fake_open_popover(page, trigger_id):
        """模拟打开 弹层行为，隔离测试中的外部依赖。"""
        opened.append(trigger_id)
        if trigger_id == "duplicate":
            raise TimeoutError("Locator.scroll_into_view_if_needed: Timeout 1200ms exceeded.")
        return ([{"entry_id": f"entry-{trigger_id}", "text": _Download.suggested_filename}], {"entry_id": f"entry-{trigger_id}", "text": _Download.suggested_filename}, "hover")

    async def fake_click(page, entry_id):
        """模拟点击行为，隔离测试中的外部依赖。"""
        clicked.append(entry_id)
        return _Download()

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no="112-7673258-2042661",
            system_order_no="103710738102789762",
            staging_root=tmp_path,
            expected_zip_count=1,
        )
    )

    assert bundle.status == "ok"
    assert len(bundle.zip_files) == 1
    assert bundle.zip_files[0].zip_filename == _Download.suggested_filename
    assert opened == ["duplicate", "real"]
    assert clicked == ["entry-real"]
    assert not (tmp_path / "112-7673258-2042661" / "B0CRKYV7C9_84_CustomizedInfo (2).zip").exists()


def test_download_bundle_reuses_existing_unique_staging_zips_without_clicking(monkeypatch, tmp_path):
    """验证定制化 zip 下载中的下载整单包复用已存在唯一暂存目录zip不依赖点击场景。"""
    order_no = "701-2422110-4725037"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    for filename in [
        "B0CNVLXTWB_43_CustomizedInfo.zip",
        "B0CNVLXTWB_43_CustomizedInfo (2).zip",
        "B0DRCVNYCZ_29_CustomizedInfo.zip",
        "B0DRCWXR7S_30_CustomizedInfo.zip",
        "B0DRCWXR7S_50_CustomizedInfo.zip",
    ]:
        (order_dir / filename).write_bytes(b"zip")

    async def fake_find_targets(page, system_order_no):
        """模拟查找目标行为，隔离测试中的外部依赖。"""
        raise AssertionError("existing complete staging zip set should skip DOM lookup")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103711515575103142",
            staging_root=tmp_path,
            expected_zip_count=4,
        )
    )

    assert bundle.status == "ok"
    assert len(bundle.zip_files) == 4
    canonical_names = {filename.replace(" (2).zip", ".zip") for filename in [item.zip_filename for item in bundle.zip_files]}
    assert canonical_names == {
        "B0CNVLXTWB_43_CustomizedInfo.zip",
        "B0DRCVNYCZ_29_CustomizedInfo.zip",
        "B0DRCWXR7S_30_CustomizedInfo.zip",
        "B0DRCWXR7S_50_CustomizedInfo.zip",
    }


def test_download_bundle_uses_existing_staging_zips_before_duplicate_targets(monkeypatch, tmp_path):
    """验证定制化 zip 下载中的下载整单包使用已存在暂存目录zip之前重复目标场景。"""
    order_no = "701-2422110-4725037"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    for filename in [
        "B0CNVLXTWB_43_CustomizedInfo.zip",
        "B0DRCVNYCZ_29_CustomizedInfo.zip",
        "B0DRCWXR7S_50_CustomizedInfo.zip",
    ]:
        (order_dir / filename).write_bytes(b"zip")

    targets = [
        {"row_index": 4, "asin": "B0DRCWXR7S", "trigger_id": "missing", "trigger_text": "鍏?", "trigger_is_interactable": True},
        {"row_index": 5, "asin": "B0DRCWXR7S", "trigger_id": "duplicate", "trigger_text": "鍏?", "trigger_is_interactable": True},
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        """模拟查找目标行为，隔离测试中的外部依赖。"""
        return targets

    async def fake_open_popover(page, trigger_id):
        """模拟打开 弹层行为，隔离测试中的外部依赖。"""
        opened.append(trigger_id)
        if trigger_id == "duplicate":
            raise TimeoutError("Locator.click: Element is not visible")
        return (
            [{"entry_id": "entry-missing", "text": "B0DRCWXR7S_30_CustomizedInfo.zip"}],
            {"entry_id": "entry-missing", "text": "B0DRCWXR7S_30_CustomizedInfo.zip"},
            "hover",
        )

    async def fake_click(page, entry_id):
        """模拟点击行为，隔离测试中的外部依赖。"""
        return _Download("B0DRCWXR7S_30_CustomizedInfo.zip")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103711515575103142",
            staging_root=tmp_path,
            expected_zip_count=4,
        )
    )

    assert bundle.status == "ok"
    assert [item.zip_filename for item in bundle.zip_files] == [
        "B0CNVLXTWB_43_CustomizedInfo.zip",
        "B0DRCVNYCZ_29_CustomizedInfo.zip",
        "B0DRCWXR7S_50_CustomizedInfo.zip",
        "B0DRCWXR7S_30_CustomizedInfo.zip",
    ]
    assert opened == ["missing"]
    assert (order_dir / "B0DRCWXR7S_30_CustomizedInfo.zip").exists()


def test_download_bundle_reports_missing_order_item_before_invisible_target_error(monkeypatch, tmp_path):
    """验证定制化 zip 下载中的下载整单包报告缺失 订单行 之前不可见目标错误场景。"""
    order_no = "114-5019404-8703446"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    existing_zip = order_dir / "B0CW56CP7M_84_CustomizedInfo.zip"
    with zipfile.ZipFile(existing_zip, "w") as archive:
        archive.writestr("present.json", json.dumps({"orderItemId": "163223573545561"}))

    targets = [
        {"row_index": 4, "asin": "B0CW56CP7M", "trigger_id": "duplicate-hidden", "trigger_text": "共3", "trigger_is_interactable": False},
    ]

    async def fake_find_targets(page, system_order_no):
        """模拟查找目标行为，隔离测试中的外部依赖。"""
        return targets

    async def fake_open_popover(page, trigger_id):
        """模拟打开 弹层行为，隔离测试中的外部依赖。"""
        raise TimeoutError("Locator.click: Element is not visible")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103715030356611759",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"163223573545561", "163223573545681"},
        )
    )

    assert bundle.status == custom_zip_downloader.CUSTOM_ZIP_NOT_FOUND
    assert bundle.error == "定制 zip 缺少 Amazon OrderItemId：163223573545681"
    assert any(
        warning.startswith("custom_zip_target_failed:")
        and "custom_zip_download_error:Locator.click" in warning
        for warning in bundle.warnings
    )


def test_download_bundle_keeps_same_filename_when_order_item_id_differs(monkeypatch, tmp_path):
    """验证定制化 zip 下载中的下载整单包保留相同文件名当 订单行 ID不一致场景。"""
    order_no = "114-7002116-8651431"
    targets = [
        {"row_index": 1, "asin": "B0DRCWYC98", "trigger_id": "row-1", "trigger_text": "共2", "trigger_is_interactable": True},
        {"row_index": 2, "asin": "B0DRCWYC98", "trigger_id": "row-2", "trigger_text": "共2", "trigger_is_interactable": True},
    ]
    downloads = {
        "entry-row-1": _Download("B0DRCWYC98_CustomizedInfo.zip", "162378086609801"),
        "entry-row-2": _Download("B0DRCWYC98_CustomizedInfo.zip", "162378086609841"),
    }

    async def fake_find_targets(page, system_order_no):
        """模拟查找目标行为，隔离测试中的外部依赖。"""
        return targets

    async def fake_open_popover(page, trigger_id):
        """模拟打开 弹层行为，隔离测试中的外部依赖。"""
        entry_id = f"entry-{trigger_id}"
        return (
            [{"entry_id": entry_id, "text": "B0DRCWYC98_CustomizedInfo.zip"}],
            {"entry_id": entry_id, "text": "B0DRCWYC98_CustomizedInfo.zip"},
            "hover",
        )

    async def fake_click(page, entry_id):
        """模拟点击行为，隔离测试中的外部依赖。"""
        return downloads[entry_id]

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103711816409234043",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"162378086609801", "162378086609841"},
        )
    )

    assert bundle.status == "ok"
    assert [item.order_item_id for item in bundle.zip_files] == ["162378086609801", "162378086609841"]
    assert [item.zip_filename for item in bundle.zip_files] == [
        "B0DRCWYC98_CustomizedInfo.zip",
        "B0DRCWYC98_CustomizedInfo (2).zip",
    ]


def test_download_bundle_continues_after_duplicate_order_item_id_for_missing_zip(monkeypatch, tmp_path):
    """已有/重复下载只覆盖一个订单行时，应继续尝试后续附件入口。"""
    order_no = "111-8112209-3174649"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    existing_zip = order_dir / "B0DZ2W2QWK_15_CustomizedInfo.zip"
    with zipfile.ZipFile(existing_zip, "w") as archive:
        archive.writestr("164173871685321.json", json.dumps({"orderItemId": "164173871685321"}))

    targets = [
        {
            "row_index": 1,
            "asin": "B0DZ2W2QWK",
            "sku": "canopytents",
            "target_key": "B0DZ2W2QWK:canopytents:1",
            "trigger_id": "row-1",
            "trigger_text": "共4",
        },
        {
            "row_index": 2,
            "asin": "B0DZ2W2QWK",
            "sku": "TENT-ROLLER-BAG-10X10-50MM",
            "target_key": "B0DZ2W2QWK:TENT-ROLLER-BAG-10X10-50MM:2",
            "trigger_id": "row-2",
            "trigger_text": "共5",
        },
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        filename = (
            "B0DZ2W2QWK_15_CustomizedInfo.zip"
            if trigger_id == "row-1"
            else "B0DZ2W2QWK_90_CustomizedInfo.zip"
        )
        return ([{"entry_id": trigger_id, "text": filename}], {"entry_id": trigger_id, "text": filename}, "hover")

    async def fake_click(page, entry_id):
        if entry_id == "row-1":
            return _Download("B0DZ2W2QWK_15_CustomizedInfo.zip", "164173871685321")
        return _Download("B0DZ2W2QWK_90_CustomizedInfo.zip", "164173871685361")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103719401767966430",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"164173871685321", "164173871685361"},
        )
    )

    assert bundle.status == "ok"
    assert opened == ["row-1", "row-2"]
    assert {item.order_item_id for item in bundle.zip_files} == {"164173871685321", "164173871685361"}
    assert any(item.zip_filename == "B0DZ2W2QWK_90_CustomizedInfo.zip" for item in bundle.zip_files)


def test_download_bundle_prefers_strict_product_rows_over_ancestor_targets(monkeypatch, tmp_path):
    order_no = "113-3416161-4901039"
    targets = [
        {
            "row_index": 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "target_key": "B0CQLN5GNL:Car-Magnet-12x24in-2pcs:1",
            "trigger_id": "ancestor-first-row",
            "trigger_text": "共6",
            "row_match_reason": "trigger-ancestor-10",
        },
        {
            "row_index": 2,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "target_key": "B0CQLN5GNL:Car-Magnet-12x24in-2pcs:2",
            "trigger_id": "strict-first-row",
            "trigger_text": "共6",
            "row_match_reason": "strict-product-table-row",
        },
        {
            "row_index": 3,
            "asin": "B0CNVMQJFX",
            "sku": "Car-Magnet-10x20in-2pcs",
            "target_key": "B0CNVMQJFX:Car-Magnet-10x20in-2pcs:3",
            "trigger_id": "strict-second-row",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        },
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        if trigger_id == "strict-first-row":
            filename = "B0CQLN5GNL_63_CustomizedInfo.zip"
        elif trigger_id == "strict-second-row":
            filename = "B0CNVMQJFX_35_CustomizedInfo.zip"
        else:
            filename = "B0CQLN5GNL_63_CustomizedInfo.zip"
        return ([{"entry_id": trigger_id, "text": filename}], {"entry_id": trigger_id, "text": filename}, "hover")

    async def fake_click(page, entry_id):
        if entry_id == "strict-second-row":
            return _Download("B0CNVMQJFX_35_CustomizedInfo.zip", "164336143368241")
        return _Download("B0CQLN5GNL_63_CustomizedInfo.zip", "164336143368201")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720241100182728",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"164336143368201", "164336143368241"},
        )
    )

    assert bundle.status == "ok"
    assert opened == ["strict-first-row", "strict-second-row"]
    assert {item.order_item_id for item in bundle.zip_files} == {"164336143368201", "164336143368241"}


def test_download_bundle_covers_same_asin_same_sku_rows_by_rowid(monkeypatch, tmp_path):
    order_no = "114-6396416-4441061"
    order_item_ids = [f"1643791758809{i:02d}" for i in range(12)]
    targets = [
        {
            "row_index": index + 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": f"row_{86 + index}",
            "attachment_label": f"artwork-{index + 1}.jpg",
            "trigger_id": f"trigger-{index + 1}",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        }
        for index in range(12)
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        index = int(trigger_id.removeprefix("trigger-"))
        filename = f"B0CQLN5GNL_{index:02d}_CustomizedInfo.zip"
        return ([{"entry_id": trigger_id, "text": filename}], {"entry_id": trigger_id, "text": filename}, "click")

    async def fake_click(page, entry_id):
        index = int(entry_id.removeprefix("trigger-")) - 1
        return _Download(f"B0CQLN5GNL_{index + 1:02d}_CustomizedInfo.zip", order_item_ids[index])

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720497261493880",
            staging_root=tmp_path,
            expected_zip_count=12,
            expected_order_item_ids=set(order_item_ids),
        )
    )

    assert bundle.status == "ok"
    assert opened == [f"trigger-{index}" for index in range(1, 13)]
    assert {item.order_item_id for item in bundle.zip_files} == set(order_item_ids)


def test_download_bundle_reuses_existing_and_downloads_missing_same_asin_rows(monkeypatch, tmp_path):
    order_no = "114-6396416-4441061"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    with zipfile.ZipFile(order_dir / "B0CQLN5GNL_01_CustomizedInfo.zip", "w") as archive:
        archive.writestr("present.json", json.dumps({"orderItemId": "164379175880901"}))

    targets = [
        {
            "row_index": index + 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": f"row_{index + 1}",
            "attachment_label": f"same-asin-{index + 1}.jpg",
            "trigger_id": f"row-{index + 1}",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        }
        for index in range(3)
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        index = int(trigger_id.removeprefix("row-"))
        filename = f"B0CQLN5GNL_0{index}_CustomizedInfo.zip"
        return ([{"entry_id": trigger_id, "text": filename}], {"entry_id": trigger_id, "text": filename}, "click")

    async def fake_click(page, entry_id):
        index = int(entry_id.removeprefix("row-"))
        order_item_id = {
            1: "164379175880901",
            2: "164379175880902",
            3: "164379175880903",
        }[index]
        return _Download(f"B0CQLN5GNL_0{index}_CustomizedInfo.zip", order_item_id)

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720497261493880",
            staging_root=tmp_path,
            expected_zip_count=3,
            expected_order_item_ids={"164379175880901", "164379175880902", "164379175880903"},
        )
    )

    assert bundle.status == "ok"
    assert opened == ["row-1", "row-2", "row-3"]
    assert {item.order_item_id for item in bundle.zip_files} == {
        "164379175880901",
        "164379175880902",
        "164379175880903",
    }


def test_download_bundle_continues_after_same_asin_row_without_zip(monkeypatch, tmp_path):
    order_no = "114-6396416-4441061"
    targets = [
        {
            "row_index": 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": "row_95",
            "attachment_label": "logo.jpg",
            "trigger_id": "missing-zip-row",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        },
        {
            "row_index": 2,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": "row_96",
            "attachment_label": "artwork.jpg",
            "trigger_id": "valid-row",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        },
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        if trigger_id == "missing-zip-row":
            return ([{"entry_id": "image-only", "text": "logo.jpg"}], None, "click")
        return (
            [{"entry_id": "valid-row", "text": "B0CQLN5GNL_96_CustomizedInfo.zip"}],
            {"entry_id": "valid-row", "text": "B0CQLN5GNL_96_CustomizedInfo.zip"},
            "click",
        )

    async def fake_click(page, entry_id):
        return _Download("B0CQLN5GNL_96_CustomizedInfo.zip", "164379175880962")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720497261493880",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"164379175880961", "164379175880962"},
        )
    )

    assert opened == ["missing-zip-row", "valid-row"]
    assert bundle.status == custom_zip_downloader.CUSTOM_ZIP_NOT_FOUND
    assert bundle.error == "定制 zip 缺少 Amazon OrderItemId：164379175880961"
    assert any("rowid=row_95" in warning and "file=logo.jpg" in warning for warning in bundle.warnings)


def test_download_bundle_tries_next_zip_candidate_when_first_order_item_is_duplicate(monkeypatch, tmp_path):
    order_no = "114-6396416-4441061"
    order_dir = tmp_path / order_no
    order_dir.mkdir()
    with zipfile.ZipFile(order_dir / "B0CQLN5GNL_33_CustomizedInfo.zip", "w") as archive:
        archive.writestr("present.json", json.dumps({"orderItemId": "164379175881041"}))

    targets = [
        {
            "row_index": 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": "row_95",
            "attachment_label": "logo.jpg",
            "trigger_id": "row-95",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        }
    ]
    opened: list[str] = []
    clicked: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        entries = [
            {
                "entry_id": "duplicate-entry",
                "text": "B0CQLN5GNL_33_CustomizedInfo.zip",
                "top": 200,
                "index": 1,
            },
            {
                "entry_id": "missing-entry",
                "text": "B0CQLN5GNL_26_CustomizedInfo.zip",
                "top": 100,
                "index": 0,
            },
        ]
        return (entries, entries[0], "click")

    async def fake_click(page, entry_id):
        clicked.append(entry_id)
        if entry_id == "duplicate-entry":
            return _Download("B0CQLN5GNL_33_CustomizedInfo.zip", "164379175881041")
        return _Download("B0CQLN5GNL_26_CustomizedInfo.zip", "164379175880961")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720497261493880",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids={"164379175881041", "164379175880961"},
        )
    )

    assert bundle.status == "ok"
    assert opened == ["row-95", "row-95"]
    assert clicked == ["duplicate-entry", "missing-entry"]
    assert {item.order_item_id for item in bundle.zip_files} == {"164379175881041", "164379175880961"}
    assert any("duplicate_custom_zip_order_item_skipped:164379175881041" in warning for warning in bundle.warnings)


def test_download_bundle_downloads_multiple_expected_zips_from_one_product_row(monkeypatch, tmp_path):
    """同一商品行的首个 ZIP 成功后，仍应继续下载其余预期订单行。"""

    order_no = "702-8772842-4295444"
    order_item_ids = ["166310972513321", "166676476984761"]
    target = {
        "row_index": 1,
        "asin": "B0DRCWYC98",
        "sku": "table_runners",
        "row_dom_id": "row_7028772842",
        "attachment_label": "custom-artwork.jpg",
        "trigger_id": "same-product-row",
        "trigger_text": "共4",
        "row_match_reason": "strict-product-table-row",
    }
    entries = [
        {
            "entry_id": "zip-first",
            "text": "B0DRCWYC98_01_CustomizedInfo.zip",
            "top": 200,
            "index": 1,
        },
        {
            "entry_id": "zip-second",
            "text": "B0DRCWYC98_02_CustomizedInfo.zip",
            "top": 100,
            "index": 0,
        },
    ]
    opened: list[str] = []
    clicked: list[str] = []

    async def fake_find_targets(page, system_order_no):
        assert system_order_no == "103731347446512771"
        return [target]

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        return (entries, entries[0], "click")

    async def fake_click(page, entry_id):
        clicked.append(entry_id)
        if entry_id == "zip-first":
            return _Download("B0DRCWYC98_01_CustomizedInfo.zip", order_item_ids[0])
        return _Download("B0DRCWYC98_02_CustomizedInfo.zip", order_item_ids[1])

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103731347446512771",
            staging_root=tmp_path,
            expected_zip_count=2,
            expected_order_item_ids=set(order_item_ids),
        )
    )

    assert bundle.status == "ok"
    assert opened == ["same-product-row", "same-product-row"]
    assert clicked == ["zip-first", "zip-second"]
    assert {item.order_item_id for item in bundle.zip_files} == set(order_item_ids)


def test_download_bundle_covers_same_asin_rows_when_missing_id_is_second_candidate(monkeypatch, tmp_path):
    order_no = "114-6396416-4441061"
    order_item_ids = [f"1643791758809{i:02d}" for i in range(11)]
    missing_order_item_id = "164379175880961"
    targets = [
        {
            "row_index": index + 1,
            "asin": "B0CQLN5GNL",
            "sku": "Car-Magnet-12x24in-2pcs",
            "row_dom_id": f"row_{86 + index}",
            "attachment_label": f"artwork-{index + 1}.jpg",
            "trigger_id": f"trigger-{index + 1}",
            "trigger_text": "共4",
            "row_match_reason": "strict-product-table-row",
        }
        for index in range(12)
    ]
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        index = int(trigger_id.removeprefix("trigger-"))
        if index == 12:
            entries = [
                {
                    "entry_id": "trigger-12-duplicate",
                    "text": "B0CQLN5GNL_11_CustomizedInfo.zip",
                    "top": 200,
                    "index": 1,
                },
                {
                    "entry_id": "trigger-12-missing",
                    "text": "B0CQLN5GNL_26_CustomizedInfo.zip",
                    "top": 100,
                    "index": 0,
                },
            ]
            return (entries, entries[0], "click")
        filename = f"B0CQLN5GNL_{index:02d}_CustomizedInfo.zip"
        return (
            [{"entry_id": trigger_id, "text": filename, "top": 100, "index": 0}],
            {"entry_id": trigger_id, "text": filename, "top": 100, "index": 0},
            "click",
        )

    async def fake_click(page, entry_id):
        if entry_id == "trigger-12-duplicate":
            return _Download("B0CQLN5GNL_11_CustomizedInfo.zip", order_item_ids[-1])
        if entry_id == "trigger-12-missing":
            return _Download("B0CQLN5GNL_26_CustomizedInfo.zip", missing_order_item_id)
        index = int(entry_id.removeprefix("trigger-")) - 1
        return _Download(f"B0CQLN5GNL_{index + 1:02d}_CustomizedInfo.zip", order_item_ids[index])

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103720497261493880",
            staging_root=tmp_path,
            expected_zip_count=12,
            expected_order_item_ids={*order_item_ids, missing_order_item_id},
        )
    )

    assert bundle.status == "ok"
    assert opened.count("trigger-12") == 2
    assert {item.order_item_id for item in bundle.zip_files} == {*order_item_ids, missing_order_item_id}


def test_download_bundle_refreshes_target_before_opening_popover(monkeypatch, tmp_path):
    """下载前应重新定位同一商品行，避免使用旧 DOM 标记。"""
    order_no = "111-8112209-3174649"
    calls = 0
    opened: list[str] = []

    async def fake_find_targets(page, system_order_no):
        nonlocal calls
        calls += 1
        trigger_id = "stale-trigger" if calls == 1 else "fresh-trigger"
        return [
            {
                "row_index": 1,
                "asin": "B0DZ2W2QWK",
                "sku": "canopytents",
                "target_key": "B0DZ2W2QWK:canopytents:1",
                "trigger_id": trigger_id,
                "trigger_text": "共4",
            }
        ]

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        return (
            [{"entry_id": "entry-fresh", "text": "B0DZ2W2QWK_15_CustomizedInfo.zip"}],
            {"entry_id": "entry-fresh", "text": "B0DZ2W2QWK_15_CustomizedInfo.zip"},
            "hover",
        )

    async def fake_click(page, entry_id):
        return _Download("B0DZ2W2QWK_15_CustomizedInfo.zip", "164173871685321")

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103719401767966430",
            staging_root=tmp_path,
            expected_zip_count=1,
            expected_order_item_ids={"164173871685321"},
        )
    )

    assert bundle.status == "ok"
    assert calls >= 2
    assert opened == ["fresh-trigger"]


def test_download_bundle_reports_safe_click_skip_when_zip_popover_cannot_open(monkeypatch, tmp_path):
    """附件入口被遮挡时，应返回安全点击诊断，而不是继续误点页面。"""
    order_no = "114-5700989-5753008"
    targets = [
        {
            "row_index": 1,
            "asin": "B0CNVLXTWB",
            "sku": "Car-Magnet-18x24in-2pcs",
            "target_key": "B0CNVLXTWB:Car-Magnet-18x24in-2pcs:1",
            "trigger_id": "covered-trigger",
            "trigger_text": "共4",
        }
    ]

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        return [], None, "safe_click_skipped:附件入口点击前命中检测失败：covered_by_other_element"

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103719941890547475",
            staging_root=tmp_path,
            expected_zip_count=1,
        )
    )

    assert bundle.status == custom_zip_downloader.CUSTOM_ZIP_NOT_FOUND
    assert "safe_click_skipped" in (bundle.error or "")
    assert "附件入口点击前命中检测失败" in (bundle.error or "")


def test_download_bundle_recovers_completed_download_from_downloads_after_timeout(monkeypatch, tmp_path):
    """浏览器已完成下载但 download 事件超时时，应按 OrderItemId 从 Downloads 恢复到 staging。"""
    order_no = "111-1197078-1671469"
    expected_order_item_id = "164596991889281"
    downloads_dir = tmp_path / "Downloads"
    _write_downloads_zip(downloads_dir / "B0D5134SJ3_35_CustomizedInfo.zip", expected_order_item_id)

    targets = [
        {
            "row_index": 1,
            "asin": "B0D5134SJ3",
            "sku": "canopytents",
            "target_key": "B0D5134SJ3:canopytents:1",
            "trigger_id": "zip-trigger",
            "trigger_text": "共11",
        }
    ]

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        return (
            [{"entry_id": "zip-entry", "text": "B0D5134SJ3_35_CustomizedInfo.zip"}],
            {"entry_id": "zip-entry", "text": "B0D5134SJ3_35_CustomizedInfo.zip"},
            "click",
        )

    async def fake_click(page, entry_id):
        raise TimeoutError('Timeout 20000ms exceeded while waiting for event "download"')

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)
    monkeypatch.setattr(custom_zip_downloader, "_default_downloads_dir", lambda: downloads_dir)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103721707403922081",
            staging_root=tmp_path / "staging",
            expected_zip_count=1,
            expected_order_item_ids={expected_order_item_id},
        )
    )

    assert bundle.status == "ok"
    assert [item.order_item_id for item in bundle.zip_files] == [expected_order_item_id]
    assert Path(bundle.zip_files[0].zip_path).exists()
    assert Path(bundle.zip_files[0].zip_path).parent == tmp_path / "staging" / order_no
    assert any("downloads_custom_zip_recovered" in warning for warning in bundle.warnings)


def test_download_bundle_does_not_recover_downloads_zip_with_wrong_order_item_id(monkeypatch, tmp_path):
    """Downloads 中有 zip 但 OrderItemId 不匹配时，仍应报告缺失而不是按文件名猜测。"""
    order_no = "111-1197078-1671469"
    expected_order_item_id = "164596991889281"
    downloads_dir = tmp_path / "Downloads"
    _write_downloads_zip(downloads_dir / "B0D5134SJ3_35_CustomizedInfo.zip", "164596991889999")

    targets = [
        {
            "row_index": 1,
            "asin": "B0D5134SJ3",
            "sku": "canopytents",
            "target_key": "B0D5134SJ3:canopytents:1",
            "trigger_id": "zip-trigger",
            "trigger_text": "共11",
        }
    ]

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        return (
            [{"entry_id": "zip-entry", "text": "B0D5134SJ3_35_CustomizedInfo.zip"}],
            {"entry_id": "zip-entry", "text": "B0D5134SJ3_35_CustomizedInfo.zip"},
            "click",
        )

    async def fake_click(page, entry_id):
        raise TimeoutError('Timeout 20000ms exceeded while waiting for event "download"')

    monkeypatch.setattr(custom_zip_downloader, "_find_product_zip_targets", fake_find_targets)
    monkeypatch.setattr(custom_zip_downloader, "_open_attachment_popover", fake_open_popover)
    monkeypatch.setattr(custom_zip_downloader, "_click_entry_and_wait_for_download", fake_click)
    monkeypatch.setattr(custom_zip_downloader, "_default_downloads_dir", lambda: downloads_dir)

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103721707403922081",
            staging_root=tmp_path / "staging",
            expected_zip_count=1,
            expected_order_item_ids={expected_order_item_id},
        )
    )

    assert bundle.status == custom_zip_downloader.CUSTOM_ZIP_NOT_FOUND
    assert bundle.error == f"定制 zip 缺少 Amazon OrderItemId：{expected_order_item_id}"
    assert not list((tmp_path / "staging" / order_no).glob("*.zip"))


def test_browser_download_stops_after_three_consecutive_download_failures(
    monkeypatch,
    tmp_path,
):
    order_no = "111-7711296-3832208"
    expected_ids = {f"165896664615{index:03d}" for index in range(20)}
    targets = [
        {
            "row_index": index + 1,
            "asin": "B0DRCWYC98",
            "sku": "Car-Magent-3.5x12in-1pcs",
            "target_key": f"row-{index + 1}",
            "trigger_id": f"trigger-{index + 1}",
            "trigger_text": f"共2 row {index + 1}",
        }
        for index in range(20)
    ]
    clicked: list[str] = []
    empty_downloads = tmp_path / "Downloads"
    empty_downloads.mkdir()

    async def fake_find_targets(_page, _system_order_no):
        return targets

    async def fake_open_popover(_page, trigger_id):
        entry = {
            "entry_id": f"entry-{trigger_id}",
            "text": "B0DRCWYC98_CustomizedInfo.zip",
        }
        return [entry], entry, "click"

    async def fake_click(_page, entry_id):
        clicked.append(entry_id)
        raise TimeoutError(
            'Timeout 10000ms exceeded while waiting for event "download"'
        )

    monkeypatch.setattr(
        custom_zip_downloader,
        "_find_product_zip_targets",
        fake_find_targets,
    )
    monkeypatch.setattr(
        custom_zip_downloader,
        "_open_attachment_popover",
        fake_open_popover,
    )
    monkeypatch.setattr(
        custom_zip_downloader,
        "_click_entry_and_wait_for_download",
        fake_click,
    )
    monkeypatch.setattr(
        custom_zip_downloader,
        "_default_downloads_dir",
        lambda: empty_downloads,
    )

    bundle = asyncio.run(
        custom_zip_downloader.download_order_custom_zip_bundle(
            SimpleNamespace(),
            platform_order_no=order_no,
            system_order_no="103727964846455762",
            staging_root=tmp_path / "staging",
            expected_zip_count=20,
            expected_order_item_ids=expected_ids,
        )
    )

    assert len(clicked) == 3
    assert bundle.status == custom_zip_downloader.CUSTOM_ZIP_DOWNLOAD_ERROR
    assert "网页附件连续下载失败 3 次，已提前停止" in (bundle.error or "")
    assert "仍缺少 Amazon OrderItemId" in (bundle.error or "")
    assert any(
        warning.startswith("browser_custom_zip_download_circuit_open:3:")
        for warning in bundle.warnings
    )
