from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import CustomZipFile, CustomizationJsonInfo, FolderNameShortenResult, OrderCustomZipBundle
from .custom_attachment_downloader import unique_zip_target_path
from .customization_json_parser import parse_customization_json_info

CUSTOM_ZIP_JSON_NOT_FOUND = "custom_zip_json_not_found"
CUSTOM_ZIP_JSON_PARSE_ERROR = "custom_zip_json_parse_error"
CUSTOM_JSON_MISSING_ORDER_ITEM_ID = "custom_json_missing_order_item_id"
CUSTOM_ZIP_MOVE_ERROR = "custom_zip_move_error"
CUSTOM_ZIP_MOVED = "custom_zip_moved"
CUSTOM_ZIP_STAGING_CLEANED = "custom_zip_staging_cleaned"
CUSTOM_ZIP_STAGING_CLEANUP_ERROR = "custom_zip_staging_cleanup_error"

FULL_FOLDER_NAME_TXT = "完整文件夹名.txt"


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """安全解压 zip，避免压缩包内路径跳出 staging 目录。"""

    extract_dir.mkdir(parents=True, exist_ok=True)
    root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (extract_dir / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"zip member path escapes staging directory: {member.filename}")
            archive.extract(member, extract_dir)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _choose_json_file(json_paths: list[Path], *, zip_file: CustomZipFile) -> Path | None:
    if not json_paths:
        return None
    if zip_file.order_item_id:
        expected_name = f"{zip_file.order_item_id}.json"
        for path in json_paths:
            if path.name == expected_name:
                return path
    if len(json_paths) == 1:
        return json_paths[0]
    for path in json_paths:
        try:
            data = _read_json(path)
        except Exception:
            continue
        if zip_file.platform_order_no and str(data.get("orderId") or "") == zip_file.platform_order_no:
            if zip_file.asin and str(data.get("asin") or "").upper() != str(zip_file.asin).upper():
                continue
            return path
    return None


def parse_custom_zip_file(zip_file: CustomZipFile, staging_dir: str | Path) -> tuple[CustomZipFile, CustomizationJsonInfo | None]:
    """解压单个 zip 并读取其中 JSON。

    zip 内 JSON 只用于解析定制化信息和生成文件夹名；
    最终订单文件夹仍然只保存原始 zip，避免把解压文件散落到生产目录。
    """

    zip_path = Path(zip_file.zip_path)
    extract_dir = Path(staging_dir) / zip_path.stem
    try:
        _safe_extract_zip(zip_path, extract_dir)
        json_paths = sorted(extract_dir.rglob("*.json"), key=lambda item: str(item))
        chosen = _choose_json_file(json_paths, zip_file=zip_file)
        if chosen is None:
            return replace(zip_file, status=CUSTOM_ZIP_JSON_NOT_FOUND, error="zip 内没有可匹配的 JSON 文件。"), None
        data = _read_json(chosen)
        info = parse_customization_json_info(data, raw_json_path=str(chosen), source_zip_path=str(zip_path))
        if not info.order_item_id:
            return replace(
                zip_file,
                status=CUSTOM_JSON_MISSING_ORDER_ITEM_ID,
                json_filename=chosen.name,
                error="zip JSON 缺少 orderItemId。",
            ), None
        return replace(
            zip_file,
            status="custom_zip_json_parsed",
            order_item_id=info.order_item_id,
            json_filename=chosen.name,
        ), info
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError) as exc:
        return replace(zip_file, status=CUSTOM_ZIP_JSON_PARSE_ERROR, error=str(exc)[:800]), None


def parse_order_custom_zip_bundle(bundle: OrderCustomZipBundle, staging_dir: str | Path) -> OrderCustomZipBundle:
    """解析一个订单已下载的所有 zip。任意一行失败，整单都不能进入 processed。"""

    parsed_files: list[CustomZipFile] = []
    infos: list[CustomizationJsonInfo] = []
    for zip_file in bundle.zip_files:
        parsed_file, info = parse_custom_zip_file(zip_file, staging_dir)
        parsed_files.append(parsed_file)
        if info is not None:
            infos.append(info)
    failed = next((item for item in parsed_files if item.status != "custom_zip_json_parsed"), None)
    status = failed.status if failed else "ok"
    error = failed.error if failed else None
    warnings = list(bundle.warnings)
    for info in infos:
        warnings.extend(info.warnings)
    return OrderCustomZipBundle(
        platform_order_no=bundle.platform_order_no,
        zip_files=parsed_files,
        customization_items=infos,
        status=status,
        error=error,
        warnings=warnings,
    )


def write_full_folder_name_txt(
    folder_path: str | Path,
    shorten_result: FolderNameShortenResult,
) -> str:
    target = Path(folder_path) / FULL_FOLDER_NAME_TXT
    removed = "\n".join(shorten_result.removed_components) if shorten_result.removed_components else "-"
    text = (
        "完整文件夹名：\n"
        f"{shorten_result.full_folder_name}\n\n"
        "实际文件夹名：\n"
        f"{shorten_result.safe_folder_name}\n\n"
        "是否缩短：\n"
        f"{'是' if shorten_result.was_shortened else '否'}\n\n"
        "被删除的片段：\n"
        f"{removed}\n"
    )
    target.write_text(text, encoding="utf-8")
    return str(target)


def copy_custom_zip_files_to_folder(zip_files: list[CustomZipFile], folder_path: str | Path) -> tuple[str, list[str], str | None]:
    """把 staging 中的原始 zip 复制到最终订单文件夹。

    最终目录只保存原始 zip 和完整文件夹名说明，不复制 staging 中解压出的 JSON/图片/PDF。
    """

    copied: list[str] = []
    try:
        for zip_file in zip_files:
            source = Path(zip_file.zip_path)
            if not source.exists():
                return CUSTOM_ZIP_MOVE_ERROR, copied, f"zip 文件不存在：{source}"
            target = unique_zip_target_path(folder_path, source.name)
            shutil.copy2(source, target)
            copied.append(str(target))
    except OSError as exc:
        return CUSTOM_ZIP_MOVE_ERROR, copied, str(exc)
    return CUSTOM_ZIP_MOVED, copied, None


def cleanup_custom_zip_staging_dir(staging_dir: str | Path) -> tuple[str, str | None]:
    """成功复制到最终文件夹后清理订单 staging 目录；失败订单保留 staging 方便排查和重试。"""

    target = Path(staging_dir)
    if not target.exists():
        return CUSTOM_ZIP_STAGING_CLEANED, None
    try:
        resolved = target.resolve()
        if resolved.name == "custom_zip_staging":
            return CUSTOM_ZIP_STAGING_CLEANUP_ERROR, f"拒绝删除 staging 根目录：{resolved}"
        if "custom_zip_staging" not in {path.name for path in [resolved, *resolved.parents]}:
            return CUSTOM_ZIP_STAGING_CLEANUP_ERROR, f"拒绝删除非 custom_zip_staging 路径：{resolved}"
        shutil.rmtree(resolved)
    except OSError as exc:
        return CUSTOM_ZIP_STAGING_CLEANUP_ERROR, str(exc)
    return CUSTOM_ZIP_STAGING_CLEANED, None
