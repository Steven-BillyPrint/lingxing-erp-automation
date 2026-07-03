from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path


class ChinaWorkdayError(ValueError):
    """中国工作日计算失败。"""


class ShippingDeadlineDateParseError(ChinaWorkdayError):
    """发货时限里没有可识别的年月日。"""


class PaymentTimeDateParseError(ChinaWorkdayError):
    """付款时间里没有可识别的年月日。"""


class ChinaWorkdayCalendarMissingError(ChinaWorkdayError):
    """缺少对应年份的中国大陆工作日表。"""


class ChinaWorkdayCalendarDataError(ChinaWorkdayError):
    """中国大陆工作日 JSON 数据格式错误。"""


@dataclass(frozen=True)
class ChinaWorkdayCalendar:
    """单年中国大陆工作日表。"""

    holidays: frozenset[date]
    adjusted_workdays: frozenset[date]


DATE_RE = re.compile(
    r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\b"
)
CHINA_WORKDAY_CALENDAR_PATH = Path(__file__).resolve().parents[2] / "data" / "china_workdays.json"


def _date_range(start: str, end: str) -> frozenset[date]:
    """生成起止日期之间的连续日期序列。"""
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    days: set[date] = set()
    current = first
    while current <= last:
        days.add(current)
        current += timedelta(days=1)
    return frozenset(days)


def parse_shipping_deadline_date(text: str | None) -> date:
    """从发货时限文本里只提取年月日，不使用脚本运行当天日期。"""

    value = str(text or "")
    match = DATE_RE.search(value)
    if not match:
        raise ShippingDeadlineDateParseError("无法从发货时限中解析日期")
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError as exc:
        raise ShippingDeadlineDateParseError(f"发货时限日期无效：{match.group(0)}") from exc
    _calendar_for_year(parsed.year)
    return parsed


def parse_payment_time_date(text: str | None) -> date:
    """从付款时间文本里只提取年月日，不使用脚本运行当天日期。"""

    return _parse_date_text(text, error_cls=PaymentTimeDateParseError, label="付款时间")


def _parse_date_text(text: str | None, *, error_cls: type[ChinaWorkdayError], label: str) -> date:
    value = str(text or "")
    match = DATE_RE.search(value)
    if not match:
        raise error_cls(f"无法从{label}中解析日期")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise error_cls(f"{label}日期无效：{match.group(0)}") from exc


def is_china_workday(day: date) -> bool:
    """按中国大陆节假日和调休规则判断某天是否工作日。"""

    calendar = _calendar_for_year(day.year)
    if day in calendar.adjusted_workdays:
        return True
    if day in calendar.holidays:
        return False
    return day.weekday() < 5


def subtract_china_workdays(start_day: date, workdays: int) -> date:
    """从起始日往前数指定数量的中国工作日，起始日本身按第 0 天处理。"""

    if workdays < 0:
        raise ValueError("workdays 不能为负数")
    _calendar_for_year(start_day.year)
    current = start_day
    remaining = workdays
    while remaining > 0:
        current -= timedelta(days=1)
        if is_china_workday(current):
            remaining -= 1
    return current


def build_instruction_customer_remark(shipping_deadline_text: str | None, *, workdays_before: int = 3) -> str:
    """生成帐篷说明书客服备注，例如 7.3发说明书。"""

    deadline = parse_shipping_deadline_date(shipping_deadline_text)
    remark_day = subtract_china_workdays(deadline, workdays_before)
    return f"{remark_day.month}.{remark_day.day}发说明书"


def build_expedited_instruction_customer_remark(payment_time_text: str | None) -> str:
    """加急订单按付款当天生成说明书客服备注。"""

    paid_day = parse_payment_time_date(payment_time_text)
    return f"{paid_day.month}.{paid_day.day}发说明书"


def _calendar_for_year(year: int) -> ChinaWorkdayCalendar:
    """加载指定年份的中国工作日历配置。"""
    calendar = _load_china_workday_calendars().get(year)
    if calendar is None:
        raise ChinaWorkdayCalendarMissingError(f"缺少 {year} 年中国大陆工作日表")
    return calendar


@lru_cache(maxsize=1)
def _load_china_workday_calendars() -> dict[int, ChinaWorkdayCalendar]:
    """从 JSON 加载工作日表；新增年份只需要维护 data/china_workdays.json。"""

    try:
        payload = json.loads(CHINA_WORKDAY_CALENDAR_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChinaWorkdayCalendarMissingError(f"缺少中国大陆工作日数据文件：{CHINA_WORKDAY_CALENDAR_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise ChinaWorkdayCalendarDataError(f"中国大陆工作日数据不是合法 JSON：{exc}") from exc

    calendars = payload.get("calendars") if isinstance(payload, dict) else None
    if not isinstance(calendars, dict):
        raise ChinaWorkdayCalendarDataError("中国大陆工作日数据缺少 calendars 对象")

    loaded: dict[int, ChinaWorkdayCalendar] = {}
    for year_text, year_payload in calendars.items():
        try:
            year = int(year_text)
        except (TypeError, ValueError) as exc:
            raise ChinaWorkdayCalendarDataError(f"年份键必须是数字：{year_text}") from exc
        if not isinstance(year_payload, dict):
            raise ChinaWorkdayCalendarDataError(f"{year} 年日历必须是对象")
        loaded[year] = ChinaWorkdayCalendar(
            holidays=_parse_date_entries(year_payload.get("holidays", []), year=year, field_name="holidays"),
            adjusted_workdays=_parse_date_entries(
                year_payload.get("adjusted_workdays", []),
                year=year,
                field_name="adjusted_workdays",
                allow_ranges=False,
            ),
        )
    return loaded


def _parse_date_entries(
    entries: object,
    *,
    year: int,
    field_name: str,
    allow_ranges: bool = True,
) -> frozenset[date]:
    """解析工作日历中的日期列表配置。"""
    if not isinstance(entries, list):
        raise ChinaWorkdayCalendarDataError(f"{year}.{field_name} 必须是数组")
    days: set[date] = set()
    for entry in entries:
        if isinstance(entry, str):
            days.add(_parse_json_date(entry, year=year, field_name=field_name))
            continue
        if allow_ranges and isinstance(entry, list) and len(entry) == 2 and all(isinstance(value, str) for value in entry):
            start_day = _parse_json_date(entry[0], year=year, field_name=field_name)
            end_day = _parse_json_date(entry[1], year=year, field_name=field_name)
            if end_day < start_day:
                raise ChinaWorkdayCalendarDataError(f"{year}.{field_name} 日期范围结束早于开始：{entry}")
            days.update(_date_range(start_day.isoformat(), end_day.isoformat()))
            continue
        raise ChinaWorkdayCalendarDataError(f"{year}.{field_name} 只能填写日期字符串或 [开始日期, 结束日期]")
    return frozenset(days)


def _parse_json_date(value: str, *, year: int, field_name: str) -> date:
    """解析JSON日期并返回结构化结果。"""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ChinaWorkdayCalendarDataError(f"{year}.{field_name} 日期格式必须是 YYYY-MM-DD：{value}") from exc
    if parsed.year != year:
        raise ChinaWorkdayCalendarDataError(f"{year}.{field_name} 日期年份不一致：{value}")
    return parsed
