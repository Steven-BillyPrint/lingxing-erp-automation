from __future__ import annotations

from lingxing_automation.cli import build_parser
from lingxing_automation.models import BatchOrderItem, CustomZipDownloadResult
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
    return BatchOrderItem(
        system_order_no="103709545124988424",
        platform_order_no="113-0987796-6853040",
        row_text="",
        asin="B0D5134SJ3",
        sku="canopytents",
    )


def test_sanitize_zip_filename_keeps_zip_suffix_and_replaces_windows_invalid_chars():
    assert sanitize_zip_filename('bad<>:"/\\|?* name.zip') == "bad_ name.zip"
    assert sanitize_zip_filename("") == "customization_images.zip"
    assert sanitize_zip_filename("B0D5134SJ3_19_Customized") == "B0D5134SJ3_19_Customized.zip"


def test_unique_zip_target_path_stays_inside_order_folder_and_does_not_overwrite(tmp_path):
    existing = tmp_path / "B0D5134SJ3_19_Customized.zip"
    existing.write_bytes(b"old")

    target = unique_zip_target_path(tmp_path, "B0D5134SJ3_19_Customized.zip")

    assert target == tmp_path / "B0D5134SJ3_19_Customized (2).zip"
    assert target.parent == tmp_path


def test_build_item_match_payload_strips_list_quantity_suffix_from_sku():
    item = _item()
    item.sku = "canopytents 共1"

    assert normalize_item_match_text("canopytents 共1") == "canopytents"
    assert build_item_match_payload(item)["sku"] == "canopytents"


def test_choose_zip_entry_prefers_explicit_zip_even_if_not_bottom():
    entries = [
        {"entry_id": "png", "text": "logo.png", "top": 100, "index": 0},
        {"entry_id": "zip", "text": "B0D5134SJ3_19_Customized.zip", "top": 140, "index": 1},
        {"entry_id": "pdf", "text": "proof.pdf", "top": 180, "index": 2},
    ]

    chosen = choose_zip_entry_from_popover_entries(entries)

    assert chosen is not None
    assert chosen["entry_id"] == "zip"


def test_choose_zip_entry_uses_bottom_non_media_when_suffix_is_hidden():
    entries = [
        {"entry_id": "img1", "text": "8047f539-8375-26bc-2955-logo.png", "top": 120, "index": 0},
        {"entry_id": "img2", "text": "e4ab9b01-09bc-1637-e788-proof.pdf", "top": 150, "index": 1},
        {"entry_id": "zip", "text": "B0CRRGTPFH_90_Customize...", "top": 190, "index": 2},
    ]

    chosen = choose_zip_entry_from_popover_entries(entries)

    assert chosen is not None
    assert chosen["entry_id"] == "zip"


def test_filter_interactable_zip_targets_keeps_offscreen_product_rows():
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
    args = build_parser().parse_args(["--batch", "--no-download-custom-zip"])

    assert args.no_download_custom_zip is True
