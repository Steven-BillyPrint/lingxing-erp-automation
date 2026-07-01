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
        if suggested_filename is not None:
            self.suggested_filename = suggested_filename
        self.order_item_id = order_item_id

    async def save_as(self, path: str) -> None:
        if not self.order_item_id:
            Path(path).write_bytes(b"zip")
            return
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                f"{self.order_item_id}.json",
                json.dumps({"orderItemId": self.order_item_id, "asin": "B0DRCWYC98"}),
            )


def test_download_bundle_stops_after_expected_zip_count(monkeypatch, tmp_path):
    targets = [
        {"row_index": 1, "asin": "B0CRKYV7C9", "trigger_id": "duplicate", "trigger_text": "共2", "trigger_is_interactable": True},
        {"row_index": 2, "asin": "B0CRKYV7C9", "trigger_id": "real", "trigger_text": "共2", "trigger_is_interactable": True},
        {"row_index": 3, "asin": "B0CRKYV7C9", "trigger_id": "extra", "trigger_text": "共2", "trigger_is_interactable": True},
    ]
    opened: list[str] = []
    clicked: list[str] = []

    async def fake_find_targets(page, system_order_no):
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        if trigger_id == "duplicate":
            raise TimeoutError("Locator.scroll_into_view_if_needed: Timeout 1200ms exceeded.")
        return ([{"entry_id": f"entry-{trigger_id}", "text": _Download.suggested_filename}], {"entry_id": f"entry-{trigger_id}", "text": _Download.suggested_filename}, "hover")

    async def fake_click(page, entry_id):
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
        return targets

    async def fake_open_popover(page, trigger_id):
        opened.append(trigger_id)
        if trigger_id == "duplicate":
            raise TimeoutError("Locator.click: Element is not visible")
        return (
            [{"entry_id": "entry-missing", "text": "B0DRCWXR7S_30_CustomizedInfo.zip"}],
            {"entry_id": "entry-missing", "text": "B0DRCWXR7S_30_CustomizedInfo.zip"},
            "hover",
        )

    async def fake_click(page, entry_id):
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
        return targets

    async def fake_open_popover(page, trigger_id):
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
        return targets

    async def fake_open_popover(page, trigger_id):
        entry_id = f"entry-{trigger_id}"
        return (
            [{"entry_id": entry_id, "text": "B0DRCWYC98_CustomizedInfo.zip"}],
            {"entry_id": entry_id, "text": "B0DRCWYC98_CustomizedInfo.zip"},
            "hover",
        )

    async def fake_click(page, entry_id):
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
