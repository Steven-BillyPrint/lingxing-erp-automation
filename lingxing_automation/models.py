from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def is_rule_missing_status(status: str | None) -> bool:
    """判断文件夹状态是否属于规则缺失，覆盖通用和各产品专用状态。"""

    text = str(status or "").strip().lower()
    return text == "folder_rule_missing" or text.endswith("_rule_missing") or "_rule_missing_" in text


def _normalize_missing_rule_text(value: Any) -> str:
    """规范化缺失规则文本，便于和定制化选项做稳定比较。"""
    return " ".join(str(value or "").strip().split()).lower()


def _strip_customization_pair_index(title: str) -> str:
    """去掉定制化标题前的序号前缀，还原真实选项标题。"""
    head, separator, tail = title.partition(".")
    if separator and head.isdigit() and tail:
        return tail
    return title


def find_missing_rule_line(
    customization_pairs: Mapping[str, Any] | None,
    title: str | None,
    value: str | None,
) -> tuple[str | None, bool]:
    """从 `1.Title = Value` 形式的定制 pairs 中定位触发 rule_missing 的原始行。"""

    title_text = str(title or "").strip()
    value_text = str(value or "").strip()
    if not title_text and not value_text:
        return None, False

    normalized_title = _normalize_missing_rule_text(title_text)
    normalized_value = _normalize_missing_rule_text(value_text)
    title_only_match: str | None = None
    for pair_title, pair_value in (customization_pairs or {}).items():
        pair_title_text = str(pair_title or "").strip()
        pair_value_text = str(pair_value or "").strip()
        if _normalize_missing_rule_text(_strip_customization_pair_index(pair_title_text)) != normalized_title:
            continue
        line = f"{pair_title_text} = {pair_value_text}"
        if not normalized_value or _normalize_missing_rule_text(pair_value_text) == normalized_value:
            return line, True
        title_only_match = title_only_match or line
    if title_only_match:
        return title_only_match, True
    if title_text and value_text:
        return f"{title_text} = {value_text}", False
    return title_text or value_text, False


def _line_exists_in_customization_pairs(line: str, customization_pairs: Mapping[str, Any] | None) -> bool:
    """判断指定缺失规则行是否存在于原始定制化选项中。"""
    if not customization_pairs:
        return False
    for pair_title, pair_value in customization_pairs.items():
        if line == f"{str(pair_title or '').strip()} = {str(pair_value or '').strip()}":
            return True
    return False


def format_rule_missing_lines(
    *,
    status: str | None,
    title: str | None = None,
    value: str | None = None,
    customization_pairs: Mapping[str, Any] | None = None,
    missing_rule_line: str | None = None,
    error: str | None = None,
) -> list[str]:
    """格式化规则缺失行。"""

    if not is_rule_missing_status(status):
        return []
    title_text = str(title or "").strip()
    value_text = str(value or "").strip()
    if title_text and value_text:
        return [f"缺少规则：{title_text} = {value_text}"]
    if title_text:
        return [f"缺少规则：{title_text}"]
    if value_text:
        return [f"缺少规则：{value_text}"]
    error_text = str(error or "").strip()
    return [f"缺少规则：{error_text}"] if error_text else []


@dataclass
class ContactInfo:
    phone: str | None
    email: str | None
    source_count: int
    source_excerpt: str
    customization_text: str | None = None

@dataclass
class SyncResult:
    system_order_no: str | None
    searched_order_no: str | None
    search_kind: str
    phone: str | None
    email: str | None
    dry_run: bool
    status: str
    message: str
    selected_search_type: str | None = None
    search_input_value: str | None = None
    search_validation_message: str | None = None
    system_order_nos: list[str] | None = None
    source_system_order_no: str | None = None
    updated_system_order_nos: list[str] | None = None
    update_messages: list[str] | None = None
    result_file: str | None = None
    screenshot_file: str | None = None
    folder_preview: dict[str, Any] | None = None
    folder_status: str | None = None
    folder_name: str | None = None
    folder_path: str | None = None
    custom_zip_status: str | None = None
    custom_zip_filename: str | None = None
    custom_zip_path: str | None = None
    custom_zip_candidates: list[str] | None = None
    custom_zip_open_method: str | None = None
    custom_zip_prepared_before_writeback: bool | None = None
    custom_zip_candidate_entries: list[dict[str, Any]] | None = None
    custom_zip_diagnostics: dict[str, Any] | None = None

@dataclass
class LoginConfig:
    account: str | None = None
    password: str | None = None
    remember_login: bool = True

    @property
    def has_credentials(self) -> bool:
        """判断登录配置是否同时包含账号和密码。"""
        return bool(self.account and self.password)

@dataclass
class BatchOrderItem:
    system_order_no: str
    platform_order_no: str
    row_text: str
    paid_at_text: str | None = None
    asin: str | None = None
    sku: str | None = None
    parent_asin: str | None = None
    product_type: str | None = None
    logistics: str | None = None
    tag_text: str | None = None
    source_page: int | None = None
    source_scroll_top: int | None = None
    matched_asins: list[str] = field(default_factory=list)
    all_asins: list[str] = field(default_factory=list)
    sales_revenue_total: str | None = None
    sales_revenue_currency: str | None = None
    sales_revenue_status: str = "missing"
    sales_revenue_source: str | None = None
    instruction_replaced_at: str | None = None
    instruction_customer_remark: str | None = None


@dataclass
class OrderCustomizationItem:
    """详情页中单个商品行对应的定制化文本。"""

    asin: str | None
    sku: str | None
    customization_text: str
    row_index: int | None = None


@dataclass
class OrderFolderLine:
    """用于生成文件夹名的单个订单商品行。"""

    asin: str | None
    sku: str | None
    parent_asin: str | None
    product_type: str | None
    quantity: int
    customization_text: str
    customization_pairs: dict[str, str] = field(default_factory=dict)
    order_item_id: str | None = None
    source_index: int | None = None


@dataclass
class CustomZipFile:
    """单个商品行下载到 staging 的定制化 zip 文件。"""

    row_index: int
    asin: str | None
    sku: str | None
    msku: str | None
    platform_order_no: str | None
    trigger_text: str | None
    zip_filename: str
    zip_path: str
    zip_candidates: list[str] = field(default_factory=list)
    order_item_id: str | None = None
    json_filename: str | None = None
    status: str = "custom_zip_downloaded"
    error: str | None = None


@dataclass
class CustomizationJsonInfo:
    """从 zip 内 JSON 解析出的单个 Amazon OrderItem 定制化信息。"""

    order_id: str
    order_item_id: str
    asin: str
    title: str | None
    quantity: int
    pairs: dict[str, str]
    raw_json_path: str | None = None
    source_zip_path: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class OrderCustomZipBundle:
    """一个平台单下所有商品行的定制化 zip 和 JSON 解析结果。"""

    platform_order_no: str
    zip_files: list[CustomZipFile] = field(default_factory=list)
    customization_items: list[CustomizationJsonInfo] = field(default_factory=list)
    status: str = "ok"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_log_dict(self) -> dict[str, Any]:
        """将当前对象转换为日志字典，便于批量流程记录和排查。"""
        return {
            "custom_zip_status": self.status,
            "custom_zip_count": len(self.zip_files),
            "custom_zip_error": self.error,
            "custom_zip_warnings": self.warnings,
            "custom_zip_files": [
                {
                    "row_index": item.row_index,
                    "asin": item.asin,
                    "sku": item.sku,
                    "zip_filename": item.zip_filename,
                    "zip_path": item.zip_path,
                    "order_item_id": item.order_item_id,
                    "json_filename": item.json_filename,
                    "status": item.status,
                    "error": item.error,
                }
                for item in self.zip_files
            ],
        }


@dataclass
class FolderNameShortenResult:
    """文件夹名缩短结果。"""

    full_folder_name: str
    safe_folder_name: str
    full_components: list[str]
    safe_components: list[str]
    removed_components: list[str]
    was_shortened: bool
    max_length: int
    error: str | None = None


@dataclass
class OrderFolderTask:
    platform_order_no: str
    system_order_no: str
    title: str
    folder_path: str | None = None
    status: str = "pending"
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomizationData:
    """订单定制化文本解析结果。"""

    raw_text: str
    pairs: dict[str, str]


@dataclass
class FolderBuildResult:
    """订单定制文件夹生成结果。"""

    status: str
    folder_root: str | None = None
    payment_time: str | None = None
    folder_date: str | None = None
    folder_date_source: str | None = None
    folder_name: str | None = None
    folder_name_full: str | None = None
    folder_path: str | None = None
    folder_components: list[str] = field(default_factory=list)
    folder_components_full: list[str] = field(default_factory=list)
    folder_name_was_shortened: bool = False
    folder_name_removed_components: list[str] = field(default_factory=list)
    folder_name_max_length: int | None = None
    full_folder_name_txt: str | None = None
    customization_pairs: dict[str, str] = field(default_factory=dict)
    folder_warnings: list[str] = field(default_factory=list)
    error: str | None = None
    missing_rule_title: str | None = None
    missing_rule_value: str | None = None
    missing_rule_line: str | None = None
    folder_name_truncated: bool = False
    quantity_fallback: bool = False

    def missing_rule_lines(self) -> list[str]:
        """生成当前文件夹结果对应的缺失规则提示行。"""
        return format_rule_missing_lines(
            status=self.status,
            title=self.missing_rule_title,
            value=self.missing_rule_value,
            customization_pairs=self.customization_pairs,
            missing_rule_line=self.missing_rule_line,
            error=self.error,
        )

    def resolved_missing_rule_line(self) -> str | None:
        """解析最适合展示给用户的缺失规则原始行。"""
        line, _line_from_pairs = find_missing_rule_line(
            self.customization_pairs,
            self.missing_rule_title,
            self.missing_rule_value,
        )
        return self.missing_rule_line or line

    def log_missing_rule_line(self) -> str | None:
        """生成适合写入批量日志的缺失规则原始行。"""
        if self.missing_rule_line:
            return self.missing_rule_line
        line, line_from_pairs = find_missing_rule_line(
            self.customization_pairs,
            self.missing_rule_title,
            self.missing_rule_value,
        )
        return line if line_from_pairs else None

    def to_log_dict(self) -> dict[str, Any]:
        """转换为批量日志字段，保留明确状态和排查信息。"""
        return {
            "folder_status": self.status,
            "folder_root": self.folder_root,
            "payment_time": self.payment_time,
            "folder_date": self.folder_date,
            "folder_date_source": self.folder_date_source,
            "folder_name": self.folder_name,
            "folder_name_full": self.folder_name_full,
            "folder_path": self.folder_path,
            "folder_components": self.folder_components,
            "folder_components_full": self.folder_components_full,
            "folder_name_was_shortened": self.folder_name_was_shortened,
            "folder_name_removed_components": self.folder_name_removed_components,
            "folder_name_max_length": self.folder_name_max_length,
            "full_folder_name_txt": self.full_folder_name_txt,
            "customization_pairs": self.customization_pairs,
            "folder_warnings": self.folder_warnings,
            "folder_error": self.error,
            "folder_missing_rule_title": self.missing_rule_title,
            "folder_missing_rule_value": self.missing_rule_value,
            "folder_missing_rule_line": self.log_missing_rule_line(),
            "folder_name_truncated": self.folder_name_truncated,
            "quantity_fallback": self.quantity_fallback,
        }


@dataclass
class CustomZipDownloadResult:
    """订单定制化 zip 下载结果。"""

    status: str
    zip_filename: str | None = None
    zip_path: str | None = None
    trigger_text: str | None = None
    zip_candidates: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    platform_order_no: str | None = None
    asin: str | None = None
    sku: str | None = None
    product_row_match: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    open_method: str | None = None
    prepared_before_writeback: bool = False
    zip_candidate_entries: list[dict[str, Any]] = field(default_factory=list)

    def _safe_candidate_entries(self) -> list[dict[str, Any]]:
        """定制文件 URL 可能带临时令牌，日志中必须脱敏。"""
        safe_entries: list[dict[str, Any]] = []
        for entry in self.zip_candidate_entries:
            safe_entry = dict(entry)
            for key in ("customized_url", "url", "href"):
                if key in safe_entry:
                    safe_entry[key] = "[redacted]"
            safe_entries.append(safe_entry)
        return safe_entries

    def to_log_dict(self) -> dict[str, Any]:
        """转换为批量/单订单日志字段，方便后续排查下载链路。"""
        return {
            "custom_zip_status": self.status,
            "custom_zip_filename": self.zip_filename,
            "custom_zip_path": self.zip_path,
            "custom_zip_trigger_text": self.trigger_text,
            "custom_zip_candidates": self.zip_candidates,
            "custom_zip_error": self.error,
            "custom_zip_warnings": self.warnings,
            "custom_zip_platform_order_no": self.platform_order_no,
            "custom_zip_asin": self.asin,
            "custom_zip_sku": self.sku,
            "custom_zip_product_row_match": self.product_row_match,
            "custom_zip_diagnostics": self.diagnostics,
            "custom_zip_open_method": self.open_method,
            "custom_zip_prepared_before_writeback": self.prepared_before_writeback,
            "custom_zip_candidate_entries": self._safe_candidate_entries(),
        }


@dataclass
class SkuDecision:
    status: str
    sku: str | None = None
    rule_id: str | None = None
    confidence: float = 0.0
    reason: str = ""
    review_required: bool = True


@dataclass
class SplitDecision:
    status: str
    should_split: bool
    rule_id: str | None = None
    target_orders: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    review_required: bool = True
