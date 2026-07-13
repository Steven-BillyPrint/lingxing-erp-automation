from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import (
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LogisticsDetail,
    LogisticsReadinessDecision,
    ShipmentCandidate,
)


NOT_READY_LOGISTICS_STATUSES = {
    "待揽收",
    "已揽收",
    "已入库",
    "查询失败",
    "未出库",
    "货物抵达仓库",
    "订单关闭",
    "订单取消",
    "订单中止",
    "已核查",
    "已取消",
}

REQUIRED_READY_FIELDS = (
    "carrier",
    "international_tracking_no",
    "actual_total",
    "chargeable_weight_kg",
)

TAIL_TRACKING_FIELDS = ("carrier", "international_tracking_no")

REAL_OVERSEAS_CARRIER_DISPLAY_NAMES = {
    "UPS": "UPS",
    "FEDEX": "FedEx",
    "DHL": "DHL",
    "USPS": "USPS",
    "GOFO": "GOFO",
    "YANWEN": "Yanwen",
    "SPEEDX": "SpeedX",
    "UNIUNI": "UniUni",
    "1ST": "1ST",
    "SWIFTX": "SwiftX",
}

CARRIER_NAME_ALIASES = {
    "FEDERALEXPRESS": "FEDEX",
    "GOFOEXPRESS": "GOFO",
    "YANWENEXPRESS": "YANWEN",
    "SPEEDXEXPRESS": "SPEEDX",
    "UNI": "UNIUNI",
    "UNIEXPRESS": "UNIUNI",
    "SWIFTXEXPRESS": "SWIFTX",
    "1STGROUP": "1ST",
}

TRACKING_NUMBER_PATTERNS = {
    "FEDEX": (
        re.compile(r"\d{10}"),
        re.compile(r"\d{12}"),
        re.compile(r"\d{15}"),
        re.compile(r"\d{20}"),
        re.compile(r"\d{22}"),
    ),
    "UPS": (
        re.compile(r"1Z[A-Z0-9]{16}"),
        re.compile(r"\d{9}"),
        re.compile(r"\d{12}"),
        re.compile(r"\d{18}"),
        re.compile(r"\d{22,34}"),
        re.compile(r"T\d{10}"),
        re.compile(r"MI[A-Z0-9]{7,28}"),
    ),
    "DHL": (
        re.compile(r"\d{10}"),
        re.compile(r"\d{16}"),
        re.compile(r"(?:GM|LX|RX)[A-Z0-9]{10,30}"),
        re.compile(r"JJD[A-Z0-9]{10,32}"),
    ),
    "USPS": (
        re.compile(r"82\d{7}"),
        re.compile(r"\d{20,22}"),
        re.compile(r"[A-Z]{2}\d{9}US"),
    ),
    "GOFO": (
        re.compile(r"(?:GF(?:US)?|KD)[A-Z0-9]{10,20}"),
    ),
    "YANWEN": (
        re.compile(r"[A-Z]{2}\d{9}(?:YP|YW|CN)"),
        re.compile(r"(?:YWPT|YE|YT|SY|YL|LP)[A-Z0-9]{8,24}"),
    ),
    "SPEEDX": (
        re.compile(r"SPX[A-Z0-9]{12,22}"),
    ),
    "UNIUNI": (
        re.compile(r"(?:UNIA|UUSC|UUS|U00|UNPB|BAUNI|MB|JD|AS|AQ|JY)[A-Z0-9]{8,20}"),
    ),
    "1ST": (
        re.compile(r"1ST\d{8,20}"),
    ),
    "SWIFTX": (
        re.compile(r"SWX\d{18}"),
    ),
}

TRACKING_MISMATCH_REASON_PREFIX = "国际物流单号与承运商不匹配："

PAGE_ERROR_KEYWORDS = (
    "无权限",
    "没有权限",
    "暂无权限",
    "无数据",
    "暂无数据",
    "页面不可访问",
    "访问受限",
    "没有找到",
    "订单不存在",
)

RETRYABLE_PAGE_ERROR_KEYWORDS = (
    "等待阿里国际站物流详情页加载或登录完成超时",
    "登录完成超时",
    "target page, context or browser has been closed",
    "browsercontext.new_page",
    "page.wait_for_timeout",
    "browser has been closed",
    "context has been closed",
    "浏览器关闭",
    "timeout",
    "timed out",
)

DETAIL_LABELS = {
    "订单状态",
    "支付状态",
    "物流订单号",
    "服务类型",
    "服务线路",
    "仓库名称",
    "国际物流服务商",
    "国际物流单号",
    "国内物流服务商",
    "国内物流单号",
    "取件码",
    "预计到仓时间",
    "预计送达时间",
    "预计上门揽收时间",
}

SECTION_HEADERS = {
    "物流订单详情",
    "物流轨迹",
    "包裹和费用",
    "包裹信息",
    "预估包裹",
    "实际包裹",
    "费用明细",
    "预估费用信息",
    "实际费用信息",
    "其他详情",
    "收发货地址",
}

LOGISTICS_NO_RE = re.compile(r"ALS\s*(\d{11})", re.I)
WEIGHT_RE = re.compile(r"计费重\s*[\(（]\s*KG\s*[\)）]\s*([0-9]+(?:\.[0-9]+)?)", re.I)
MONEY_RE = re.compile(r"(?:[A-Z]{3}\s*)?\d+(?:\.\d+)?")


def logistics_detail_url(logistics_no: str) -> str:
    """Build the Alibaba logistics detail URL from a logistics number."""

    value = str(logistics_no or "").strip()
    if value.upper().startswith("ALS") and value[3:].isdigit():
        return f"https://scm.alibaba.com/luyou/express/detail.htm?id={int(value[3:])}"
    return f"https://scm.alibaba.com/luyou/express/detail.htm?id={value}"


def logistics_no_from_detail_url(url: str | None) -> str | None:
    parsed = urlparse(str(url or ""))
    id_values = parse_qs(parsed.query).get("id") or []
    if not id_values:
        return None
    value = str(id_values[0] or "").strip()
    if not value:
        return None
    if value.upper().startswith("ALS"):
        match = LOGISTICS_NO_RE.search(value)
        return f"ALS{match.group(1)}" if match else value.upper()
    if value.isdigit():
        return f"ALS{int(value):011d}"
    return value


def normalize_logistics_status(status_text: str | None) -> str:
    return " ".join(str(status_text or "").strip().split())


def is_not_ready_logistics_status(status_text: str | None) -> bool:
    return normalize_logistics_status(status_text) in NOT_READY_LOGISTICS_STATUSES


def normalize_carrier_name(carrier: str | None) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(carrier or "").upper())
    return CARRIER_NAME_ALIASES.get(normalized, normalized)


def normalize_tracking_number(tracking_no: str | None) -> str:
    return re.sub(r"[\s-]+", "", str(tracking_no or "").upper())


def is_real_overseas_carrier(carrier: str | None) -> bool:
    return normalize_carrier_name(carrier) in REAL_OVERSEAS_CARRIER_DISPLAY_NAMES


def tracking_number_matches_carrier(carrier: str | None, tracking_no: str | None) -> bool:
    carrier_key = normalize_carrier_name(carrier)
    normalized_tracking = normalize_tracking_number(tracking_no)
    if not normalized_tracking or not normalized_tracking.isalnum():
        return False
    patterns = TRACKING_NUMBER_PATTERNS.get(carrier_key)
    return bool(patterns and any(pattern.fullmatch(normalized_tracking) for pattern in patterns))


def tracking_number_mismatch_reason(carrier: str | None, tracking_no: str | None) -> str:
    return (
        f"{TRACKING_MISMATCH_REASON_PREFIX}"
        f"{normalize_carrier_name(carrier) or carrier or '-'} / {tracking_no or '-'}，"
        "请审核后选择处理方式。"
    )


def is_tracking_number_mismatch_reason(reason: str | None) -> bool:
    return str(reason or "").startswith(TRACKING_MISMATCH_REASON_PREFIX)


def parse_logistics_detail_from_text(
    text: str | None,
    *,
    source_url: str | None = None,
    fallback_logistics_no: str | None = None,
) -> LogisticsDetail:
    """Parse Alibaba logistics detail fields from visible page text."""

    raw_text = str(text or "")
    lines = _text_lines(raw_text)
    source_logistics_no = _first_logistics_no(raw_text) or logistics_no_from_detail_url(source_url) or fallback_logistics_no or ""
    mapping = _parse_label_value_blocks(lines)
    service_type = _clean(mapping.get("服务类型"))
    use_estimated = service_type == "快递门到门"
    amount = _section_label_value(
        lines,
        "预估费用信息" if use_estimated else "实际费用信息",
        "预估总额" if use_estimated else "实际总额",
    )
    chargeable_weight = _section_weight(
        lines,
        "预估包裹" if use_estimated else "实际包裹",
    )
    page_error = _detect_page_error(raw_text)

    international_tracking_no = _clean(mapping.get("国际物流单号")) or _tracking_no_from_timeline(lines)
    detail = LogisticsDetail(
        logistics_no=source_logistics_no,
        status_text=_clean(mapping.get("订单状态") or _value_after_label(lines, "订单状态")),
        service_type=service_type,
        carrier=_clean(mapping.get("国际物流服务商")),
        international_tracking_no=international_tracking_no,
        actual_total=_clean(amount),
        chargeable_weight_kg=_clean(chargeable_weight),
        package_count=_section_package_count(lines, "预估包裹" if use_estimated else "实际包裹"),
        source_url=source_url,
        page_error=page_error,
        raw={"line_count": len(lines), "source": "text"},
    )
    if not detail.page_error and not _has_any_detail_field(detail):
        detail.page_error = "页面结构完全无法识别，未读取到物流详情字段。"
    return detail


def parse_logistics_detail_from_json_payloads(
    payloads: list[Any],
    *,
    source_url: str | None = None,
    fallback_logistics_no: str | None = None,
) -> LogisticsDetail | None:
    """Best-effort fallback for XHR/JSON payloads by reusing the text parser."""

    for payload in payloads:
        strings = list(_json_strings(payload))
        if not strings:
            continue
        detail = parse_logistics_detail_from_text(
            "\n".join(strings),
            source_url=source_url,
            fallback_logistics_no=fallback_logistics_no,
        )
        if detail.status_text or detail.international_tracking_no or detail.page_error:
            detail.raw["source"] = "json"
            return detail
    return None


def parse_json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def logistics_readiness_decision(
    detail: LogisticsDetail,
    *,
    tracking_manually_verified: bool = False,
) -> LogisticsReadinessDecision:
    status_text = normalize_logistics_status(detail.status_text)
    if detail.page_error:
        if _is_retryable_page_error(detail.page_error):
            return LogisticsReadinessDecision(
                logistics_state=LOGISTICS_RETRYABLE,
                should_continue=False,
                reason=detail.page_error,
                status_text=status_text,
            )
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_BLOCKED,
            should_continue=False,
            reason=detail.page_error,
            status_text=status_text,
        )
    if not status_text:
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_BLOCKED,
            should_continue=False,
            reason="阿里物流详情缺少订单状态，需人工复核。",
            status_text=status_text,
        )
    if is_not_ready_logistics_status(status_text):
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_WAITING,
            should_continue=False,
            reason=f"阿里物流状态未就绪：{status_text}",
            status_text=status_text,
        )

    missing_tail_fields = [
        field_name for field_name in TAIL_TRACKING_FIELDS if not str(getattr(detail, field_name) or "").strip()
    ]
    if missing_tail_fields:
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_WAITING,
            should_continue=False,
            reason="缺少国际物流服务商或国际物流单号，下次继续查询。",
            status_text=status_text,
        )

    if not is_real_overseas_carrier(detail.carrier):
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_WAITING,
            should_continue=False,
            reason=f"国际物流服务商不是真实海外尾程承运商：{detail.carrier or '-'}，请人工确认。",
            status_text=status_text,
        )

    if (
        not tracking_manually_verified
        and not tracking_number_matches_carrier(detail.carrier, detail.international_tracking_no)
    ):
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_BLOCKED,
            should_continue=False,
            reason=tracking_number_mismatch_reason(detail.carrier, detail.international_tracking_no),
            status_text=status_text,
        )

    missing_fields = [
        field_name
        for field_name in REQUIRED_READY_FIELDS
        if not str(getattr(detail, field_name) or "").strip()
    ]
    if missing_fields:
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_BLOCKED,
            should_continue=False,
            reason=f"阿里物流状态可处理，但缺少物流字段：{', '.join(missing_fields)}",
            status_text=status_text,
        )

    return LogisticsReadinessDecision(
        logistics_state=LOGISTICS_READY,
        should_continue=True,
        reason="阿里物流详情已就绪。",
        status_text=status_text,
    )


def _is_retryable_page_error(value: str | None) -> bool:
    text = str(value or "").lower()
    return any(keyword.lower() in text for keyword in RETRYABLE_PAGE_ERROR_KEYWORDS)


def apply_logistics_detail_to_candidate(
    candidate: ShipmentCandidate,
    detail: LogisticsDetail,
) -> tuple[ShipmentCandidate, LogisticsReadinessDecision]:
    decision = logistics_readiness_decision(detail)
    updated = replace(
        candidate,
        carrier=detail.carrier,
        international_tracking_no=detail.international_tracking_no,
        actual_total=detail.actual_total,
        chargeable_weight_kg=detail.chargeable_weight_kg,
        package_count=detail.package_count,
    )
    return updated, decision


async def fetch_logistics_detail(*_args, **_kwargs) -> LogisticsDetail:
    raise NotImplementedError("Use shipment_automation.logistics_worker for phase-two logistics lookup.")


def _text_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_logistics_no(text: str) -> str | None:
    match = LOGISTICS_NO_RE.search(text)
    return f"ALS{match.group(1)}" if match else None


def _parse_label_value_blocks(lines: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    index = 0
    while index < len(lines):
        if lines[index] not in DETAIL_LABELS:
            index += 1
            continue

        labels: list[str] = []
        while index < len(lines) and lines[index] in DETAIL_LABELS:
            labels.append(lines[index])
            index += 1

        values: list[str] = []
        while index < len(lines) and len(values) < len(labels):
            value = lines[index]
            if value in DETAIL_LABELS or value in SECTION_HEADERS:
                break
            values.append(value)
            index += 1

        for label, value in zip(labels, values):
            mapping.setdefault(label, value)
    return mapping


def _value_after_label(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines):
        if line == label:
            for value in lines[index + 1 :]:
                if value and value not in DETAIL_LABELS:
                    return value
            return None
        if line.startswith(label):
            value = line[len(label) :].strip("：: \t")
            if value:
                return value
    return None


def _section_label_value(lines: list[str], section_name: str, label: str) -> str | None:
    start = _find_line(lines, section_name)
    if start < 0:
        return None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if index > start + 1 and line in SECTION_HEADERS and line != label:
            break
        if line == label:
            return _next_value(lines, index)
        if line.startswith(label):
            value = line[len(label) :].strip("：: \t")
            return value or _next_value(lines, index)
    return None


def _section_weight(lines: list[str], section_name: str) -> str | None:
    start = _find_line(lines, section_name)
    if start < 0:
        return None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if index > start + 1 and line in SECTION_HEADERS:
            break
        match = WEIGHT_RE.search(line)
        if match:
            return match.group(1)
    return None


def _section_package_count(lines: list[str], section_name: str) -> int | None:
    start = _find_line(lines, section_name)
    if start < 0:
        return None
    total = 0
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if index > start + 1 and line in SECTION_HEADERS:
            break
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if len(parts) >= 4 and parts[0].isdigit() and parts[-1].isdigit():
            total += int(parts[-1])
    return total or None


def _tracking_no_from_timeline(lines: list[str]) -> str | None:
    for line in lines:
        match = re.search(r"国际快递单号\s*([A-Za-z0-9-]+)", line)
        if match:
            return match.group(1)
    return None


def _find_line(lines: list[str], value: str) -> int:
    try:
        return lines.index(value)
    except ValueError:
        return -1


def _next_value(lines: list[str], index: int) -> str | None:
    for value in lines[index + 1 :]:
        if value and value not in DETAIL_LABELS:
            match = MONEY_RE.search(value)
            return match.group(0) if match else value
    return None


def _detect_page_error(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return "页面内容为空，未读取到物流详情。"
    for keyword in PAGE_ERROR_KEYWORDS:
        if keyword in stripped:
            return f"阿里物流详情页面异常：{keyword}"
    return None


def _has_any_detail_field(detail: LogisticsDetail) -> bool:
    return any(
        [
            detail.status_text,
            detail.service_type,
            detail.carrier,
            detail.international_tracking_no,
            detail.actual_total,
            detail.chargeable_weight_kg,
        ]
    )


def _json_strings(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _json_strings(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
        return
    if isinstance(value, (int, float)):
        yield str(value)
