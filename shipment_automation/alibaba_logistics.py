from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence
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

FULL_ROUTE_SERVICE_LINES = (
    "FedEx-IP",
    "Express-HK-DL",
    "Express-HK Saver",
    "Express-IP",
    "Express-Saver",
    "Express-Expedited",
    "Express-HK Expedited",
    "FedEx-IE",
    "Express-IE",
    "UPS-Saver",
    "UPS-Expedited",
    "中国香港Express-Saver",
)

_SERVICE_LINE_DASHES_RE = re.compile(r"[-‐‑‒–—―﹘﹣－\s]+")


def normalize_service_line(value: object) -> str:
    """Normalize a service-line label for exact Alibaba route matching."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    while text.startswith("无忧"):
        text = text[2:].lstrip()
    return _SERVICE_LINE_DASHES_RE.sub("", text).casefold()


FULL_ROUTE_SERVICE_LINE_KEYS = frozenset(
    normalize_service_line(value) for value in FULL_ROUTE_SERVICE_LINES
)


def is_full_route_service_line(value: object) -> bool:
    normalized = normalize_service_line(value)
    return bool(normalized) and normalized in FULL_ROUTE_SERVICE_LINE_KEYS

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
    "YWE": "YANWEN",
    "SPEEDXEXPRESS": "SPEEDX",
    "UNI": "UNIUNI",
    "UNIEXPRESS": "UNIUNI",
    "SWIFTXEXPRESS": "SWIFTX",
    "1STGROUP": "1ST",
}

UNKNOWN_CARRIER_KEYS = frozenset({"UNKNOWN", "UNKNOW", "NA", "NONE", "NULL"})

TRACKING_NUMBER_PATTERNS = {
    "FEDEX": (
        re.compile(r"\d{10}"),
        re.compile(r"\d{12}"),
        re.compile(r"\d{14}"),
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
        re.compile(r"\d{7}"),
        re.compile(r"\d{9}"),
        re.compile(r"\d{10}"),
        re.compile(r"\d{11}"),
        re.compile(r"\d{16}"),
        re.compile(r"\d[A-Z]{2}\d{5}"),
        re.compile(r"[A-Z]{3}\d{6}"),
        re.compile(r"[A-Z]{5}\d{7}"),
        re.compile(r"(?:GM|LX|RX)[A-Z0-9]{8,37}"),
        re.compile(r"JJD[A-Z0-9]{10,32}"),
        re.compile(r"3S[A-Z0-9]{8,37}"),
        re.compile(r"[A-Z]{2}\d{9}[A-Z]{2}"),
    ),
    "USPS": (
        re.compile(r"82\d{7}"),
        re.compile(r"\d{20,22}"),
        re.compile(r"420\d{22,31}"),
        re.compile(r"[A-Z]{2}\d{9}US"),
    ),
    "GOFO": (
        re.compile(r"(?:GF(?:US)?|KD)[A-Z0-9]{10,20}"),
    ),
    "YANWEN": (
        re.compile(r"[A-Z]{2}\d{9}(?:YP|YW|CN)"),
        re.compile(r"YW(?:[A-Z]{2,3})?\d{8,12}"),
        re.compile(r"(?:YWPT|YE|YT|SY|YL|LP)[A-Z0-9]{8,24}"),
    ),
    "SPEEDX": (
        re.compile(r"SPX[A-Z0-9]{12,22}"),
    ),
    "UNIUNI": (
        re.compile(r"UR\d{17}"),
        re.compile(r"URB\d{16}"),
        re.compile(r"(?:UNIA|UUSC|UUS|U00|UNPB|BAUNI|MB|JD|AS|AQ|JY)[A-Z0-9]{8,20}"),
    ),
    "1ST": (
        re.compile(r"1ST\d{8,20}"),
    ),
    "SWIFTX": (
        re.compile(r"SWX\d{15,18}"),
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
    "nonetype' object has no attribute 'new_page",
    "浏览器关闭",
    "timeout",
    "timed out",
    "页面结构无法可靠识别",
    "物流字段组件存在歧义",
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
    "订单异常联系人",
}

TRACKING_UI_TEXT_LABELS = DETAIL_LABELS | {
    "联系人",
    "异常联系人",
    "国际货运跟踪号",
    "物流订单详情",
    "物流轨迹",
}

STRUCTURED_FIELD_LABELS = DETAIL_LABELS | {
    "订单异常联系人",
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
CHINESE_TEXT_RE = re.compile(r"[\u3400-\u9fff]")
ALS_TRACKING_PLACEHOLDER_RE = re.compile(r"ALS\d{11}", re.I)
JYCP_INTERMEDIARY_TRACKING_RE = re.compile(r"JYCP\d{8,20}", re.I)


@dataclass(frozen=True)
class LogisticsFieldGroup:
    """One self-contained Alibaba detail component and its semantic fields."""

    source: str
    group_id: str
    fields: Mapping[str, str]


@dataclass(frozen=True)
class TrackingCandidateDecision:
    """Classification of one value shown as an international tracking number."""

    category: str
    normalized: str | None
    usable: bool
    retryable: bool
    reason: str
    carrier_matches: bool | None = None


_DOM_FIELD_GROUP_SCRIPT = """
() => {
  const labels = new Set(%s);
  const clean = (value) => String(value || '').replace(/\u00a0/g, ' ').trim();
  const elementText = (element) => clean(element && (element.innerText || element.textContent));
  const groups = [];
  const seen = new Set();

  const addGroup = (source, groupId, fields) => {
    const cleaned = {};
    for (const [label, value] of Object.entries(fields || {})) {
      const normalizedLabel = clean(label);
      const normalizedValue = clean(value);
      if (!labels.has(normalizedLabel) || !normalizedValue || normalizedValue.length > 500) continue;
      if (!(normalizedLabel in cleaned)) cleaned[normalizedLabel] = normalizedValue;
    }
    const keys = Object.keys(cleaned).sort();
    if (!keys.length) return;
    const signature = JSON.stringify(keys.map((key) => [key, cleaned[key]]));
    if (seen.has(signature)) return;
    seen.add(signature);
    groups.push({ source, group_id: groupId, fields: cleaned });
  };

  Array.from(document.querySelectorAll('table')).forEach((table, tableIndex) => {
    const fields = {};
    let headers = [];
    const rows = Array.from(table.querySelectorAll('tr')).filter((row) => row.closest('table') === table);
    for (const row of rows) {
      const directCells = Array.from(row.children).filter((cell) => cell.tagName === 'TH' || cell.tagName === 'TD');
      const headerCells = directCells.filter((cell) => cell.tagName === 'TH');
      const valueCells = directCells.filter((cell) => cell.tagName === 'TD');
      if (headerCells.length) {
        headers = headerCells.map(elementText);
        continue;
      }
      if (!valueCells.length || !headers.length || valueCells.length !== headers.length) continue;
      valueCells.forEach((cell, index) => {
        const label = headers[index];
        const value = elementText(cell);
        if (labels.has(label) && value && !(label in fields)) fields[label] = value;
      });
    }
    addGroup('table', `table:${tableIndex}`, fields);
  });

  Array.from(document.querySelectorAll('dl')).forEach((list, listIndex) => {
    const fields = {};
    const children = Array.from(list.children);
    for (let index = 0; index < children.length; index += 1) {
      const item = children[index];
      if (item.tagName !== 'DT') continue;
      const label = elementText(item);
      const valueNode = children.slice(index + 1).find((candidate) => candidate.tagName === 'DD');
      const value = elementText(valueNode);
      if (labels.has(label) && value && !(label in fields)) fields[label] = value;
    }
    addGroup('definition_list', `dl:${listIndex}`, fields);
  });

  const readPairs = (nodes, fields) => {
    for (let index = 0; index + 1 < nodes.length; index += 1) {
      const label = elementText(nodes[index]);
      if (!labels.has(label)) continue;
      const value = elementText(nodes[index + 1]);
      if (value && !labels.has(value) && value.length <= 500 && !(label in fields)) fields[label] = value;
    }
  };
  const containers = Array.from(document.querySelectorAll('section,article,div,li'));
  containers.forEach((container, containerIndex) => {
    if (container.closest('table') || container.closest('dl')) return;
    const children = Array.from(container.children);
    if (children.length < 2 || children.length > 20) return;
    const fields = {};
    readPairs(children, fields);
    for (const child of children) {
      const grandchildren = Array.from(child.children);
      if (grandchildren.length >= 2 && grandchildren.length <= 12) readPairs(grandchildren, fields);
    }
    addGroup('label_value_card', `card:${containerIndex}`, fields);
  });

  return groups.slice(0, 100);
}
""" % json.dumps(sorted(STRUCTURED_FIELD_LABELS), ensure_ascii=False)


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


def classify_tracking_candidate(
    logistics_no: str | None,
    carrier: str | None,
    candidate: str | None,
) -> TrackingCandidateDecision:
    """Reject placeholders and visible UI prose before carrier validation."""

    value = str(candidate or "").strip()
    if not value:
        return TrackingCandidateDecision(
            category="missing",
            normalized=None,
            usable=False,
            retryable=True,
            reason="阿里页面尚未提供国际物流单号。",
        )

    normalized = normalize_tracking_number(value)
    expected = normalize_tracking_number(logistics_no)
    if (
        (expected and normalized == expected)
        or bool(ALS_TRACKING_PLACEHOLDER_RE.fullmatch(normalized))
    ):
        return TrackingCandidateDecision(
            category="placeholder",
            normalized=normalized,
            usable=False,
            retryable=True,
            reason="阿里页面的国际物流单号仍为 ALS 物流订单号，等待真实尾程单号。",
        )

    if JYCP_INTERMEDIARY_TRACKING_RE.fullmatch(normalized):
        return TrackingCandidateDecision(
            category="intermediary",
            normalized=normalized,
            usable=False,
            retryable=True,
            reason=f"阿里页面的国际物流单号仍为 {normalized}，等待真实尾程单号。",
        )

    if value in TRACKING_UI_TEXT_LABELS or CHINESE_TEXT_RE.search(value):
        return TrackingCandidateDecision(
            category="ui_text",
            normalized=normalized or None,
            usable=False,
            retryable=True,
            reason="国际物流单号位置显示的是页面文案，等待重新读取。",
        )

    if not normalized or not normalized.isalnum():
        return TrackingCandidateDecision(
            category="invalid",
            normalized=normalized or None,
            usable=False,
            retryable=True,
            reason="国际物流单号包含无法识别的字符，等待重新读取。",
        )

    carrier_matches = None
    if normalize_carrier_name(carrier) in REAL_OVERSEAS_CARRIER_DISPLAY_NAMES:
        carrier_matches = tracking_number_matches_carrier(carrier, value)
    return TrackingCandidateDecision(
        category="candidate",
        normalized=normalized,
        usable=True,
        retryable=False,
        reason="已读取国际物流单号候选值。",
        carrier_matches=carrier_matches,
    )


def is_obvious_tracking_parser_artifact(
    logistics_no: str | None,
    carrier: str | None,
    candidate: str | None,
) -> bool:
    return classify_tracking_candidate(logistics_no, carrier, candidate).category in {
        "placeholder",
        "intermediary",
        "ui_text",
    }


def is_real_overseas_carrier(carrier: str | None) -> bool:
    return normalize_carrier_name(carrier) in REAL_OVERSEAS_CARRIER_DISPLAY_NAMES


def is_unknown_carrier(carrier: str | None) -> bool:
    text = str(carrier or "").strip()
    return bool(text) and normalize_carrier_name(text) in UNKNOWN_CARRIER_KEYS


def tracking_number_matches_carrier(carrier: str | None, tracking_no: str | None) -> bool:
    carrier_key = normalize_carrier_name(carrier)
    normalized_tracking = normalize_tracking_number(tracking_no)
    if not normalized_tracking or not normalized_tracking.isalnum():
        return False
    patterns = TRACKING_NUMBER_PATTERNS.get(carrier_key)
    return bool(patterns and any(pattern.fullmatch(normalized_tracking) for pattern in patterns))


def infer_carrier_from_tracking_number(tracking_no: str | None) -> str | None:
    """只在运单特征唯一时推断真实尾程承运商。"""

    normalized = normalize_tracking_number(tracking_no)
    if not normalized or not normalized.isalnum():
        return None
    if re.fullmatch(r"1Z[A-Z0-9]{16}", normalized):
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES["UPS"]
    if re.fullmatch(r"[A-Z]{2}\d{9}US", normalized):
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES["USPS"]
    if re.fullmatch(r"82\d{7}", normalized):
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES["USPS"]
    if (
        re.fullmatch(r"\d{20,22}", normalized)
        and normalized.startswith(("92", "93", "94", "95"))
    ):
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES["USPS"]
    if normalized.startswith("420") and re.fullmatch(r"\d{25,34}", normalized):
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES["USPS"]

    matches = [
        carrier_key
        for carrier_key, patterns in TRACKING_NUMBER_PATTERNS.items()
        if any(pattern.fullmatch(normalized) for pattern in patterns)
    ]
    if len(matches) != 1:
        return None
    return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES[matches[0]]


def resolve_unknown_carrier(detail: LogisticsDetail) -> str | None:
    """用高置信度运单特征修复 Unknow/Unknown 承运商并保留审计信息。"""

    if not is_unknown_carrier(detail.carrier):
        return detail.carrier
    inferred = infer_carrier_from_tracking_number(detail.international_tracking_no)
    if not inferred:
        return detail.carrier
    raw = dict(detail.raw)
    raw["original_carrier"] = detail.carrier
    raw["carrier_inferred_from_tracking"] = True
    detail.raw = raw
    detail.carrier = inferred
    return inferred


def tracking_number_mismatch_reason(carrier: str | None, tracking_no: str | None) -> str:
    return (
        f"{TRACKING_MISMATCH_REASON_PREFIX}"
        f"{normalize_carrier_name(carrier) or carrier or '-'} / {tracking_no or '-'}，"
        "请审核后选择处理方式。"
    )


def is_tracking_number_mismatch_reason(reason: str | None) -> bool:
    return str(reason or "").startswith(TRACKING_MISMATCH_REASON_PREFIX)


async def extract_logistics_field_groups(page: Any) -> list[LogisticsFieldGroup]:
    """Read semantic field groups without relying on page-wide text positions."""

    payload = await page.evaluate(_DOM_FIELD_GROUP_SCRIPT)
    groups: list[LogisticsFieldGroup] = []
    if not isinstance(payload, list):
        return groups
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            continue
        raw_fields = item.get("fields")
        if not isinstance(raw_fields, Mapping):
            continue
        fields = {
            str(label).strip(): str(value).strip()
            for label, value in raw_fields.items()
            if str(label).strip() in STRUCTURED_FIELD_LABELS and str(value).strip()
        }
        if not fields:
            continue
        groups.append(
            LogisticsFieldGroup(
                source=str(item.get("source") or "dom").strip() or "dom",
                group_id=str(item.get("group_id") or f"group:{index}").strip()
                or f"group:{index}",
                fields=fields,
            )
        )
    return groups


def parse_logistics_detail_from_field_groups(
    groups: Sequence[LogisticsFieldGroup | Mapping[str, Any]],
    expected_logistics_no: str,
    *,
    source_url: str | None = None,
) -> LogisticsDetail:
    """Parse one identity-matched component; never combine unrelated components."""

    normalized_groups = _normalize_field_groups(groups)
    expected = normalize_tracking_number(expected_logistics_no)
    identity_groups = [
        group
        for group in normalized_groups
        if normalize_tracking_number(group.fields.get("物流订单号")) == expected
    ]
    groups_with_other_identity = [
        group
        for group in normalized_groups
        if group.fields.get("物流订单号")
        and normalize_tracking_number(group.fields.get("物流订单号")) != expected
    ]
    tail_groups = [
        group
        for group in normalized_groups
        if "国际物流服务商" in group.fields or "国际物流单号" in group.fields
    ]
    identity_tail_groups = [group for group in identity_groups if group in tail_groups]

    page_error: str | None = None
    selected: LogisticsFieldGroup | None = None
    candidates = identity_tail_groups or tail_groups or identity_groups
    unique_candidates = _dedupe_field_groups(candidates)
    if len(unique_candidates) == 1:
        selected = unique_candidates[0]
    elif len(unique_candidates) > 1:
        page_error = "物流字段组件存在歧义，无法可靠确定国际物流服务商和单号。"
    elif groups_with_other_identity and not identity_groups:
        page_error = "页面结构无法可靠识别：物流字段组件与当前物流订单号不一致。"
    elif not normalized_groups:
        page_error = "页面结构无法可靠识别，未找到独立物流字段组件。"

    selected_fields = dict(selected.fields) if selected is not None else {}
    candidate_value = selected_fields.get("国际物流单号")
    candidate_decision = classify_tracking_candidate(
        expected_logistics_no,
        selected_fields.get("国际物流服务商"),
        candidate_value,
    )
    raw = {
        "source": "dom_structured",
        "field_group_count": len(normalized_groups),
        "tail_group_count": len(tail_groups),
        "tracking_label_present": bool(
            selected is not None and "国际物流单号" in selected.fields
        ),
        "carrier_label_present": bool(
            selected is not None and "国际物流服务商" in selected.fields
        ),
        "tracking_candidate_class": candidate_decision.category,
        "tracking_candidate_reason": candidate_decision.reason,
    }
    if selected is not None:
        raw.update(
            {
                "selected_group_source": selected.source,
                "selected_group_id": selected.group_id,
                "selected_labels": sorted(selected.fields),
            }
        )

    return LogisticsDetail(
        logistics_no=str(expected_logistics_no or "").strip(),
        status_text=_clean(selected_fields.get("订单状态")) or "",
        service_type=_clean(selected_fields.get("服务类型")),
        service_line=_clean(selected_fields.get("服务线路")),
        carrier=_clean(selected_fields.get("国际物流服务商")),
        international_tracking_no=(
            _clean(candidate_value) if candidate_decision.usable else None
        ),
        source_url=source_url,
        page_error=page_error,
        raw=raw,
    )


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
    service_line = _clean(mapping.get("服务线路"))
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

    detail = LogisticsDetail(
        logistics_no=source_logistics_no,
        status_text=_clean(mapping.get("订单状态") or _value_after_label(lines, "订单状态")),
        service_type=service_type,
        service_line=service_line,
        carrier=None,
        international_tracking_no=None,
        actual_total=_clean(amount),
        chargeable_weight_kg=_clean(chargeable_weight),
        package_count=_section_package_count(lines, "预估包裹" if use_estimated else "实际包裹"),
        source_url=source_url,
        page_error=page_error,
        raw={
            "line_count": len(lines),
            "source": "text",
            "critical_tail_fields_ignored": True,
        },
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
    """Parse only explicit, identity-bound JSON objects.

    Flattening arbitrary JSON strings loses object boundaries and can turn an
    unrelated label or contact field into a tracking number.
    """

    expected = normalize_tracking_number(fallback_logistics_no)
    if not expected:
        return None
    candidates: list[LogisticsDetail] = []
    for payload in payloads:
        for item in _json_objects(payload):
            normalized = {_normalize_json_key(key): value for key, value in item.items()}
            identity = _first_json_value(
                normalized,
                "logisticsno",
                "logisticsorderno",
                "logisticsorderid",
            )
            if normalize_tracking_number(identity) != expected:
                continue
            carrier = _first_json_value(
                normalized,
                "internationalcarrier",
                "internationallogisticsprovider",
                "carriername",
                "carrier",
            )
            tracking_candidate = _first_json_value(
                normalized,
                "internationaltrackingno",
                "internationallogisticsno",
            )
            status_text = _first_json_value(normalized, "statustext", "logisticsstatus")
            service_type = _first_json_value(normalized, "servicetype")
            service_line = _first_json_value(
                normalized,
                "serviceline",
                "serviceroute",
                "logisticsserviceline",
            )
            if not any(
                (carrier, tracking_candidate, status_text, service_type, service_line)
            ):
                continue
            decision = classify_tracking_candidate(
                fallback_logistics_no,
                carrier,
                tracking_candidate,
            )
            candidates.append(
                LogisticsDetail(
                    logistics_no=str(fallback_logistics_no or "").strip(),
                    status_text=_clean(status_text) or "",
                    service_type=_clean(service_type),
                    service_line=_clean(service_line),
                    carrier=_clean(carrier),
                    international_tracking_no=(
                        _clean(tracking_candidate) if decision.usable else None
                    ),
                    source_url=source_url,
                    raw={
                        "source": "json_explicit",
                        "tracking_label_present": tracking_candidate is not None,
                        "carrier_label_present": carrier is not None,
                        "tracking_candidate_class": decision.category,
                        "tracking_candidate_reason": decision.reason,
                    },
                )
            )
    unique = _dedupe_json_details(candidates)
    if len(unique) == 1:
        return unique[0]
    return None


def merge_logistics_detail_sources(
    expected_logistics_no: str,
    *,
    text_detail: LogisticsDetail,
    structured_detail: LogisticsDetail,
    json_detail: LogisticsDetail | None = None,
) -> LogisticsDetail:
    """Merge sources while keeping critical tail fields structurally scoped."""

    structured_has_tracking_label = bool(
        structured_detail.raw.get("tracking_label_present")
    )
    structured_has_carrier_label = bool(
        structured_detail.raw.get("carrier_label_present")
    )
    json_has_tracking_label = bool(
        json_detail and json_detail.raw.get("tracking_label_present")
    )
    json_has_carrier_label = bool(
        json_detail and json_detail.raw.get("carrier_label_present")
    )

    if structured_has_tracking_label:
        tracking_no = structured_detail.international_tracking_no
        tracking_raw = structured_detail.raw
        tracking_source = "dom_structured"
    elif json_has_tracking_label and json_detail is not None:
        tracking_no = json_detail.international_tracking_no
        tracking_raw = json_detail.raw
        tracking_source = "json_explicit"
    else:
        tracking_no = None
        tracking_raw = structured_detail.raw
        tracking_source = "none"

    if structured_has_carrier_label:
        carrier = structured_detail.carrier
    elif json_has_carrier_label and json_detail is not None:
        carrier = json_detail.carrier
    else:
        carrier = None

    explicit_json_is_usable = bool(
        json_detail
        and json_has_tracking_label
        and json_has_carrier_label
        and not json_detail.page_error
    )
    service_line_values = [
        detail.service_line
        for detail in (structured_detail, text_detail, json_detail)
        if detail is not None and str(detail.service_line or "").strip()
    ]
    normalized_service_lines = {
        normalize_service_line(value) for value in service_line_values
    }
    service_line_conflict = len(normalized_service_lines) > 1

    page_error = text_detail.page_error
    if page_error is None and structured_detail.page_error and not explicit_json_is_usable:
        page_error = structured_detail.page_error
    if page_error is None and service_line_conflict:
        page_error = "阿里物流服务线路来源冲突，无法安全选择 ERP 物流渠道。"

    raw = {
        "source": "merged",
        "tail_source": tracking_source,
        "text": dict(text_detail.raw),
        "structured": dict(structured_detail.raw),
        "json": dict(json_detail.raw) if json_detail is not None else None,
        "tracking_candidate_class": tracking_raw.get("tracking_candidate_class", "missing"),
        "tracking_candidate_reason": tracking_raw.get(
            "tracking_candidate_reason",
            "阿里页面尚未提供国际物流单号。",
        ),
    }
    return LogisticsDetail(
        logistics_no=str(expected_logistics_no or "").strip(),
        status_text=(
            text_detail.status_text
            or structured_detail.status_text
            or (json_detail.status_text if json_detail is not None else "")
        ),
        service_type=(
            structured_detail.service_type
            or text_detail.service_type
            or (json_detail.service_type if json_detail is not None else None)
        ),
        service_line=(
            structured_detail.service_line
            or text_detail.service_line
            or (json_detail.service_line if json_detail is not None else None)
        ),
        carrier=carrier,
        international_tracking_no=tracking_no,
        actual_total=text_detail.actual_total or (
            json_detail.actual_total if json_detail is not None else None
        ),
        chargeable_weight_kg=text_detail.chargeable_weight_kg or (
            json_detail.chargeable_weight_kg if json_detail is not None else None
        ),
        package_count=text_detail.package_count or (
            json_detail.package_count if json_detail is not None else None
        ),
        source_url=text_detail.source_url or structured_detail.source_url,
        page_error=page_error,
        raw=raw,
    )


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

    resolve_unknown_carrier(detail)

    candidate_class = str(detail.raw.get("tracking_candidate_class") or "").strip()
    candidate_reason = str(detail.raw.get("tracking_candidate_reason") or "").strip()
    if candidate_class in {"placeholder", "intermediary", "ui_text", "invalid"}:
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_WAITING,
            should_continue=False,
            reason=candidate_reason or "阿里页面尚未提供真实国际物流单号，下次继续查询。",
            status_text=status_text,
        )

    if detail.international_tracking_no:
        candidate_decision = classify_tracking_candidate(
            detail.logistics_no,
            detail.carrier,
            detail.international_tracking_no,
        )
        if not candidate_decision.usable:
            return LogisticsReadinessDecision(
                logistics_state=LOGISTICS_WAITING,
                should_continue=False,
                reason=candidate_decision.reason,
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

    if is_unknown_carrier(detail.carrier):
        return LogisticsReadinessDecision(
            logistics_state=LOGISTICS_BLOCKED,
            should_continue=False,
            reason=(
                f"承运商为 {detail.carrier}，且无法根据运单号 "
                f"{detail.international_tracking_no or '-'} 唯一判断，请人工复核。"
            ),
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


def _normalize_field_groups(
    groups: Sequence[LogisticsFieldGroup | Mapping[str, Any]],
) -> list[LogisticsFieldGroup]:
    normalized: list[LogisticsFieldGroup] = []
    for index, group in enumerate(groups):
        if isinstance(group, LogisticsFieldGroup):
            raw_fields = group.fields
            source = group.source
            group_id = group.group_id
        elif isinstance(group, Mapping):
            raw_fields = group.get("fields")
            source = str(group.get("source") or "dom")
            group_id = str(group.get("group_id") or f"group:{index}")
        else:
            continue
        if not isinstance(raw_fields, Mapping):
            continue
        fields = {
            str(label).strip(): str(value).strip()
            for label, value in raw_fields.items()
            if str(label).strip() in STRUCTURED_FIELD_LABELS and str(value).strip()
        }
        if fields:
            normalized.append(
                LogisticsFieldGroup(
                    source=str(source or "dom").strip() or "dom",
                    group_id=str(group_id or f"group:{index}").strip() or f"group:{index}",
                    fields=fields,
                )
            )
    return normalized


def _dedupe_field_groups(groups: Sequence[LogisticsFieldGroup]) -> list[LogisticsFieldGroup]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[LogisticsFieldGroup] = []
    for group in groups:
        signature = tuple(sorted((str(key), str(value)) for key, value in group.fields.items()))
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(group)
    return unique


def _normalize_json_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _first_json_value(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def _json_objects(value: Any) -> Any:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_objects(item)


def _dedupe_json_details(details: Sequence[LogisticsDetail]) -> list[LogisticsDetail]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[LogisticsDetail] = []
    for detail in details:
        signature = (
            detail.status_text or "",
            detail.service_type or "",
            detail.service_line or "",
            detail.carrier or "",
            detail.international_tracking_no or "",
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(detail)
    return unique


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
            detail.service_line,
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
