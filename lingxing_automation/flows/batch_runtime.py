from __future__ import annotations

import argparse
from typing import Any

from ..constants import DEFAULT_BATCH_INTERVAL_MINUTES


def get_batch_interval_seconds(args: argparse.Namespace) -> int:
    """计算批量巡检间隔，分钟参数优先，旧的小时参数继续兼容。"""
    minutes = getattr(args, "batch_interval_minutes", None)
    if minutes is not None:
        return max(60, int(float(minutes) * 60))

    hours = getattr(args, "batch_interval_hours", DEFAULT_BATCH_INTERVAL_MINUTES / 60)
    return max(60, int(float(hours) * 3600))


def print_batch_round_summary(payload: dict[str, Any]) -> None:
    """统一输出每轮巡检摘要，避免主流程里重复拼接日志。"""
    print(
        f"批量巡检完成：候选 {payload.get('candidate_count', 0)}，"
        f"写回 {payload.get('updated_count', 0)}，跳过 {payload.get('skipped_count', 0)}。"
    )
    if payload.get("status") == "error":
        print(f"批量巡检失败：{payload.get('message')}")
        if payload.get("screenshot_file"):
            print(f"截图：{payload['screenshot_file']}")


async def wait_before_next_round(page, args: argparse.Namespace) -> int:
    """一轮候选全部处理完成后，等待固定间隔再开始下一轮。"""
    sleep_seconds = get_batch_interval_seconds(args)
    print(f"等待 {sleep_seconds // 60} 分钟后开始下一轮。")
    await page.wait_for_timeout(sleep_seconds * 1000)
    return sleep_seconds
