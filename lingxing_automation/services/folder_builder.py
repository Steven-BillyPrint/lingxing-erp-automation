from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..models import BatchOrderItem, ContactInfo, FolderBuildResult, FolderNameShortenResult, OrderFolderLine, OrderFolderTask
from ..rule_matching import lookup_with_plural_variants
from ..products.car_magnets import (
    CAR_MAGNET_LEGACY_PROOF_INSTRUCTION,
    CAR_MAGNET_PROOF_OPTIONS,
    CAR_MAGNET_PROOF_TITLE,
    CAR_MAGNET_SAME_DESIGN_OPTIONS,
    CAR_MAGNET_SAME_DESIGN_PARENT_ASIN,
    CAR_MAGNET_SAME_DESIGN_TITLE,
    PRODUCT_TYPE_CAR_MAGNET,
    find_car_magnet_parent_asin,
    get_car_magnet_fixed_size,
    get_car_magnet_unit_quantity,
    is_car_magnet_asin,
    normalize_car_magnet_proof_value,
    normalize_car_magnet_same_design_value,
    normalize_car_magnet_size_value,
)
from ..products.feather_flags import (
    FEATHER_FLAG_TITLE_ALIASES,
    PRODUCT_TYPE_FEATHER_FLAGS,
    find_feather_flag_parent_asin,
    get_feather_flag_option_rules,
    get_feather_flag_pair_by_title_aliases,
    get_feather_flag_printing_side_rules,
    get_feather_flag_product_name,
    get_feather_flag_size,
    is_feather_flag_asin,
    normalize_feather_flag_option_value,
)
from ..products.pop_up_displays import (
    POP_UP_DISPLAY_PARENT_WITH_FABRIC_PANEL_QUANTITY,
    POP_UP_DISPLAY_PARENT_WITH_SIDE_PANELS,
    POP_UP_DISPLAY_PARENTS_WITH_LED,
    POP_UP_DISPLAY_PARENTS_WITH_PRINTING_SIDES,
    POP_UP_DISPLAY_TITLE_ALIASES,
    PRODUCT_TYPE_POP_UP_DISPLAYS,
    find_pop_up_display_parent_asin,
    get_pop_up_display_option_rules,
    get_pop_up_display_pair_by_title_aliases,
    get_pop_up_display_product_name,
    get_pop_up_display_size,
    get_pop_up_display_stand_type,
    is_pop_up_display_asin,
    normalize_pop_up_display_option_value,
)
from ..products.posters import (
    POSTER_PROOF_TITLE_ALIASES,
    PRODUCT_TYPE_POSTERS,
    find_poster_parent_asin,
    get_poster_fragment,
    get_poster_pair_by_title_aliases,
    is_poster_asin,
    lookup_poster_proof_option,
)
from ..products.roll_up_banners import (
    PRODUCT_TYPE_ROLL_UP_BANNERS,
    ROLL_UP_BANNER_PRINTING_PROCESS_OPTIONS,
    ROLL_UP_BANNER_PRINTING_PROCESS_TITLE,
    ROLL_UP_BANNER_PROOF_OPTIONS,
    ROLL_UP_BANNER_PROOF_TITLE,
    find_roll_up_banner_parent_asin,
    get_roll_up_banner_fragment,
    is_roll_up_banner_asin,
    normalize_roll_up_banner_option_value,
)
from ..products.tablecloths import (
    PRODUCT_TYPE_TABLECLOTHS,
    TABLECLOTH_TITLE_ALIASES,
    find_tablecloth_parent_asin,
    get_tablecloth_option_rules,
    get_tablecloth_pair_by_title_aliases,
    get_tablecloth_product_name,
    get_tablecloth_size,
    is_tablecloth_asin,
    normalize_tablecloth_option_value,
)
from ..products.table_runners import (
    PRODUCT_TYPE_TABLE_RUNNERS,
    TABLE_RUNNER_PRODUCT_NAME,
    TABLE_RUNNER_TITLE_ALIASES,
    find_table_runner_parent_asin,
    get_table_runner_option_rules,
    get_table_runner_pair_by_title_aliases,
    get_table_runner_size,
    is_table_runner_asin,
    normalize_table_runner_option_value,
)
from ..products.tents import find_tent_parent_asin, get_tent_top_size, get_wall_only_asin_kind, is_default_expedited_tent_asin
from ..products.vinyl_banners import (
    PRODUCT_TYPE_VINYL_BANNERS,
    VINYL_BANNER_TITLE_ALIASES,
    find_vinyl_banner_parent_asin,
    get_pair_by_title_aliases,
    get_vinyl_banner_default_printed_sides,
    get_vinyl_banner_option_rules,
    get_vinyl_banner_product_name,
    get_vinyl_banner_size,
    is_vinyl_banner_asin,
    normalize_option_value,
)
from ..products.x_stands import (
    PRODUCT_TYPE_X_STANDS,
    X_STAND_PRINTING_PROCESS_OPTIONS,
    X_STAND_PRINTING_PROCESS_TITLE,
    X_STAND_PROOF_OPTIONS,
    X_STAND_PROOF_TITLE,
    find_x_stand_parent_asin,
    get_x_stand_fragment,
    is_x_stand_asin,
    normalize_x_stand_option_value,
)
from .customization_parser import parse_customization_pairs
from .order_folder_rules import (
    DEFAULT_ORDER_FOLDER_RULES,
    FRAME_TITLES,
    TABLE_CLOTH_TITLES,
    TITLE_DOUBLE_SIDE,
    TITLE_FABRIC,
    TITLE_FLAG,
    TITLE_CAR_MAGNET_CORNER,
    TITLE_CAR_MAGNET_PROOF,
    TITLE_CAR_MAGNET_SAME_DESIGN,
    TITLE_CAR_MAGNET_SHAPE,
    TITLE_CAR_MAGNET_SIZE,
    TITLE_CAR_MAGNET_SURFACE,
    TITLE_CAR_MAGNET_THICKNESS,
    TITLE_CANOPY_FRAME_SIZE,
    TITLE_RAIL_ADAPTER,
    TITLE_FULL_WALL_ATTACHMENT,
    TITLE_FULL_WALL_SIZE,
    TITLE_ROLLER_BAG,
    TITLE_ROPE_STAKE,
    TITLE_SANDBAGS,
    TITLE_SANDBAGS_6PCS,
    TITLE_SIDE_WALL,
    TITLE_TENT_SAME_DESIGN,
    OrderFolderRules,
    WallRuleComponent,
    is_empty_option,
    normalize_rule_key,
)
from .tent_sku_planner import parse_destination_region

DEFAULT_FOLDER_ROOT = r"Z:\Amazon每日订单汇总"
FOLDER_NAME_MAX_LENGTH = 180
# 部分 Z 盘/网络盘会按字节限制单个目录名；中文片段很多时即使字符数未超 180，
# 也可能因为 UTF-8 字节数过长导致 mkdir 失败。因此在保留字符长度限制外，
# 再加一层保守的字节预算，超出时仍按完整 “+” 片段删除，不硬截断业务片段。
FOLDER_NAME_MAX_UTF8_BYTES = 240
FOLDER_EXISTING_PLATFORM_ORDER = "folder_existing_platform_order"
SUCCESS_FOLDER_STATUSES = {"folder_created", "folder_exists", "folder_preview", FOLDER_EXISTING_PLATFORM_ORDER}
WINDOWS_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_INVALID_FILENAME_CHAR_MAP = {
    "<": " - ",
    ">": " - ",
    ":": " - ",
    '"': " - ",
    "/": " - ",
    "\\": " - ",
    "|": " - ",
    "?": " - ",
    "*": " - ",
}
# 双面打印半高侧墙的最大数量（产品限制）
MAX_DOUBLE_SIDE_HALF_WALLS = 2


class FolderRuleMissingError(ValueError):
    def __init__(self, title: str, value: str):
        """初始化文件夹规则缺失错误的运行状态。"""
        super().__init__(f"缺少文件夹规则：{title} = {value}")
        self.title = title
        self.value = value
        self.missing_rule_line: str | None = None


class MissingSizeRuleError(ValueError):
    pass


class VinylBannerFolderError(ValueError):
    """喷绘文件夹生成错误。

    喷绘规则独立于帐篷/汽车磁贴，状态值需要保留产品名前缀，方便批量日志快速定位。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化vinyl banner 文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class PosterFolderError(ValueError):
    """海报文件夹生成错误。

    海报只有 Proof 一个可选定制项，尺寸规格由 ASIN 决定；单独状态能让日志直接定位到海报规则。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化poster 文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class TableRunnerFolderError(ValueError):
    """桌旗文件夹生成错误。

    桌旗规则独立于桌布和喷绘，单独状态方便批量日志直接定位到桌旗规则缺失。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化表格 runner 文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class PopUpDisplayFolderError(ValueError):
    """拉网展架文件夹生成错误。

    拉网展架的尺寸、带/不带支架和选项规则都来自独立 PDF 文本节点；
    使用单独状态能让批量日志准确定位缺失的是哪一类规则。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化弹出 up display 文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class RollUpBannerFolderError(ValueError):
    """易拉宝文件夹生成错误。

    易拉宝的品名片段只由子 ASIN 决定，Proof 出现时需要严格匹配；
    单独错误状态可以让巡检日志直接看出是易拉宝规则缺失或 ASIN 映射缺失。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化roll up banner 文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class XStandFolderError(ValueError):
    """X展架文件夹生成错误。

    X展架独立于易拉宝和拉网展架，尺寸片段只由子 ASIN 决定；单独状态方便巡检日志定位。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化x 展架文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


class FeatherFlagFolderError(ValueError):
    """刀旗文件夹生成错误。

    刀旗的规则来自 flag.pdf 文本层，单双面还会嵌入品名片段；
    独立状态方便在批量日志里直接定位缺少的是尺寸、Printing Side 还是配件规则。
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        title: str | None = None,
        value: str | None = None,
        parent_asin: str | None = None,
    ):
        """初始化feather 旗帜文件夹错误的运行状态。"""
        super().__init__(message)
        self.status = status
        self.title = title
        self.value = value
        self.parent_asin = parent_asin
        self.missing_rule_line: str | None = None


def parse_folder_date_override(value: str | date | datetime | None) -> date | None:
    """解析人工传入的文件夹日期覆盖值。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"--folder-date 格式必须是 YYYY-MM-DD：{value}") from exc


def resolve_folder_date(payment_time: str | datetime | date | None, override_date: str | date | datetime | None = None) -> date:
    """
    解析订单文件夹日期。

    业务要求：文件夹归档日期必须以订单付款时间为准。
    --folder-date 仅用于补单或调试时人工覆盖，正常批量运行不能使用脚本当天日期。
    """
    override = parse_folder_date_override(override_date)
    if override:
        return override
    if payment_time is None or str(payment_time).strip() == "":
        raise ValueError("missing_payment_time")
    if isinstance(payment_time, datetime):
        return payment_time.date()
    if isinstance(payment_time, date):
        return payment_time

    text = str(payment_time).strip()
    match = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", text)
    if not match:
        # 不能 fallback 到今天日期，否则会把订单归档到错误日期且后续很难排查。
        raise ValueError("invalid_payment_time")
    normalized = match.group(0).replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            continue
    raise ValueError("invalid_payment_time")


def build_daily_folder(root: str | Path, folder_date: date) -> Path:
    """根据订单付款日期生成每日目录路径。"""
    return Path(root) / f"{folder_date.year}" / f"{folder_date.month}月" / f"{folder_date:%m%d}"


def build_month_folder(root: str | Path, folder_date: date) -> Path:
    """根据付款日期生成当月目录路径，用于跨日查重同一平台单号。"""
    return Path(root) / f"{folder_date.year}" / f"{folder_date.month}月"


def sanitize_folder_name(folder_name: str, replacement: str = "_", max_length: int | None = FOLDER_NAME_MAX_LENGTH) -> str:
    """清洗 Windows 文件夹名非法字符，但保留 + 分隔符。"""
    # Windows 文件夹不能包含 <>:"/\|?* 和控制字符；+ 是业务分隔符，必须保留。
    # 客户名里的 / 往往表示机构/部门关系，直接替换成下划线会改变阅读含义；
    # 因此普通非法符号改为 “ - ” 连接，只有控制字符才退回 replacement。
    def replace_invalid_char(match: re.Match[str]) -> str:
        """把 Windows 文件名非法字符替换为安全片段。"""
        char = match.group(0)
        return WINDOWS_INVALID_FILENAME_CHAR_MAP.get(char, replacement)

    cleaned = WINDOWS_INVALID_FILENAME_CHARS_RE.sub(replace_invalid_char, str(folder_name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    while " - - " in cleaned:
        cleaned = cleaned.replace(" - - ", " - ")
    cleaned = re.sub(rf"{re.escape(replacement)}+", replacement, cleaned)
    if not cleaned:
        cleaned = "未命名"
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned.rstrip(" ._") or "未命名"


def _clean_folder_component(component: str) -> str:
    """清洗单个文件夹业务片段，保留片段内部的 +，但去掉首尾误带的 +。"""

    cleaned = sanitize_folder_name(str(component), max_length=None)
    cleaned = re.sub(r"\++", "+", cleaned).strip("+")
    return cleaned.strip()


def _folder_name_within_limits(folder_name: str, max_length: int) -> bool:
    """判断文件夹名是否同时满足字符长度和网络盘字节长度限制。"""

    return len(folder_name) <= max_length and len(folder_name.encode("utf-8")) <= FOLDER_NAME_MAX_UTF8_BYTES


def shorten_folder_name_by_components(
    components: list[str],
    max_length: int = FOLDER_NAME_MAX_LENGTH,
) -> FolderNameShortenResult:
    """按 + 分隔的完整业务片段缩短文件夹名。

    文件夹名过长时只能删除完整的 + 片段；
    不能硬截断半个业务片段，并且必须保留完整平台单号和完整人名。
    """

    full_components = [
        cleaned
        for component in components
        if str(component or "").strip()
        for cleaned in [_clean_folder_component(str(component))]
        if cleaned
    ]
    full_folder_name = "+".join(full_components)
    if _folder_name_within_limits(full_folder_name, max_length):
        return FolderNameShortenResult(
            full_folder_name=full_folder_name,
            safe_folder_name=full_folder_name,
            full_components=full_components,
            safe_components=full_components,
            removed_components=[],
            was_shortened=False,
            max_length=max_length,
        )
    if len(full_components) < 2:
        return FolderNameShortenResult(
            full_folder_name=full_folder_name,
            safe_folder_name=full_folder_name,
            full_components=full_components,
            safe_components=full_components,
            removed_components=[],
            was_shortened=False,
            max_length=max_length,
            error="folder_name_too_long_even_minimal",
        )
    safe_components = list(full_components)
    removed_components: list[str] = []
    while not _folder_name_within_limits("+".join(safe_components), max_length) and len(safe_components) > 2:
        removed_components.insert(0, safe_components.pop(-2))
    safe_folder_name = "+".join(safe_components)
    if not _folder_name_within_limits(safe_folder_name, max_length):
        return FolderNameShortenResult(
            full_folder_name=full_folder_name,
            safe_folder_name=safe_folder_name,
            full_components=full_components,
            safe_components=safe_components,
            removed_components=removed_components,
            was_shortened=bool(removed_components),
            max_length=max_length,
            error="folder_name_too_long_even_minimal",
        )
    return FolderNameShortenResult(
        full_folder_name=full_folder_name,
        safe_folder_name=safe_folder_name,
        full_components=full_components,
        safe_components=safe_components,
        removed_components=removed_components,
        was_shortened=True,
        max_length=max_length,
    )


def sanitize_path_part(value: str, replacement: str = "_", max_length: int = 120) -> str:
    """兼容旧阶段测试的路径片段清洗函数。"""
    return sanitize_folder_name(value, replacement=replacement, max_length=max_length)


def build_order_folder_name(task_or_components: OrderFolderTask | list[str]) -> str:
    """过滤空组件并用 + 拼接最终文件夹名。

    兼容旧的 OrderFolderTask 调用；新订单定制文件夹传入组件列表。
    """
    if isinstance(task_or_components, OrderFolderTask):
        raw = f"{task_or_components.platform_order_no} {task_or_components.system_order_no} {task_or_components.title}"
        return sanitize_folder_name(raw, max_length=FOLDER_NAME_MAX_LENGTH)
    components = [
        cleaned
        for item in task_or_components
        if str(item or "").strip()
        for cleaned in [_clean_folder_component(str(item))]
        if cleaned
    ]
    return sanitize_folder_name("+".join(components), max_length=FOLDER_NAME_MAX_LENGTH)


def build_order_folder_path(root: str | Path, task: OrderFolderTask, current_date: date | None = None) -> Path:
    """构建订单文件夹路径。"""
    if current_date is None:
        raise ValueError("文件夹日期必须显式传入，不能默认使用今天。")
    return build_daily_folder(root, current_date) / build_order_folder_name(task)


def find_platform_order_folders(
    root: str | Path,
    folder_date: date,
    platform_order_nos: Iterable[str],
    *,
    strict: bool = False,
) -> dict[str, Path]:
    """Match order folders from the fixed month/day/order directory layout.

    Production output lives on an SFTP-mounted Synology volume.  Recursively
    walking a month also enters every order folder and performs thousands of
    network metadata calls.  Order folders are direct children of the daily
    directories, so scan exactly those two directory levels and stop as soon
    as every requested platform order is found.

    ``strict`` is used by authoritative reconciliation: an unavailable mount
    must abort that operation instead of being mistaken for proof that folders
    are absent.  Interactive single-order lookup keeps the historical
    best-effort behavior and returns no match on an unreadable directory.
    """

    targets = {
        str(value or "").strip()
        for value in platform_order_nos
        if str(value or "").strip()
    }
    if not targets:
        return {}
    remaining = set(targets)
    matches: dict[str, Path] = {}
    month_folder = build_month_folder(root, folder_date)
    try:
        with os.scandir(month_folder) as entries:
            day_entries = sorted(entries, key=lambda item: item.name)
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}

    for day_entry in day_entries:
        try:
            if not day_entry.is_dir(follow_symlinks=False):
                continue
            with os.scandir(day_entry.path) as entries:
                order_entries = sorted(
                    entries,
                    key=lambda item: item.name,
                )
        except FileNotFoundError:
            continue
        except OSError:
            if strict:
                raise
            continue

        for order_entry in order_entries:
            # Keep the historical substring-matching rule exactly.  Some
            # migrated records contain non-standard platform identifiers.
            matched_targets = {
                target for target in remaining if target in order_entry.name
            }
            if not matched_targets:
                continue
            try:
                if not order_entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                if strict:
                    raise
                continue
            path = Path(order_entry.path)
            for target in sorted(matched_targets):
                matches[target] = path
                remaining.discard(target)
            if not remaining:
                return matches
    return matches


def find_existing_platform_order_folder(
    root: str | Path,
    folder_date: date,
    platform_order_no: str,
) -> Path | None:
    """
    在付款时间所在月份查找是否已存在同平台单号文件夹。

    业务原因：补跑或重复巡检时，订单可能已经在同月其它日期目录下归档。
    如果当月任意文件夹名已包含平台单号，就不能再新建重复文件夹，但该订单可以加入 processed。
    """
    platform_order_no = str(platform_order_no or "").strip()
    if not platform_order_no:
        return None
    return find_platform_order_folders(
        root,
        folder_date,
        (platform_order_no,),
    ).get(platform_order_no)


def _lookup_required(mapping: dict[str, str], title: str, value: str) -> str:
    """查找必填规则值，缺失时抛出可定位的规则错误。"""
    key = normalize_rule_key(value)
    lookup = lookup_with_plural_variants(mapping, key)
    if lookup.matched:
        return lookup.value or ""
    if is_empty_option(value):
        return ""
    # 未知选项必须返回 folder_rule_missing，不能猜一个默认中文片段导致误建文件夹。
    raise FolderRuleMissingError(title, value)


def _find_first_pair(pairs: dict[str, str], titles: tuple[str, ...]) -> tuple[str, str] | None:
    """按标题别名查找第一个可用的定制化选项值。"""
    for title in titles:
        if title in pairs:
            return title, pairs[title]
    return None


def _double_side_counts(value: str | None) -> dict[str, int]:
    """处理双面面数量相关逻辑，并返回后续流程所需结果。"""

    text = normalize_rule_key(value)
    output: dict[str, int] = {}
    if not text or is_empty_option(text):
        return output
    # 只有明确选择 2-sided / double-sided 时才修饰墙体；1-sided Printing 只是单面，不应加“双面”。
    if not _is_double_sided_option(text):
        return output
    # Amazon 选项里的数字表示需要双面打印的数量，不能只按 full/half 类型整体加“双面”。
    for match in re.finditer(r"\b(?P<count>\d+)\s*(?P<kind>full|half)\b", text):
        kind = "full_wall" if match.group("kind") == "full" else "half_wall"
        output[kind] = output.get(kind, 0) + int(match.group("count"))
    return output


def _is_double_sided_option(value: str | None) -> bool:
    """判断选项是否明确要求双面打印。"""

    text = normalize_rule_key(value)
    return bool(
        re.search(
            r"\b2\s*-\s*sided\b|\b2\s*sided\b|double\s*-\s*sided|double\s+sided",
            text,
        )
    )


def _wall_component_count(component: WallRuleComponent) -> int:
    """读取规则组件开头的墙体数量；规则数据异常时保持安全失败。"""

    count_match = re.match(r"^(\d+)", component.text)
    if not count_match:
        raise FolderRuleMissingError(TITLE_SIDE_WALL, component.text)
    return int(count_match.group(1))


def _split_double_side_walls(
    component: WallRuleComponent,
    double_count: int,
    double_value: str | None,
) -> list[str]:
    """按墙体总数和双面数量拆分中文片段。

    Double-sided Printing Options 里的数字是双面墙数量；数量超过实际墙体数必须报错，
    避免把 1 个双面全高背墙误生成成 3 个双面全高背墙。
    """
    count = _wall_component_count(component)
    count_match = re.match(r"^(\d+)", component.text)
    assert count_match is not None
    suffix = component.text[count_match.end():]
    if double_count <= 0:
        return [component.text]

    if double_count > count:
        raise FolderRuleMissingError(TITLE_DOUBLE_SIDE, double_value or "")
    if double_count == count:
        return [f"{count}双面{suffix}"]

    # 同一种墙体里只有一部分双面时，先列双面数量，再列剩余单面数量。
    single_count = count - double_count
    return [f"{double_count}双面{suffix}", f"{single_count}{suffix}"]


def _apply_double_side_wall_counts(
    components: tuple[WallRuleComponent, ...] | list[WallRuleComponent],
    double_value: str | None,
) -> list[str]:
    """通用校验墙型和数量，再把双面数量准确分配到实际墙体。"""

    double_counts = _double_side_counts(double_value)
    if _is_double_sided_option(double_value) and (
        not double_counts
        or any(requested_count <= 0 for requested_count in double_counts.values())
    ):
        raise FolderRuleMissingError(TITLE_DOUBLE_SIDE, double_value or "")
    if not double_counts:
        return [component.text for component in components]

    available_counts: dict[str, int] = {}
    for component in components:
        available_counts[component.kind] = (
            available_counts.get(component.kind, 0)
            + _wall_component_count(component)
        )

    for kind, requested_count in double_counts.items():
        if kind == "half_wall" and requested_count > MAX_DOUBLE_SIDE_HALF_WALLS:
            raise FolderRuleMissingError(TITLE_DOUBLE_SIDE, double_value or "")
        if requested_count > available_counts.get(kind, 0):
            raise FolderRuleMissingError(TITLE_DOUBLE_SIDE, double_value or "")

    remaining = dict(double_counts)
    result: list[str] = []
    for component in components:
        component_count = _wall_component_count(component)
        allocated = min(remaining.get(component.kind, 0), component_count)
        result.extend(
            _split_double_side_walls(
                component,
                allocated,
                double_value,
            )
        )
        remaining[component.kind] = max(
            0,
            remaining.get(component.kind, 0) - allocated,
        )
    return result


def _wall_components(pairs: dict[str, str], rules: OrderFolderRules) -> list[str]:
    """生成侧墙组件文件夹名组件。"""
    value = pairs.get(TITLE_SIDE_WALL)
    double_value = pairs.get(TITLE_DOUBLE_SIDE)
    if value is None or is_empty_option(value):
        return _apply_double_side_wall_counts((), double_value)
    key = normalize_rule_key(value)
    lookup = lookup_with_plural_variants(rules.wall_options, key)
    if not lookup.matched or lookup.value is None:
        raise FolderRuleMissingError(TITLE_SIDE_WALL, value)
    return _apply_double_side_wall_counts(lookup.value, double_value)


def _table_cloth_component(pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成桌布组件文件夹名组件。"""
    pair = _find_first_pair(pairs, TABLE_CLOTH_TITLES)
    if pair is None or is_empty_option(pair[1]):
        return ""
    title, value = pair
    return _lookup_required(rules.table_cloth_options, title, value)


def _flag_component(value: str | None) -> str:
    """生成旗帜组件文件夹名组件。"""
    if value is None or is_empty_option(value):
        return ""
    normalized = re.sub(r"\s+", " ", value.strip())
    match = re.fullmatch(
        r"(?P<count>[12])\s+Set:\s*(?P<size>6\.9ft|9\.8ft)\s+2-Sided\s+"
        r"(?P<shape>Feather|Teardrop)\+Pole\+(?P<mount>Base|Holder)",
        normalized,
        flags=re.I,
    )
    if not match:
        raise FolderRuleMissingError(TITLE_FLAG, value)
    count = match.group("count")
    shape = match.group("shape").lower()
    size = match.group("size").lower()
    size_text = {
        ("feather", "6.9ft"): "0.5x2m",
        ("feather", "9.8ft"): "0.6x2.5m",
        ("teardrop", "6.9ft"): "0.75x1.65m",
        ("teardrop", "9.8ft"): "0.95x2.3m",
    }[(shape, size)]
    shape_text = "双面刀旗" if shape == "feather" else "双面水滴旗"
    mount_text = (
        "全纤维杆+铁板十字底座3KG+水袋"
        if match.group("mount").lower() == "base"
        else "全纤维杆+连接件+夹具"
    )
    return f"{count}套（{size_text}{shape_text}+{mount_text}）"


def _accessory_component(title: str, pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成配件组件文件夹名组件。"""
    if title not in pairs:
        return ""
    value = pairs[title]
    key = (title, normalize_rule_key(value))
    if key in rules.accessory_options:
        return rules.accessory_options[key]
    value_lookup = lookup_with_plural_variants(
        {
            option_key: component
            for option_title, option_key in rules.accessory_options
            if option_title == title
            for component in [rules.accessory_options[(option_title, option_key)]]
        },
        key[1],
    )
    if value_lookup.matched:
        return value_lookup.value or ""
    if is_empty_option(value):
        return ""
    raise FolderRuleMissingError(title, value)


def _rail_adapter_component(pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成横杆转接件对应的文件夹名片段。"""
    if TITLE_RAIL_ADAPTER not in pairs:
        return ""
    return _lookup_required(rules.rail_adapter_options, TITLE_RAIL_ADAPTER, pairs[TITLE_RAIL_ADAPTER])


def _full_wall_attachment_component(pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成全高侧墙安装方式对应的文件夹名片段。"""
    if TITLE_FULL_WALL_ATTACHMENT not in pairs:
        return ""
    return _lookup_required(rules.full_wall_attachment_options, TITLE_FULL_WALL_ATTACHMENT, pairs[TITLE_FULL_WALL_ATTACHMENT])


def _full_wall_size_component(pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成全高侧墙尺寸组件文件夹名组件。"""
    if TITLE_FULL_WALL_SIZE not in pairs:
        return ""
    return _lookup_required(rules.full_wall_size_options, TITLE_FULL_WALL_SIZE, pairs[TITLE_FULL_WALL_SIZE])


def _canopy_frame_size_component(pairs: dict[str, str], rules: OrderFolderRules) -> str:
    """生成帐篷架尺寸对应的文件夹名片段。"""
    if TITLE_CANOPY_FRAME_SIZE not in pairs:
        return ""
    return _lookup_required(rules.canopy_frame_size_options, TITLE_CANOPY_FRAME_SIZE, pairs[TITLE_CANOPY_FRAME_SIZE])


def _destination_category_from_shipping_address(shipping_address_text: str | None) -> str:
    """从收货地址推导文件夹命名用地区分类，缺失时保持旧规则。"""

    if not str(shipping_address_text or "").strip():
        return ""
    return parse_destination_region(shipping_address_text).category


def _frame_component(
    pairs: dict[str, str],
    rules: OrderFolderRules,
    *,
    destination_category: str = "",
) -> str:
    """生成支架片段；38mm 仅在加拿大/美国非本土保留原规格。"""

    frame_pair = _find_first_pair(pairs, FRAME_TITLES)
    if not frame_pair:
        return ""
    title, value = frame_pair
    if (
        destination_category in {"canada", "us_non_mainland"}
        and normalize_rule_key(value) == normalize_rule_key('Standard 1.5"/38mm square aluminum')
    ):
        return "38mm方形铝"
    return _lookup_required(rules.frame_options, title, value)


def _wall_only_text(kind: str, quantity: int, pairs: dict[str, str]) -> list[str]:
    """独立墙体 ASIN 按数量生成全高/半高墙面片段，可能因双面打印限制拆分为多段。"""
    double_value = pairs.get(TITLE_DOUBLE_SIDE)
    base_text = f"{quantity}全高背墙" if kind == "full_wall" else f"{quantity}半高侧墙"
    components = _apply_double_side_wall_counts(
        (WallRuleComponent(kind=kind, text=base_text),),
        double_value,
    )
    if kind == "full_wall":
        # B0D6KZ7G88 是单卖 3x3m 帐篷全围，不包含帐篷顶；
        # 文件夹名要直接说明“3x3m帐篷的全高背墙”，并按双面数量拆分。
        return [f"{component[0]}个3x3m帐篷的{component[1:]}" for component in components]
    return components


def _is_expedited_order(
    logistics: str | None,
    *,
    asin: str | None = None,
    order_lines: list[OrderFolderLine] | None = None,
    line_items: list[dict[str, Any]] | None = None,
    destination_category: str = "",
) -> bool:
    """判断订单是否需要在文件夹名前加“加急”。"""

    logistics_text = str(logistics or "").lower()
    if "expedited" in logistics_text or "加急" in str(logistics or ""):
        return True
    if destination_category in {"canada", "us_non_mainland"}:
        return False
    if is_default_expedited_tent_asin(asin):
        return True
    if order_lines and any(is_default_expedited_tent_asin(line.asin) for line in order_lines):
        return True
    if line_items and any(is_default_expedited_tent_asin(str(item.get("asin") or item.get("ASIN") or "")) for item in line_items):
        return True
    return False


TENT_SAME_DESIGN_OPTIONS = {
    normalize_rule_key("Yes, please use the same design."): "相同设计",
    normalize_rule_key("Yes, please use the same design"): "相同设计",
    normalize_rule_key("Yes, please ues the same design."): "相同设计",
    normalize_rule_key("Yes, please ues the same design"): "相同设计",
    normalize_rule_key("No, I would like different design."): "不同设计",
    normalize_rule_key("No, I would like different design"): "不同设计",
    normalize_rule_key("No, I would like different designs."): "不同设计",
    normalize_rule_key("No, I would like different designs"): "不同设计",
}


def _tent_same_design_component(pairs: dict[str, str]) -> str:
    """生成帐篷相同设计组件文件夹名组件。"""
    if TITLE_TENT_SAME_DESIGN not in pairs:
        return ""
    return _lookup_required(TENT_SAME_DESIGN_OPTIONS, TITLE_TENT_SAME_DESIGN, pairs[TITLE_TENT_SAME_DESIGN])


def _format_inches(value: float) -> str:
    """格式化英寸。"""
    return str(int(value)) if value.is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


def _car_magnet_ratio_size(size_text: str, ratio: str) -> str:
    """处理汽车磁贴 比例尺寸相关逻辑，并返回后续流程所需结果。"""
    number_match = re.fullmatch(r"(?P<number>\d+(?:\.\d+)?)in", size_text)
    if not number_match:
        raise FolderRuleMissingError(TITLE_CAR_MAGNET_SIZE, size_text)
    width = float(number_match.group("number"))
    height = width / 2 if ratio == "2:1" else width * 3 / 4
    return f"{_format_inches(width)}x{_format_inches(height)}in"


def _car_magnet_special_product_component(
    pairs: dict[str, str],
    rules: OrderFolderRules,
) -> tuple[str, str]:
    """生成 B0CRKYV7C9 的“尺寸+形状+品名”和额外圆角片段。

    该 ASIN 的尺寸不是由子 ASIN 决定，而是由 Car Magnet Size + Shapes / Die Cut 两个定制项共同决定；
    长方形比例选项需要把 8in 这类单边尺寸换算成 8x4in / 8x6in。
    """
    raw_size = pairs.get(TITLE_CAR_MAGNET_SIZE)
    size_text = normalize_car_magnet_size_value(raw_size)
    if not size_text:
        raise FolderRuleMissingError(TITLE_CAR_MAGNET_SIZE, raw_size or "missing")
    shape_value = pairs.get(TITLE_CAR_MAGNET_SHAPE)
    if not shape_value:
        raise FolderRuleMissingError(TITLE_CAR_MAGNET_SHAPE, "missing")
    shape_key = normalize_rule_key(shape_value)
    shape_lookup = lookup_with_plural_variants(rules.car_magnet_shape_options, shape_key)
    if not shape_lookup.matched or shape_lookup.value is None:
        raise FolderRuleMissingError(TITLE_CAR_MAGNET_SHAPE, shape_value)
    product_text, extra_component = shape_lookup.value
    if "length:width=2:1" in shape_key:
        size_text = _car_magnet_ratio_size(size_text, "2:1")
    elif "length:width=4:3" in shape_key:
        size_text = _car_magnet_ratio_size(size_text, "4:3")
    return f"{size_text}{product_text}", extra_component


def _car_magnet_fixed_product_component(asin: str | None) -> str:
    """生成汽车磁贴固定产品组件文件夹名组件。"""
    size_text = get_car_magnet_fixed_size(asin)
    if not size_text:
        raise MissingSizeRuleError(str(asin or ""))
    return f"{size_text}汽车磁贴"


def _car_magnet_lookup_optional(
    mapping: dict[str, str],
    title: str,
    pairs: dict[str, str],
) -> str:
    """查找汽车磁贴可选规则值，未命中时返回空结果。"""
    if title not in pairs:
        return ""
    return _lookup_required(mapping, title, pairs[title])


def _car_magnet_required_lookup(
    mapping: dict[str, str],
    title: str,
    pairs: dict[str, str],
) -> str:
    """查找汽车磁贴必填规则值，缺失时抛出可定位的规则错误。"""
    if title not in pairs:
        raise FolderRuleMissingError(title, "missing")
    return _lookup_required(mapping, title, pairs[title])


def _car_magnet_same_design_component(parent_asin: str | None, pairs: dict[str, str]) -> str:
    """生成汽车磁贴相同设计组件文件夹名组件。"""
    if parent_asin != CAR_MAGNET_SAME_DESIGN_PARENT_ASIN:
        return ""
    value = pairs.get(TITLE_CAR_MAGNET_SAME_DESIGN) or pairs.get(CAR_MAGNET_SAME_DESIGN_TITLE)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    lookup = lookup_with_plural_variants(CAR_MAGNET_SAME_DESIGN_OPTIONS, normalize_car_magnet_same_design_value(value))
    if lookup.matched:
        return lookup.value or ""
    raise FolderRuleMissingError(TITLE_CAR_MAGNET_SAME_DESIGN, str(value))


def _car_magnet_proof_component(pairs: dict[str, str]) -> str:
    """生成汽车磁贴确认稿组件文件夹名组件。"""
    value = pairs.get(TITLE_CAR_MAGNET_PROOF) or pairs.get(CAR_MAGNET_PROOF_TITLE)
    if value is None:
        return ""
    if not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_car_magnet_proof_value(value)
    if key == normalize_car_magnet_proof_value(CAR_MAGNET_LEGACY_PROOF_INSTRUCTION):
        return ""
    lookup = lookup_with_plural_variants(CAR_MAGNET_PROOF_OPTIONS, key)
    if lookup.matched:
        return lookup.value or ""
    raise FolderRuleMissingError(TITLE_CAR_MAGNET_PROOF, str(value))


def _car_magnet_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
    rules: OrderFolderRules,
) -> list[str]:
    """生成汽车磁贴组件文件夹名组件。"""
    components = [
        platform_order_no,
        *_car_magnet_item_components(
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=row_quantity,
            pairs=pairs,
            rules=rules,
        ),
        recipient_name,
        _car_magnet_proof_component(pairs),
    ]
    return [component for component in components if component]


def _car_magnet_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    rules: OrderFolderRules,
) -> list[str]:
    """生成单个汽车磁贴商品行的文件夹片段，不包含平台单号和客户名。"""
    parent = parent_asin or find_car_magnet_parent_asin(asin)
    unit_quantity = get_car_magnet_unit_quantity(parent)
    if not unit_quantity:
        raise MissingSizeRuleError(str(asin or ""))
    total_quantity = unit_quantity * max(row_quantity, 1)

    if find_car_magnet_parent_asin(asin) == "B0CRKSZ5TB":
        product_component, shape_extra = _car_magnet_special_product_component(pairs, rules)
    else:
        product_component = _car_magnet_fixed_product_component(asin)
        shape_extra = _car_magnet_lookup_optional(rules.car_magnet_corner_options, TITLE_CAR_MAGNET_CORNER, pairs)

    return [
        f"{total_quantity}个{product_component}",
        _car_magnet_same_design_component(parent, pairs),
        shape_extra,
        _car_magnet_lookup_optional(rules.car_magnet_surface_options, TITLE_CAR_MAGNET_SURFACE, pairs),
        _car_magnet_required_lookup(rules.car_magnet_thickness_options, TITLE_CAR_MAGNET_THICKNESS, pairs),
    ]


def _poster_item_components(*, parent_asin: str | None, asin: str | None, row_quantity: int) -> list[str]:
    """根据单个海报 OrderItem 生成商品片段。

    海报没有用于区分设计的定制选项，尺寸规格品名完全由子 ASIN 决定；
    数量必须来自 Amazon QuantityOrdered，不能从 DOM 的 xN 读取。
    """

    parent = parent_asin or find_poster_parent_asin(asin)
    if not parent:
        raise PosterFolderError(
            "posters_unknown_parent_asin",
            f"海报 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    fragment = get_poster_fragment(asin)
    if not fragment:
        raise PosterFolderError(
            "posters_missing_fragment_rule",
            f"海报 ASIN 找不到尺寸规格品名规则：{asin}",
            parent_asin=parent,
        )
    quantity = max(int(row_quantity or 0), 1)
    return [f"{quantity}个{fragment}"]


def _poster_proof_component(pairs: dict[str, str]) -> str:
    """从海报 JSON pairs 中读取 Proof Option。

    Proof 是选填项：缺失或空值时不参与文件夹名；出现未知值时返回明确规则错误，避免误建。
    """

    value = get_poster_pair_by_title_aliases(pairs, POSTER_PROOF_TITLE_ALIASES)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    component = lookup_poster_proof_option(value)
    if component is not None:
        return component
    raise PosterFolderError(
        "posters_rule_missing",
        f"缺少海报文件夹规则：{POSTER_PROOF_TITLE_ALIASES[0]} = {value}",
        title=POSTER_PROOF_TITLE_ALIASES[0],
        value=str(value),
    )


def _poster_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成海报组件文件夹名组件。"""
    item_components = _poster_item_components(parent_asin=parent_asin, asin=asin, row_quantity=row_quantity)
    proof_component = _poster_proof_component(pairs)
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _tablecloth_lookup_required(parent_asin: str, group: str, title: str, value: str) -> str:
    """查找桌布必填规则值，缺失时抛出可定位的规则错误。"""
    option_rules = get_tablecloth_option_rules(parent_asin, group)
    key = normalize_tablecloth_option_value(value)
    lookup = lookup_with_plural_variants(option_rules, key)
    if lookup.matched:
        return lookup.value or ""
    raise FolderRuleMissingError(title, value)


def _tablecloth_lookup_by_aliases(parent_asin: str, group: str, pairs: dict[str, str], *, required: bool) -> str:
    """按标题别名查找桌布规则值。"""
    aliases = TABLECLOTH_TITLE_ALIASES[group]
    value = get_tablecloth_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        if required:
            raise FolderRuleMissingError(aliases[0], "missing")
        return ""
    return _tablecloth_lookup_required(parent_asin, group, aliases[0], value)


def _tablecloth_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """生成桌布条目组件单个订单行的文件夹名组件。"""
    parent = parent_asin or find_tablecloth_parent_asin(asin)
    if not parent:
        raise MissingSizeRuleError(str(asin or ""))
    size = get_tablecloth_size(asin)
    product_name = get_tablecloth_product_name(parent)
    if not size or not product_name:
        raise MissingSizeRuleError(str(asin or ""))
    quantity = max(int(row_quantity or 0), 1)
    components = [
        f"{quantity}个{size}{product_name}",
        _tablecloth_lookup_by_aliases(parent, "fabric", pairs, required=True),
        _tablecloth_lookup_by_aliases(parent, "back", pairs, required=False),
    ]
    return [component for component in components if component]


def _tablecloth_proof_component(parent_asin: str, pairs: dict[str, str]) -> str:
    """生成桌布确认稿组件文件夹名组件。"""
    return _tablecloth_lookup_by_aliases(parent_asin, "proof", pairs, required=False)


def _tablecloth_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成桌布组件文件夹名组件。"""
    parent = parent_asin or find_tablecloth_parent_asin(asin)
    item_components = _tablecloth_item_components(
        parent_asin=parent,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _tablecloth_proof_component(parent, pairs) if parent else ""
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _table_runner_lookup_required(parent_asin: str, group: str, title: str, value: str) -> str:
    """查找桌旗必填规则值，缺失时抛出可定位的规则错误。"""
    option_rules = get_table_runner_option_rules(parent_asin, group)
    key = normalize_table_runner_option_value(value)
    lookup = lookup_with_plural_variants(option_rules, key)
    if lookup.matched:
        return lookup.value or ""
    raise TableRunnerFolderError(
        "table_runners_rule_missing",
        f"缺少桌旗文件夹规则：{title} = {value}",
        title=title,
        value=value,
        parent_asin=parent_asin,
    )


def _table_runner_lookup_by_aliases(parent_asin: str, group: str, pairs: dict[str, str], *, required: bool) -> str:
    """按标题别名查找桌旗规则值。"""
    aliases = TABLE_RUNNER_TITLE_ALIASES[group]
    value = get_table_runner_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        if required:
            raise TableRunnerFolderError(
                "table_runners_rule_missing",
                f"缺少桌旗文件夹规则：{aliases[0]} = missing",
                title=aliases[0],
                value="missing",
                parent_asin=parent_asin,
            )
        return ""
    return _table_runner_lookup_required(parent_asin, group, aliases[0], value)


def _table_runner_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """根据单个桌旗 OrderItem 和 zip JSON pairs 生成商品片段。

    桌旗同样使用 zip 内 JSON 作为业务数据源；ERP 浮窗文字只用于下载 zip，
    不参与文件夹命名，避免 Notes 或浮窗截断影响规则判断。
    """

    parent = parent_asin or find_table_runner_parent_asin(asin)
    if not parent:
        raise TableRunnerFolderError(
            "table_runners_unknown_parent_asin",
            f"桌旗 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    size = get_table_runner_size(asin)
    if not size:
        raise TableRunnerFolderError(
            "table_runners_missing_size_rule",
            f"桌旗 ASIN 找不到尺寸规则：{asin}",
            parent_asin=parent,
        )
    quantity = max(int(row_quantity or 0), 1)
    components = [
        f"{quantity}个{size}{TABLE_RUNNER_PRODUCT_NAME}",
        _table_runner_lookup_by_aliases(parent, "material", pairs, required=True),
    ]
    return [component for component in components if component]


def _table_runner_proof_component(parent_asin: str, pairs: dict[str, str]) -> str:
    """生成桌旗 确认稿对应的文件夹名组件。"""

    return _table_runner_lookup_by_aliases(parent_asin, "proof", pairs, required=False)


def _table_runner_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成桌旗组件文件夹名组件。"""
    parent = parent_asin or find_table_runner_parent_asin(asin)
    item_components = _table_runner_item_components(
        parent_asin=parent,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _table_runner_proof_component(parent, pairs) if parent else ""
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _vinyl_banner_lookup_required(parent_asin: str, group: str, title: str, value: str) -> str:
    """查找喷绘横幅必填规则值，缺失时抛出可定位的规则错误。"""
    option_rules = get_vinyl_banner_option_rules(parent_asin, group)
    key = normalize_option_value(value)
    lookup = lookup_with_plural_variants(option_rules, key)
    if lookup.matched:
        return lookup.value or ""
    raise VinylBannerFolderError(
        "vinyl_banners_rule_missing",
        f"缺少喷绘文件夹规则：{title} = {value}",
        title=title,
        value=value,
        parent_asin=parent_asin,
    )


def _vinyl_banner_lookup_optional(parent_asin: str, group: str, pairs: dict[str, str]) -> str:
    """查找喷绘横幅可选规则值，未命中时返回空结果。"""
    aliases = VINYL_BANNER_TITLE_ALIASES[group]
    value = get_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_option_value(value)
    # 喷绘的 15oz sturdy vinyl 在生产命名中用“550”表示；
    # 其它材质不参与命名，所以材质未知时跳过，不误报规则缺失。
    if group == "surface_material":
        if "15 oz" in key and "sturdy vinyl" in key:
            return "550"
        lookup = lookup_with_plural_variants(get_vinyl_banner_option_rules(parent_asin, group), key)
        if lookup.matched:
            return lookup.value or ""
        return ""
    return _vinyl_banner_lookup_required(parent_asin, group, aliases[0], value)


def _vinyl_banner_proof_component(parent_asin: str, pairs: dict[str, str]) -> str:
    """生成喷绘横幅确认稿组件文件夹名组件。"""
    value = get_pair_by_title_aliases(pairs, VINYL_BANNER_TITLE_ALIASES["proof"])
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    return _vinyl_banner_lookup_required(parent_asin, "proof", VINYL_BANNER_TITLE_ALIASES["proof"][0], value)


def _vinyl_banner_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """根据单个喷绘 OrderItem 和 zip JSON pairs 生成商品片段。

    喷绘同样使用 zip 内 JSON 作为业务数据源；ERP 浮窗文字只用于下载 zip，不用于生成文件夹名。
    """

    parent = parent_asin or find_vinyl_banner_parent_asin(asin)
    if not parent:
        raise VinylBannerFolderError(
            "vinyl_banners_unknown_parent_asin",
            f"喷绘 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    size = get_vinyl_banner_size(asin)
    if not size:
        raise VinylBannerFolderError(
            "vinyl_banners_missing_size_rule",
            f"喷绘 ASIN 找不到尺寸规则：{asin}",
            parent_asin=parent,
        )
    printed_value = get_pair_by_title_aliases(pairs, VINYL_BANNER_TITLE_ALIASES["printed_sides"])
    if printed_value is None or not str(printed_value).strip():
        printed_value = get_vinyl_banner_default_printed_sides(asin)
    if printed_value is None or not str(printed_value).strip():
        raise VinylBannerFolderError(
            "vinyl_banners_rule_missing_printed_sides",
            "喷绘缺少 Printed Sides 定制选项",
            title=VINYL_BANNER_TITLE_ALIASES["printed_sides"][0],
            value="missing",
            parent_asin=parent,
        )
    printed_sides = _vinyl_banner_lookup_required(
        parent,
        "printed_sides",
        VINYL_BANNER_TITLE_ALIASES["printed_sides"][0],
        printed_value,
    )
    quantity = max(int(row_quantity or 0), 1)
    product_name = get_vinyl_banner_product_name(parent)
    components: list[str] = [f"{quantity}个{size}{printed_sides}{product_name}"]

    if printed_sides == "双面":
        components.append(_vinyl_banner_lookup_optional(parent, "same_design", pairs))
    components.append(_vinyl_banner_lookup_optional(parent, "surface_material", pairs))
    components.append(_vinyl_banner_lookup_optional(parent, "hanging", pairs))
    components.append(_vinyl_banner_lookup_optional(parent, "edge", pairs))
    components.append(_vinyl_banner_lookup_optional(parent, "packaging", pairs))
    components.append(_vinyl_banner_lookup_optional(parent, "accessories", pairs))
    return [component for component in components if component]


def _vinyl_banner_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成喷绘横幅组件文件夹名组件。"""
    parent = parent_asin or find_vinyl_banner_parent_asin(asin)
    item_components = _vinyl_banner_item_components(
        parent_asin=parent,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _vinyl_banner_proof_component(parent, pairs) if parent else ""
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _pop_up_display_lookup_required(parent_asin: str, group: str, title: str, value: str) -> str:
    """查找展架必填规则值，缺失时抛出可定位的规则错误。"""
    option_rules = get_pop_up_display_option_rules(parent_asin, group)
    key = normalize_pop_up_display_option_value(value)
    lookup = lookup_with_plural_variants(option_rules, key)
    if lookup.matched:
        return lookup.value or ""
    raise PopUpDisplayFolderError(
        "pop_up_displays_rule_missing",
        f"缺少拉网展架文件夹规则：{title} = {value}",
        title=title,
        value=value,
        parent_asin=parent_asin,
    )


def _pop_up_display_lookup_optional(parent_asin: str, group: str, pairs: dict[str, str]) -> str:
    """查找展架可选规则值，未命中时返回空结果。"""
    aliases = POP_UP_DISPLAY_TITLE_ALIASES[group]
    value = get_pop_up_display_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    return _pop_up_display_lookup_required(parent_asin, group, aliases[0], value)


def _pop_up_display_proof_component(parent_asin: str, pairs: dict[str, str]) -> str:
    """生成展架确认稿组件文件夹名组件。"""
    value = get_pop_up_display_pair_by_title_aliases(pairs, POP_UP_DISPLAY_TITLE_ALIASES["proof"])
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    return _pop_up_display_lookup_required(parent_asin, "proof", POP_UP_DISPLAY_TITLE_ALIASES["proof"][0], value)


def _pop_up_display_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """根据单个拉网展架 OrderItem 和 zip JSON pairs 生成商品片段。

    拉网展架同样使用 zip 内 JSON 作为业务数据源；ERP 浮窗文字只用于下载 zip，
    不参与文件夹名生成，避免 Notes/换行/浮窗截断影响规则匹配。
    """

    parent = parent_asin or find_pop_up_display_parent_asin(asin)
    if not parent:
        raise PopUpDisplayFolderError(
            "pop_up_displays_unknown_parent_asin",
            f"拉网展架 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    size = get_pop_up_display_size(asin)
    stand_type = get_pop_up_display_stand_type(asin)
    product_name = get_pop_up_display_product_name(parent, asin)
    if not size or not stand_type or not product_name:
        raise PopUpDisplayFolderError(
            "pop_up_displays_missing_size_rule",
            f"拉网展架 ASIN 找不到尺寸或支架规则：{asin}",
            parent_asin=parent,
        )

    quantity = max(int(row_quantity or 0), 1)
    if parent in POP_UP_DISPLAY_PARENTS_WITH_PRINTING_SIDES:
        printed_value = get_pop_up_display_pair_by_title_aliases(pairs, POP_UP_DISPLAY_TITLE_ALIASES["printing_sides"])
        if printed_value is None or not str(printed_value).strip():
            raise PopUpDisplayFolderError(
                "pop_up_displays_rule_missing_printing_sides",
                "拉网展架缺少单/双面定制选项",
                title=POP_UP_DISPLAY_TITLE_ALIASES["printing_sides"][0],
                value="missing",
                parent_asin=parent,
            )
        printed_sides = _pop_up_display_lookup_required(
            parent,
            "printing_sides",
            POP_UP_DISPLAY_TITLE_ALIASES["printing_sides"][0],
            printed_value,
        )
        components: list[str] = [f"{quantity}个{printed_sides}{size}{product_name}"]
    else:
        components = [f"{quantity}个{size}{product_name}"]

    components.append(_pop_up_display_lookup_optional(parent, "same_design", pairs))
    frame_component = ""
    if stand_type == "不带支架":
        frame_component = _pop_up_display_lookup_optional(parent, "frame", pairs)
    components.append(frame_component or stand_type)
    if parent == POP_UP_DISPLAY_PARENT_WITH_SIDE_PANELS:
        components.append(_pop_up_display_lookup_optional(parent, "side_panels", pairs))
    if parent == POP_UP_DISPLAY_PARENT_WITH_FABRIC_PANEL_QUANTITY:
        components.append(_pop_up_display_lookup_optional(parent, "fabric_panel_quantity", pairs))
    if parent in POP_UP_DISPLAY_PARENTS_WITH_LED:
        components.append(_pop_up_display_lookup_optional(parent, "led", pairs))
    return [component for component in components if component]


def _pop_up_display_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成展架组件文件夹名组件。"""
    parent = parent_asin or find_pop_up_display_parent_asin(asin)
    item_components = _pop_up_display_item_components(
        parent_asin=parent,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _pop_up_display_proof_component(parent, pairs) if parent else ""
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _roll_up_banner_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """生成单个易拉宝商品行的品名片段。

    易拉宝没有材质/边缘/附件等额外命名选项，尺寸和规格只认子 ASIN；
    因此这里不从 SKU 或标题猜测，缺少映射时直接返回易拉宝专用错误。
    """

    parent = parent_asin or find_roll_up_banner_parent_asin(asin)
    if not parent:
        raise RollUpBannerFolderError(
            "roll_up_banners_unknown_parent_asin",
            f"易拉宝 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    fragment = get_roll_up_banner_fragment(asin)
    if not fragment:
        raise RollUpBannerFolderError(
            "roll_up_banners_missing_fragment_rule",
            f"易拉宝 ASIN 找不到尺寸规格规则：{asin}",
            parent_asin=parent,
        )
    quantity = max(int(row_quantity or 0), 1)
    printing_process_component = _roll_up_banner_printing_process_component(pairs)
    return [component for component in [f"{quantity}个{fragment}", printing_process_component] if component]


def _roll_up_banner_printing_process_component(pairs: dict[str, str]) -> str:
    """将易拉宝打印工艺转换为紧跟品名的文件夹组件。"""

    value = pairs.get(ROLL_UP_BANNER_PRINTING_PROCESS_TITLE)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_roll_up_banner_option_value(value)
    lookup = lookup_with_plural_variants(ROLL_UP_BANNER_PRINTING_PROCESS_OPTIONS, key)
    if lookup.matched:
        return lookup.value or ""
    raise RollUpBannerFolderError(
        "roll_up_banners_rule_missing",
        f"缺少易拉宝文件夹规则：{ROLL_UP_BANNER_PRINTING_PROCESS_TITLE} = {value}",
        title=ROLL_UP_BANNER_PRINTING_PROCESS_TITLE,
        value=str(value),
    )


def _roll_up_banner_proof_component(pairs: dict[str, str]) -> str:
    """读取易拉宝 Proof Option，并按需转换为文件夹名里的生产确认片段。

    Proof 是可选项：缺失、空值或明确空选项时不加入文件夹名；
    但只要页面给了未知 Proof 值，就报规则错误，避免生成错误尾部片段。
    """

    value = pairs.get(ROLL_UP_BANNER_PROOF_TITLE)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_roll_up_banner_option_value(value)
    lookup = lookup_with_plural_variants(ROLL_UP_BANNER_PROOF_OPTIONS, key)
    if lookup.matched:
        return lookup.value or ""
    raise RollUpBannerFolderError(
        "roll_up_banners_rule_missing",
        f"缺少易拉宝文件夹规则：{ROLL_UP_BANNER_PROOF_TITLE} = {value}",
        title=ROLL_UP_BANNER_PROOF_TITLE,
        value=str(value),
    )


def _roll_up_banner_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """按“平台单号+品名+人名+Proof”顺序生成单行易拉宝文件夹组件。"""

    item_components = _roll_up_banner_item_components(
        parent_asin=parent_asin,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _roll_up_banner_proof_component(pairs)
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _x_stand_item_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
) -> list[str]:
    """生成单个 X展架商品行的品名片段。

    X展架尺寸只认子 ASIN；不能从 SKU、标题或其它展示产品规则推断，避免误建文件夹。
    """

    parent = parent_asin or find_x_stand_parent_asin(asin)
    if not parent:
        raise XStandFolderError(
            "x_stands_unknown_parent_asin",
            f"X展架 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    fragment = get_x_stand_fragment(asin)
    if not fragment:
        raise XStandFolderError(
            "x_stands_missing_fragment_rule",
            f"X展架 ASIN 找不到尺寸规则：{asin}",
            parent_asin=parent,
        )
    quantity = max(int(row_quantity or 0), 1)
    printing_process_component = _x_stand_printing_process_component(pairs)
    return [component for component in [f"{quantity}个{fragment}", printing_process_component] if component]


def _x_stand_printing_process_component(pairs: dict[str, str]) -> str:
    """将 X 展架打印工艺转换为紧跟品名的文件夹组件。"""

    value = pairs.get(X_STAND_PRINTING_PROCESS_TITLE)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_x_stand_option_value(value)
    lookup = lookup_with_plural_variants(X_STAND_PRINTING_PROCESS_OPTIONS, key)
    if lookup.matched:
        return lookup.value or ""
    raise XStandFolderError(
        "x_stands_rule_missing",
        f"缺少 X展架文件夹规则：{X_STAND_PRINTING_PROCESS_TITLE} = {value}",
        title=X_STAND_PRINTING_PROCESS_TITLE,
        value=str(value),
    )


def _x_stand_proof_component(pairs: dict[str, str]) -> str:
    """生成展架 确认稿对应的文件夹名组件。"""

    value = pairs.get(X_STAND_PROOF_TITLE)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    key = normalize_x_stand_option_value(value)
    lookup = lookup_with_plural_variants(X_STAND_PROOF_OPTIONS, key)
    if lookup.matched:
        return lookup.value or ""
    raise XStandFolderError(
        "x_stands_rule_missing",
        f"缺少 X展架文件夹规则：{X_STAND_PROOF_TITLE} = {value}",
        title=X_STAND_PROOF_TITLE,
        value=str(value),
    )


def _x_stand_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """按“平台单号+品名+人名+Proof”顺序生成单行 X展架文件夹组件。"""

    item_components = _x_stand_item_components(
        parent_asin=parent_asin,
        asin=asin,
        row_quantity=row_quantity,
        pairs=pairs,
    )
    proof_component = _x_stand_proof_component(pairs)
    return [component for component in [platform_order_no, *item_components, recipient_name, proof_component] if component]


def _feather_flag_lookup_required(parent_asin: str, group: str, title: str, value: str) -> str:
    """查找刀旗必填规则值，缺失时抛出可定位的规则错误。"""
    option_rules = get_feather_flag_option_rules(parent_asin, group)
    key = normalize_feather_flag_option_value(value)
    lookup = lookup_with_plural_variants(option_rules, key)
    if lookup.matched:
        return lookup.value or ""
    raise FeatherFlagFolderError(
        "feather_flags_rule_missing",
        f"缺少刀旗文件夹规则：{title} = {value}",
        title=title,
        value=value,
        parent_asin=parent_asin,
    )


def _feather_flag_lookup_optional(parent_asin: str, group: str, pairs: dict[str, str]) -> str:
    """查找刀旗可选规则值，未命中时返回空结果。"""
    aliases = FEATHER_FLAG_TITLE_ALIASES[group]
    value = get_feather_flag_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip() or is_empty_option(str(value)):
        return ""
    return _feather_flag_lookup_required(parent_asin, group, aliases[0], value)


def _feather_flag_printing_side_components(parent_asin: str, pairs: dict[str, str]) -> tuple[str, str]:
    """生成刀旗打印面组件文件夹名组件。"""
    aliases = FEATHER_FLAG_TITLE_ALIASES["printing_side"]
    value = get_feather_flag_pair_by_title_aliases(pairs, aliases)
    if value is None or not str(value).strip():
        raise FeatherFlagFolderError(
            "feather_flags_rule_missing_printing_side",
            "刀旗缺少 Printing Side 定制选项",
            title=aliases[0],
            value="missing",
            parent_asin=parent_asin,
        )
    key = normalize_feather_flag_option_value(value)
    lookup = lookup_with_plural_variants(get_feather_flag_printing_side_rules(parent_asin), key)
    if lookup.matched and lookup.value:
        return lookup.value
    raise FeatherFlagFolderError(
        "feather_flags_rule_missing",
        f"缺少刀旗文件夹规则：{aliases[0]} = {value}",
        title=aliases[0],
        value=str(value),
        parent_asin=parent_asin,
    )


def _feather_flag_proof_component(parent_asin: str, pairs: dict[str, str]) -> str:
    """生成刀旗 确认稿对应的文件夹名组件。"""

    return _feather_flag_lookup_optional(parent_asin, "proof", pairs)


def _feather_flag_package_components(
    *,
    parent_asin: str | None,
    asin: str | None,
    pairs: dict[str, str],
) -> list[str]:
    """生成一套刀旗的内部配置片段，不包含数量、人名和 Proof。

    Printing Side 的“单面/双面”必须嵌入品名片段，而不是单独用 + 连接；
    这样才能得到“尺寸+单面/双面+子 ASIN 品名+相同设计+铝纤维杆...”的业务格式。
    """

    parent = parent_asin or find_feather_flag_parent_asin(asin)
    if not parent:
        raise FeatherFlagFolderError(
            "feather_flags_unknown_parent_asin",
            f"刀旗 ASIN 找不到父 ASIN：{asin}",
            parent_asin=str(parent_asin or ""),
        )
    size = get_feather_flag_size(asin)
    product_name = get_feather_flag_product_name(asin)
    if not size or not product_name:
        raise FeatherFlagFolderError(
            "feather_flags_missing_size_rule",
            f"刀旗 ASIN 找不到尺寸/品名规则：{asin}",
            parent_asin=parent,
        )
    side_component, design_component = _feather_flag_printing_side_components(parent, pairs)
    components = [
        f"{size}{side_component}{product_name}",
        design_component,
        _feather_flag_lookup_optional(parent, "pole", pairs),
        _feather_flag_lookup_optional(parent, "cross_base", pairs),
        _feather_flag_lookup_optional(parent, "carrying_bag", pairs),
        _feather_flag_lookup_optional(parent, "water_bag", pairs),
        _feather_flag_lookup_optional(parent, "ground_spike", pairs),
    ]
    return [component for component in components if component]


def _wrap_feather_flag_package_components(quantity: int, package_components: list[str]) -> str:
    """刀旗数量大于 1 时，配件属于整套配置，需要用 N套（...）包住。"""

    clean_components = [component for component in package_components if component]
    return f"{max(quantity, 1)}套（{'+'.join(clean_components)}）"


def _feather_flag_line_components(quantity: int, package_components: list[str]) -> list[str]:
    """生成刀旗行组件订单行级文件夹名组件。"""
    if quantity >= 2:
        return [_wrap_feather_flag_package_components(quantity, package_components)]
    if not package_components:
        return []
    return [f"{max(quantity, 1)}套{package_components[0]}", *package_components[1:]]


def _feather_flag_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    row_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
) -> list[str]:
    """生成刀旗组件文件夹名组件。"""
    parent = parent_asin or find_feather_flag_parent_asin(asin)
    package_components = _feather_flag_package_components(parent_asin=parent, asin=asin, pairs=pairs)
    proof_component = _feather_flag_proof_component(parent, pairs) if parent else ""
    return [
        component
        for component in [
            platform_order_no,
            *_feather_flag_line_components(max(int(row_quantity or 0), 1), package_components),
            recipient_name,
            proof_component,
        ]
        if component
    ]


def build_car_magnet_order_folder_components(
    *,
    platform_order_no: str,
    line_items: list[dict[str, Any]],
    recipient_name: str,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
) -> list[str]:
    """按 Amazon OrderItems 顺序生成汽车磁贴整单文件夹组件。

    同一平台单号下的多个汽车磁贴都属于同一个文件夹；Amazon API 提供数量和商品行顺序，
    领星 tooltip 提供每一行的定制选项。这里把每个商品行生成独立片段后拼到客户名前。
    """

    if _is_expedited_order(logistics, line_items=line_items):
        platform_order_no = f"加急{platform_order_no}"
    components: list[str] = [platform_order_no]
    for line in line_items:
        asin = str(line.get("asin") or "")
        parent_asin = str(line.get("parent_asin") or "") or find_car_magnet_parent_asin(asin)
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            raise ValueError("missing_quantity_ordered")
        customization_text = str(line.get("customization_text") or "")
        pairs = parse_customization_pairs(customization_text)
        components.extend(
            component
            for component in _car_magnet_item_components(
                parent_asin=parent_asin,
                asin=asin,
                row_quantity=quantity,
                pairs=pairs,
                rules=rules,
            )
            if component
        )
    components.append(recipient_name)
    return [component for component in components if component]


def _wrap_tent_package_components(quantity: int, package_components: list[str]) -> str:
    """把多套帐篷的单套配置合并成一个文件夹片段。

    业务要求：同一个帐篷顶商品买 2 套或更多时，数量表示“整套配置”重复；
    因此不能再写成 2个帐篷顶+支架+围墙，而要写成 2套（帐篷顶+支架+围墙...）。
    """

    clean_components = [component for component in package_components if component]
    return f"{quantity}套（{'+'.join(clean_components)}）"


def _tent_package_components(
    top_size: str,
    pairs: dict[str, str],
    rules: OrderFolderRules,
    *,
    destination_category: str = "",
) -> list[str]:
    """生成单套帐篷顶套餐的配置片段，不包含平台单号、数量和客户名。"""

    package_components: list[str] = [top_size]
    package_components.append(_tent_same_design_component(pairs))
    package_components.append(_frame_component(pairs, rules, destination_category=destination_category))

    package_components.extend(_wall_components(pairs, rules))

    if TITLE_FABRIC in pairs:
        package_components.append(_lookup_required(rules.fabric_options, TITLE_FABRIC, pairs[TITLE_FABRIC]))

    package_components.append(_accessory_component(TITLE_ROLLER_BAG, pairs, rules))
    package_components.append(_accessory_component(TITLE_SANDBAGS, pairs, rules))
    package_components.append(_accessory_component(TITLE_SANDBAGS_6PCS, pairs, rules))
    package_components.append(_accessory_component(TITLE_ROPE_STAKE, pairs, rules))
    package_components.append(_table_cloth_component(pairs, rules))
    package_components.append(_flag_component(pairs.get(TITLE_FLAG)))
    package_components.append(_canopy_frame_size_component(pairs, rules))
    return [component for component in package_components if component]


def _tent_package_line_components(quantity: int, package_components: list[str]) -> list[str]:
    """按单个商品行数量生成帐篷顶套餐片段。"""

    if quantity >= 2:
        return [_wrap_tent_package_components(quantity, package_components)]
    return [f"{max(quantity, 1)}个{package_components[0]}", *package_components[1:]]


def _wrap_item_components_when_customized(components: list[str]) -> list[str]:
    """商品行有定制片段时，把数量后的整套内容放进括号，提升多商品订单可读性。"""

    clean_components = [component for component in components if component]
    if len(clean_components) <= 1:
        return clean_components
    match = re.match(r"^(\d+\s*(?:个|套))(.+)$", clean_components[0])
    if not match:
        return clean_components
    quantity_prefix = re.sub(r"\s+", "", match.group(1))
    product_name = match.group(2).strip()
    if not product_name or product_name.startswith(("(", "（")):
        return clean_components
    return [f"{quantity_prefix}({'+'.join([product_name, *clean_components[1:]])})"]


def _merge_order_line_entries(entries: list[dict[str, Any]]) -> list[str]:
    """按 Amazon 商品行顺序展开组件，不跨商品行合并数量。"""

    output: list[str] = []
    wrap_line_components = len(entries) > 1
    for entry in entries:
        components = [component for component in entry.get("components", []) if component]
        if wrap_line_components and entry.get("wrap_components", True):
            output.extend(_wrap_item_components_when_customized(components))
        else:
            output.extend(components)
    return output


def build_order_folder_components_from_lines(
    *,
    platform_order_no: str,
    order_lines: list[OrderFolderLine],
    recipient_name: str,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
    shipping_address_text: str | None = None,
) -> list[str]:
    """按订单商品行顺序生成整单文件夹组件。

    多商品订单只生成一个文件夹：平台单号放第一位，客户名放最后，
    中间按 Amazon OrderItems 顺序追加每个商品行自己的定制片段。
    """

    destination_category = _destination_category_from_shipping_address(shipping_address_text)
    if _is_expedited_order(logistics, order_lines=order_lines, destination_category=destination_category):
        platform_order_no = f"加急{platform_order_no}"
    line_entries: list[dict[str, Any]] = []
    proof_components: list[str] = []

    def append_line_entry(line: OrderFolderLine) -> None:
        """追加单个订单行的文件夹组件条目。"""
        pairs = line.customization_pairs or parse_customization_pairs(line.customization_text)
        if line.product_type == PRODUCT_TYPE_CAR_MAGNET or is_car_magnet_asin(line.asin):
            parent = line.parent_asin or find_car_magnet_parent_asin(line.asin)
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_CAR_MAGNET,
                    "components": _car_magnet_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                        rules=rules,
                    ),
                }
            )
            proof_component = _car_magnet_proof_component(pairs)
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_TABLECLOTHS or is_tablecloth_asin(line.asin):
            parent = line.parent_asin or find_tablecloth_parent_asin(line.asin)
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_TABLECLOTHS,
                    "components": _tablecloth_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _tablecloth_proof_component(parent, pairs) if parent else ""
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_TABLE_RUNNERS or is_table_runner_asin(line.asin):
            parent = line.parent_asin or find_table_runner_parent_asin(line.asin)
            # 同一个桌旗 ASIN 可能出现多行；只有 ASIN 相同且材质等生成片段完全一致时才合并数量。
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_TABLE_RUNNERS,
                    "components": _table_runner_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _table_runner_proof_component(parent, pairs) if parent else ""
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_POSTERS or is_poster_asin(line.asin):
            parent = line.parent_asin or find_poster_parent_asin(line.asin)
            fragment = get_poster_fragment(line.asin)
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_POSTERS,
                    "merge_kind": "poster_asin",
                    "quantity": max(int(line.quantity or 0), 1),
                    "poster_fragment": fragment,
                    "components": _poster_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                    ),
                }
            )
            proof_component = _poster_proof_component(pairs)
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_POP_UP_DISPLAYS or is_pop_up_display_asin(line.asin):
            parent = line.parent_asin or find_pop_up_display_parent_asin(line.asin)
            # 同一个 ASIN 多行按 Amazon 商品行逐行保留。
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_POP_UP_DISPLAYS,
                    "components": _pop_up_display_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _pop_up_display_proof_component(parent, pairs) if parent else ""
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_ROLL_UP_BANNERS or is_roll_up_banner_asin(line.asin):
            # 易拉宝商品行生成“数量+尺寸规格易拉宝+打印工艺”；Proof 作为整单尾部片段单独收集。
            parent = line.parent_asin or find_roll_up_banner_parent_asin(line.asin)
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_ROLL_UP_BANNERS,
                    "wrap_components": False,
                    "components": _roll_up_banner_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _roll_up_banner_proof_component(pairs)
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_X_STANDS or is_x_stand_asin(line.asin):
            # X展架商品行生成“数量+尺寸X展架+打印工艺”；Proof 作为整单尾部片段单独收集。
            parent = line.parent_asin or find_x_stand_parent_asin(line.asin)
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_X_STANDS,
                    "wrap_components": False,
                    "components": _x_stand_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _x_stand_proof_component(pairs)
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_FEATHER_FLAGS or is_feather_flag_asin(line.asin):
            parent = line.parent_asin or find_feather_flag_parent_asin(line.asin)
            package_components = _feather_flag_package_components(parent_asin=parent, asin=line.asin, pairs=pairs)
            quantity = max(int(line.quantity or 0), 1)
            # 刀旗的杆、底座、手提袋等都是整套配置的一部分；多数量或跨行合并时，
            # 需要像帐篷套餐一样把整套配置放进“套（...）”里，避免配件看起来只属于某一个片段。
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_FEATHER_FLAGS,
                    "merge_kind": "feather_flag_package",
                    "quantity": quantity,
                    "package_components": package_components,
                    "components": _feather_flag_line_components(quantity, package_components),
                }
            )
            proof_component = _feather_flag_proof_component(parent, pairs) if parent else ""
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        if line.product_type == PRODUCT_TYPE_VINYL_BANNERS or is_vinyl_banner_asin(line.asin):
            parent = line.parent_asin or find_vinyl_banner_parent_asin(line.asin)
            # 同一个 ASIN 多行按 Amazon 商品行逐行保留。
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": PRODUCT_TYPE_VINYL_BANNERS,
                    "components": _vinyl_banner_item_components(
                        parent_asin=parent,
                        asin=line.asin,
                        row_quantity=line.quantity,
                        pairs=pairs,
                    ),
                }
            )
            proof_component = _vinyl_banner_proof_component(parent, pairs) if parent else ""
            if proof_component and proof_component not in proof_components:
                proof_components.append(proof_component)
            return
        top_size = get_tent_top_size(line.asin)
        if top_size and not get_wall_only_asin_kind(line.asin) and not is_car_magnet_asin(line.asin):
            package_components = _tent_package_components(
                top_size,
                pairs,
                rules,
                destination_category=destination_category,
            )
            line_entries.append(
                {
                    "asin": line.asin,
                    "product_type": "tent",
                    "merge_kind": "tent_package",
                    "quantity": max(int(line.quantity or 0), 1),
                    "package_components": package_components,
                    "components": _tent_package_line_components(max(int(line.quantity or 0), 1), package_components),
                }
            )
            return
        line_components = build_order_folder_components_from_pairs(
            platform_order_no="__ORDER__",
            parent_asin=line.parent_asin,
            asin=line.asin,
            tent_quantity=line.quantity,
            pairs=pairs,
            recipient_name="__RECIPIENT__",
            rules=rules,
            logistics=None,
            shipping_address_text=shipping_address_text,
        )
        # 单行规则本身仍复用原有商品逻辑；整单组合时去掉每行自己的平台单号和客户名，
        # 避免多商品订单出现重复单号/重复人名。
        line_entries.append(
            {
                "asin": line.asin,
                "product_type": line.product_type,
                "components": [component for component in line_components if component not in {"__ORDER__", "__RECIPIENT__"}],
            }
        )

    for line_index, line in enumerate(order_lines, start=1):
        try:
            append_line_entry(line)
        except (
            VinylBannerFolderError,
            PosterFolderError,
            TableRunnerFolderError,
            PopUpDisplayFolderError,
            RollUpBannerFolderError,
            XStandFolderError,
            FeatherFlagFolderError,
        ) as exc:
            if getattr(exc, "title", None) and not getattr(exc, "value", None):
                exc.value = "missing"
            if not getattr(exc, "missing_rule_line", None):
                exc.missing_rule_line = _customization_line_label(line_index, getattr(exc, "title", None), getattr(exc, "value", None))
            raise
    components: list[str] = [
        platform_order_no,
        *_merge_order_line_entries(line_entries),
    ]
    components.append(recipient_name)
    components.extend(proof_components[:1])
    return [component for component in components if component]


def build_order_folder_components_from_pairs(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    tent_quantity: int,
    pairs: dict[str, str],
    recipient_name: str,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
    shipping_address_text: str | None = None,
) -> list[str]:
    """按业务顺序从 JSON pairs 生成订单定制文件夹名组件。"""
    if is_car_magnet_asin(asin):
        parent_asin = parent_asin or find_car_magnet_parent_asin(asin)
    elif is_tablecloth_asin(asin):
        parent_asin = parent_asin or find_tablecloth_parent_asin(asin)
    elif is_table_runner_asin(asin):
        parent_asin = parent_asin or find_table_runner_parent_asin(asin)
    elif is_poster_asin(asin):
        parent_asin = parent_asin or find_poster_parent_asin(asin)
    elif is_pop_up_display_asin(asin):
        parent_asin = parent_asin or find_pop_up_display_parent_asin(asin)
    elif is_roll_up_banner_asin(asin):
        parent_asin = parent_asin or find_roll_up_banner_parent_asin(asin)
    elif is_x_stand_asin(asin):
        parent_asin = parent_asin or find_x_stand_parent_asin(asin)
    elif is_feather_flag_asin(asin):
        parent_asin = parent_asin or find_feather_flag_parent_asin(asin)
    elif is_vinyl_banner_asin(asin):
        parent_asin = parent_asin or find_vinyl_banner_parent_asin(asin)
    else:
        parent_asin = parent_asin or find_tent_parent_asin(asin)
    destination_category = _destination_category_from_shipping_address(shipping_address_text)
    # 加急订单在平台单号前加"加急"前缀
    if _is_expedited_order(logistics, asin=asin, destination_category=destination_category):
        platform_order_no = f"加急{platform_order_no}"
    if is_car_magnet_asin(asin):
        return _car_magnet_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
            rules=rules,
        )
    if is_tablecloth_asin(asin):
        return _tablecloth_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_table_runner_asin(asin):
        return _table_runner_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_poster_asin(asin):
        return _poster_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_pop_up_display_asin(asin):
        return _pop_up_display_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_roll_up_banner_asin(asin):
        return _roll_up_banner_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_x_stand_asin(asin):
        return _x_stand_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_feather_flag_asin(asin):
        return _feather_flag_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    if is_vinyl_banner_asin(asin):
        return _vinyl_banner_components(
            platform_order_no=platform_order_no,
            parent_asin=parent_asin,
            asin=asin,
            row_quantity=tent_quantity,
            pairs=pairs,
            recipient_name=recipient_name,
        )
    wall_only_kind = get_wall_only_asin_kind(asin)
    if wall_only_kind:
        components = [
            platform_order_no,
            *_wall_only_text(wall_only_kind, tent_quantity, pairs),
            _tent_same_design_component(pairs),
        ]
        if wall_only_kind == "full_wall":
            components.append(_full_wall_attachment_component(pairs, rules))
        components.append(_rail_adapter_component(pairs, rules))
        if TITLE_FABRIC in pairs:
            components.append(_lookup_required(rules.fabric_options, TITLE_FABRIC, pairs[TITLE_FABRIC]))
        if wall_only_kind == "full_wall":
            components.append(_full_wall_size_component(pairs, rules))
        components.append(_canopy_frame_size_component(pairs, rules))
        components.append(recipient_name)
        return [component for component in components if component]

    top_size = get_tent_top_size(asin)
    if top_size is None:
        raise MissingSizeRuleError(str(asin or ""))

    package_components: list[str] = [top_size]
    package_components.append(_tent_same_design_component(pairs))

    package_components.append(_frame_component(pairs, rules, destination_category=destination_category))

    package_components.extend(_wall_components(pairs, rules))

    if TITLE_FABRIC in pairs:
        package_components.append(_lookup_required(rules.fabric_options, TITLE_FABRIC, pairs[TITLE_FABRIC]))

    package_components.append(_accessory_component(TITLE_ROLLER_BAG, pairs, rules))
    package_components.append(_accessory_component(TITLE_SANDBAGS, pairs, rules))
    package_components.append(_accessory_component(TITLE_SANDBAGS_6PCS, pairs, rules))
    package_components.append(_accessory_component(TITLE_ROPE_STAKE, pairs, rules))
    package_components.append(_table_cloth_component(pairs, rules))
    package_components.append(_flag_component(pairs.get(TITLE_FLAG)))
    package_components.append(_canopy_frame_size_component(pairs, rules))

    if tent_quantity >= 2:
        components = [platform_order_no, _wrap_tent_package_components(tent_quantity, package_components), recipient_name]
        return [component for component in components if component]

    components: list[str] = [
        platform_order_no,
        f"{tent_quantity}个{top_size}",
        *package_components[1:],
    ]
    components.append(recipient_name)
    return [component for component in components if component]


def build_order_folder_components(
    *,
    platform_order_no: str,
    parent_asin: str | None,
    asin: str | None,
    tent_quantity: int,
    customization_text: str,
    recipient_name: str,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
    shipping_address_text: str | None = None,
) -> list[str]:
    """兼容旧调用：从完整文本解析 pairs 后再生成组件。"""

    return build_order_folder_components_from_pairs(
        platform_order_no=platform_order_no,
        parent_asin=parent_asin,
        asin=asin,
        tent_quantity=tent_quantity,
        pairs=parse_customization_pairs(customization_text),
        recipient_name=recipient_name,
        rules=rules,
        logistics=logistics,
        shipping_address_text=shipping_address_text,
    )


def create_order_folder(
    root: str | Path,
    folder_date: date,
    folder_name: str,
    *,
    create_folder: bool = True,
) -> FolderBuildResult:
    """创建订单文件夹并返回创建结果。"""
    daily_folder = build_daily_folder(root, folder_date)
    folder_path = daily_folder / folder_name
    if not create_folder:
        return FolderBuildResult(status="folder_preview", folder_path=str(folder_path), folder_name=folder_name)
    try:
        existed = folder_path.exists()
        folder_path.mkdir(parents=True, exist_ok=True)
        return FolderBuildResult(
            status="folder_exists" if existed else "folder_created",
            folder_path=str(folder_path),
            folder_name=folder_name,
        )
    except OSError as exc:
        return FolderBuildResult(status="folder_create_error", folder_path=str(folder_path), folder_name=folder_name, error=str(exc))


def _platform_order_no_from_preview(preview: FolderBuildResult) -> str | None:
    """从文件夹预览结果中提取平台单号。"""
    candidates = [*preview.folder_components, preview.folder_name or "", preview.folder_name_full or ""]
    for candidate in candidates:
        match = re.search(r"\b\d{3}-\d{7}-\d{7}\b", str(candidate or ""))
        if match:
            return match.group(0)
    return None


def create_order_folder_from_preview(
    preview: FolderBuildResult,
    *,
    create_folder: bool = True,
    platform_order_no: str | None = None,
) -> FolderBuildResult:
    """
    根据已确认的预览结果创建文件夹。

    业务上需要先把文件夹名打印给用户确认，再真正 mkdir；因此计算规则和
    文件系统写入分成两步，避免用户还没确认就创建了错误目录。
    """
    if preview.status not in SUCCESS_FOLDER_STATUSES:
        return preview
    if preview.status == FOLDER_EXISTING_PLATFORM_ORDER:
        return preview
    if not preview.folder_root or not preview.folder_date or not preview.folder_name:
        preview.status = "folder_create_error"
        preview.error = "folder preview missing root/date/name"
        return preview
    try:
        folder_date = date.fromisoformat(preview.folder_date)
    except ValueError:
        preview.status = "folder_invalid_payment_time"
        preview.error = f"invalid folder_date: {preview.folder_date}"
        return preview

    existing_platform_order = (platform_order_no or _platform_order_no_from_preview(preview) or "").strip()
    if existing_platform_order:
        existing_folder = find_existing_platform_order_folder(preview.folder_root, folder_date, existing_platform_order)
        if existing_folder:
            result = FolderBuildResult(
                status=FOLDER_EXISTING_PLATFORM_ORDER,
                folder_path=str(existing_folder),
                folder_name=existing_folder.name,
            )
            result.folder_root = preview.folder_root
            result.payment_time = preview.payment_time
            result.folder_date = preview.folder_date
            result.folder_date_source = preview.folder_date_source
            result.folder_components = list(preview.folder_components)
            result.folder_components_full = list(preview.folder_components_full)
            result.folder_name_full = preview.folder_name_full
            result.folder_name_was_shortened = preview.folder_name_was_shortened
            result.folder_name_removed_components = list(preview.folder_name_removed_components)
            result.folder_name_max_length = preview.folder_name_max_length
            result.full_folder_name_txt = preview.full_folder_name_txt
            result.customization_pairs = dict(preview.customization_pairs)
            result.folder_warnings = [*preview.folder_warnings, "existing_platform_order_folder_rechecked_before_create"]
            result.missing_rule_title = preview.missing_rule_title
            result.missing_rule_value = preview.missing_rule_value
            result.missing_rule_line = preview.missing_rule_line
            result.folder_name_truncated = preview.folder_name_truncated
            result.quantity_fallback = preview.quantity_fallback
            return result

    result = create_order_folder(
        preview.folder_root,
        folder_date,
        preview.folder_name,
        create_folder=create_folder,
    )
    result.folder_root = preview.folder_root
    result.payment_time = preview.payment_time
    result.folder_date = preview.folder_date
    result.folder_date_source = preview.folder_date_source
    result.folder_components = list(preview.folder_components)
    result.folder_components_full = list(preview.folder_components_full)
    result.folder_name_full = preview.folder_name_full
    result.folder_name_was_shortened = preview.folder_name_was_shortened
    result.folder_name_removed_components = list(preview.folder_name_removed_components)
    result.folder_name_max_length = preview.folder_name_max_length
    result.full_folder_name_txt = preview.full_folder_name_txt
    result.customization_pairs = dict(preview.customization_pairs)
    result.folder_warnings = list(preview.folder_warnings)
    result.missing_rule_title = preview.missing_rule_title
    result.missing_rule_value = preview.missing_rule_value
    result.missing_rule_line = preview.missing_rule_line
    result.folder_name_truncated = preview.folder_name_truncated
    result.quantity_fallback = preview.quantity_fallback
    return result


def _finalize_folder_result(
    *,
    order_item: BatchOrderItem,
    folder_root: str | Path,
    payment_time: Any,
    folder_date: date,
    folder_date_source: str,
    components: list[str],
    customization_pairs: dict[str, str],
    create_folder: bool,
    quantity_fallback: bool = False,
) -> FolderBuildResult:
    """补齐文件夹结果中的路径、组件和缩短信息。"""
    shorten_result = shorten_folder_name_by_components(components, FOLDER_NAME_MAX_LENGTH)
    if shorten_result.error:
        result = FolderBuildResult(status=shorten_result.error)
        result.folder_root = str(folder_root)
        result.payment_time = str(payment_time) if payment_time is not None else None
        result.folder_date = folder_date.isoformat()
        result.folder_date_source = folder_date_source
        result.folder_components = shorten_result.safe_components
        result.folder_components_full = shorten_result.full_components
        result.folder_name = shorten_result.safe_folder_name
        result.folder_name_full = shorten_result.full_folder_name
        result.folder_name_was_shortened = shorten_result.was_shortened
        result.folder_name_removed_components = shorten_result.removed_components
        result.folder_name_max_length = shorten_result.max_length
        result.customization_pairs = customization_pairs
        result.error = shorten_result.error
        return result
    folder_name = shorten_result.safe_folder_name
    existing_folder = find_existing_platform_order_folder(folder_root, folder_date, order_item.platform_order_no)
    if existing_folder:
        folder_result = FolderBuildResult(
            status=FOLDER_EXISTING_PLATFORM_ORDER,
            folder_path=str(existing_folder),
            folder_name=existing_folder.name,
        )
        folder_result.folder_warnings.append("existing_platform_order_folder")
    else:
        folder_result = create_order_folder(folder_root, folder_date, folder_name, create_folder=create_folder)
    folder_result.folder_root = str(folder_root)
    folder_result.payment_time = str(payment_time) if payment_time is not None else None
    folder_result.folder_date = folder_date.isoformat()
    folder_result.folder_date_source = folder_date_source
    folder_result.folder_components = shorten_result.safe_components
    folder_result.folder_components_full = shorten_result.full_components
    folder_result.folder_name_full = shorten_result.full_folder_name
    folder_result.folder_name_was_shortened = shorten_result.was_shortened
    folder_result.folder_name_removed_components = shorten_result.removed_components
    folder_result.folder_name_max_length = shorten_result.max_length
    folder_result.customization_pairs = customization_pairs
    folder_result.quantity_fallback = quantity_fallback
    folder_result.folder_name_truncated = shorten_result.was_shortened
    if quantity_fallback:
        folder_result.folder_warnings.append("quantity_fallback")
    return folder_result


def _base_result(
    *,
    status: str,
    folder_root: str | Path,
    payment_time: Any,
    folder_date: date | None = None,
    folder_date_source: str | None = None,
    customization_pairs: dict[str, str] | None = None,
    error: str | None = None,
    quantity_fallback: bool = False,
) -> FolderBuildResult:
    """构造默认结果对象，统一状态、错误和候选字段。"""
    return FolderBuildResult(
        status=status,
        folder_root=str(folder_root),
        payment_time=str(payment_time) if payment_time is not None else None,
        folder_date=folder_date.isoformat() if folder_date else None,
        folder_date_source=folder_date_source,
        customization_pairs=customization_pairs or {},
        error=error,
        quantity_fallback=quantity_fallback,
    )


def _line_has_required_customization(line: OrderFolderLine) -> bool:
    """判断商品行是否具备生成文件夹所需的定制数据。

    海报的核心命名信息来自 ASIN 和 Amazon QuantityOrdered，Proof/联系方式都是选填；
    因此海报允许 JSON pairs 为空。其它已迁移品类仍要求有 JSON pairs，避免误用空数据建错文件夹。
    """

    if line.product_type == PRODUCT_TYPE_POSTERS or is_poster_asin(line.asin):
        return True
    return bool(line.customization_pairs or line.customization_text.strip())


def _customization_line_label(index: int, title: str | None, value: str | None) -> str | None:
    """格式化定制化选项行，供缺失规则提示使用。"""
    title_text = str(title or "").strip()
    if not title_text:
        return None
    value_text = str(value or "").strip() or "missing"
    return f"{index}.{title_text} = {value_text}"


def build_and_create_order_folder_from_lines(
    *,
    order_item: BatchOrderItem,
    order_lines: list[OrderFolderLine],
    recipient_name: str | None,
    payment_time: str | datetime | date | None,
    folder_root: str | Path = DEFAULT_FOLDER_ROOT,
    override_date: str | date | datetime | None = None,
    create_folder: bool = True,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
    shipping_address_text: str | None = None,
) -> FolderBuildResult:
    """基于多条订单商品行生成同一个订单文件夹。"""

    effective_logistics = logistics if logistics is not None else order_item.logistics
    customization_pairs: dict[str, str] = {}
    for index, line in enumerate(order_lines, start=1):
        pairs = line.customization_pairs or parse_customization_pairs(line.customization_text)
        for title, value in pairs.items():
            customization_pairs[f"{index}.{title}"] = value
    try:
        folder_date = resolve_folder_date(payment_time, override_date)
    except ValueError as exc:
        status = "folder_missing_payment_time" if str(exc) == "missing_payment_time" else "folder_invalid_payment_time"
        return _base_result(
            status=status,
            folder_root=folder_root,
            payment_time=payment_time,
            customization_pairs=customization_pairs,
            error=str(exc),
        )
    folder_date_source = "override" if parse_folder_date_override(override_date) else "payment_time"
    if not order_lines:
        return _base_result(
            status="folder_missing_customization_text",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error="订单没有可用于文件夹生成的商品行。",
        )
    if any(not _line_has_required_customization(line) for line in order_lines):
        return _base_result(
            status="folder_missing_customization_text",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error="至少一个商品行缺少完整定制化文本。",
        )
    if not recipient_name or not recipient_name.strip():
        return _base_result(
            status="folder_missing_recipient_name",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
        )
    try:
        components = build_order_folder_components_from_lines(
            platform_order_no=order_item.platform_order_no,
            order_lines=order_lines,
            recipient_name=recipient_name.strip(),
            rules=rules,
            logistics=effective_logistics,
            shipping_address_text=shipping_address_text,
        )
    except (
        VinylBannerFolderError,
        PosterFolderError,
        TableRunnerFolderError,
        PopUpDisplayFolderError,
        RollUpBannerFolderError,
        XStandFolderError,
        FeatherFlagFolderError,
    ) as exc:
        result = _base_result(
            status=exc.status,
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
        )
        result.missing_rule_title = exc.title
        result.missing_rule_value = exc.value
        result.missing_rule_line = getattr(exc, "missing_rule_line", None)
        return result
    except MissingSizeRuleError as exc:
        return _base_result(
            status="folder_missing_size_rule",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
        )
    except FolderRuleMissingError as exc:
        result = _base_result(
            status="folder_rule_missing",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
        )
        result.missing_rule_title = exc.title
        result.missing_rule_value = exc.value
        result.missing_rule_line = getattr(exc, "missing_rule_line", None)
        return result
    except ValueError as exc:
        return _base_result(
            status="folder_rule_missing",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
        )

    return _finalize_folder_result(
        order_item=order_item,
        folder_root=folder_root,
        payment_time=payment_time,
        folder_date=folder_date,
        folder_date_source=folder_date_source,
        components=components,
        customization_pairs=customization_pairs,
        create_folder=create_folder,
        quantity_fallback=False,
    )


def build_and_create_order_folder(
    *,
    order_item: BatchOrderItem,
    contact_info: ContactInfo,
    recipient_name: str | None,
    payment_time: str | datetime | date | None,
    folder_root: str | Path = DEFAULT_FOLDER_ROOT,
    override_date: str | date | datetime | None = None,
    create_folder: bool = True,
    tent_quantity: int | None = None,
    quantity_fallback: bool = False,
    rules: OrderFolderRules = DEFAULT_ORDER_FOLDER_RULES,
    logistics: str | None = None,
    shipping_address_text: str | None = None,
) -> FolderBuildResult:
    """生成并按需创建订单文件夹。

    文件夹生成失败不能回滚电话邮箱写回；这里把业务错误转成稳定状态，
    让批量流程可以不加入 processed，后续继续重试文件夹。
    """
    effective_logistics = logistics if logistics is not None else order_item.logistics
    customization_text = contact_info.customization_text or ""
    customization_pairs = parse_customization_pairs(customization_text) if customization_text.strip() else {}
    try:
        folder_date = resolve_folder_date(payment_time, override_date)
    except ValueError as exc:
        status = "folder_missing_payment_time" if str(exc) == "missing_payment_time" else "folder_invalid_payment_time"
        return _base_result(
            status=status,
            folder_root=folder_root,
            payment_time=payment_time,
            customization_pairs=customization_pairs,
            error=str(exc),
            quantity_fallback=quantity_fallback,
        )
    folder_date_source = "override" if parse_folder_date_override(override_date) else "payment_time"
    if not customization_text.strip():
        return _base_result(
            status="folder_missing_customization_text",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            quantity_fallback=quantity_fallback,
        )
    if not recipient_name or not recipient_name.strip():
        return _base_result(
            status="folder_missing_recipient_name",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            quantity_fallback=quantity_fallback,
        )
    if not tent_quantity or tent_quantity <= 0:
        tent_quantity = 1
        quantity_fallback = True
    try:
        components = build_order_folder_components(
            platform_order_no=order_item.platform_order_no,
            parent_asin=order_item.parent_asin,
            asin=order_item.asin,
            tent_quantity=tent_quantity,
            customization_text=customization_text,
            recipient_name=recipient_name.strip(),
            rules=rules,
            logistics=effective_logistics,
            shipping_address_text=shipping_address_text,
        )
    except (
        VinylBannerFolderError,
        PosterFolderError,
        TableRunnerFolderError,
        PopUpDisplayFolderError,
        RollUpBannerFolderError,
        XStandFolderError,
        FeatherFlagFolderError,
    ) as exc:
        result = _base_result(
            status=exc.status,
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
            quantity_fallback=quantity_fallback,
        )
        result.missing_rule_title = exc.title
        result.missing_rule_value = exc.value
        return result
    except MissingSizeRuleError as exc:
        return _base_result(
            status="folder_missing_size_rule",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
            quantity_fallback=quantity_fallback,
        )
    except FolderRuleMissingError as exc:
        result = _base_result(
            status="folder_rule_missing",
            folder_root=folder_root,
            payment_time=payment_time,
            folder_date=folder_date,
            folder_date_source=folder_date_source,
            customization_pairs=customization_pairs,
            error=str(exc),
            quantity_fallback=quantity_fallback,
        )
        result.missing_rule_title = exc.title
        result.missing_rule_value = exc.value
        return result

    return _finalize_folder_result(
        order_item=order_item,
        folder_root=folder_root,
        payment_time=payment_time,
        folder_date=folder_date,
        folder_date_source=folder_date_source,
        components=components,
        customization_pairs=customization_pairs,
        create_folder=create_folder,
        quantity_fallback=quantity_fallback,
    )
