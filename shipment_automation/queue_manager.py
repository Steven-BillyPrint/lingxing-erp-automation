from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from .models import (
    EMAIL_SENT,
    ERP_DONE,
    IDENTITY_ACTIVE,
    IDENTITY_CANCELLED,
    IDENTITY_CONFLICT,
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
)
from .queue_store import ShipmentWorkflowStore


InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


def run_interactive_queue_manager(
    store: ShipmentWorkflowStore,
    *,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> int:
    while True:
        rows = store.list_attention()
        _print_attention_list(rows, output_func)
        if not rows:
            output_func("当前没有需要人工处理的队列任务，返回主菜单。")
            return 0
        choice = _read(input_func, "请输入任务编号，输入 0 返回主菜单：")
        if choice is None or choice == "0":
            return 0
        try:
            selected_index = int(choice) - 1
            selected = rows[selected_index]
        except (ValueError, IndexError):
            output_func("编号无效，请重新选择。")
            continue
        if selected_index < 0:
            output_func("编号无效，请重新选择。")
            continue
        _manage_selected_job(store, selected["logistics_no"], input_func, output_func)


def _manage_selected_job(
    store: ShipmentWorkflowStore,
    logistics_no: str,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> None:
    while True:
        item = store.get_by_logistics_no(logistics_no)
        if not item:
            output_func(f"未找到物流单号：{logistics_no}")
            return
        _print_job_detail(item, output_func)
        _print_history(store, logistics_no, output_func)
        actions = _available_actions(item)
        output_func("\n可执行操作")
        for index, (_, label) in enumerate(actions, start=1):
            output_func(f"{index}. {label}")
        output_func("0. 返回任务列表")
        choice = _read(input_func, "请选择操作：")
        if choice is None or choice == "0":
            return
        try:
            action = actions[int(choice) - 1][0]
        except (ValueError, IndexError):
            output_func("操作编号无效。")
            continue
        if int(choice) <= 0:
            output_func("操作编号无效。")
            continue
        if _execute_action(store, item, action, input_func, output_func):
            return


def _available_actions(item: dict) -> list[tuple[str, str]]:
    actions: list[tuple[str, str]] = []
    mismatch_blocked = (
        item.get("logistics_state") == LOGISTICS_BLOCKED
        and "国际物流单号与承运商不匹配" in str(item.get("logistics_last_error") or "")
    )
    if mismatch_blocked:
        actions.append(("confirm-tracking", "人工确认当前承运商与国际物流单号"))
    if item.get("identity_state") == IDENTITY_ACTIVE and item.get("erp_state") != ERP_DONE:
        actions.append(("retry-logistics", "重新查询物流"))
    if (
        item.get("identity_state") == IDENTITY_ACTIVE
        and item.get("logistics_state") == LOGISTICS_READY
        and item.get("erp_state") != ERP_DONE
    ):
        actions.append(("retry-erp", "重新执行 ERP 标发"))
    if item.get("email_state") and item.get("email_state") != EMAIL_SENT:
        actions.append(("retry-email", "重新处理邮件"))
    if item.get("identity_state") == IDENTITY_CONFLICT:
        actions.append(("resolve-conflict", "解决订单归属冲突"))
    if item.get("identity_state") != IDENTITY_CANCELLED:
        actions.append(("cancel", "取消自动标发任务"))
    return actions


def _execute_action(
    store: ShipmentWorkflowStore,
    item: dict,
    action: str,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> bool:
    logistics_no = item["logistics_no"]
    if action == "confirm-tracking":
        preview = (
            "人工确认后该订单将允许进入 ERP。\n"
            f"承运商：{item.get('carrier') or '-'}\n"
            f"国际物流单号：{item.get('international_tracking_no') or '-'}\n"
            "该确认仅对以上承运商与单号组合有效。"
        )
        changed = _confirm_and_run(
            preview,
            lambda: store.confirm_tracking_override(logistics_no),
            input_func,
            output_func,
        )
    elif action == "retry-logistics":
        changed = _confirm_and_run(
            f"将 {logistics_no} 放回物流查询队列。",
            lambda: store.retry_stage(logistics_no, "logistics", reason="队列管理人工要求重新查询物流"),
            input_func,
            output_func,
        )
    elif action == "retry-erp":
        changed = _confirm_and_run(
            f"将 {logistics_no} 放回 ERP 标发队列。",
            lambda: store.retry_stage(logistics_no, "erp", reason="队列管理人工要求重试 ERP"),
            input_func,
            output_func,
        )
    elif action == "retry-email":
        changed = _confirm_and_run(
            f"将 {logistics_no} 对应的最新邮件批次放回待处理队列。",
            lambda: store.retry_email_for_logistics_no(logistics_no, reason="队列管理人工要求重试邮件"),
            input_func,
            output_func,
        )
    elif action == "resolve-conflict":
        system_order_no = _read(input_func, "请输入确认归属的系统单号：")
        platform_order_no = _read(input_func, "请输入确认归属的平台单号：")
        if not system_order_no or not platform_order_no:
            output_func("系统单号或平台单号为空，未修改。")
            return False
        changed = _confirm_and_run(
            f"物流单号：{logistics_no}\n系统单号：{system_order_no}\n平台单号：{platform_order_no}",
            lambda: store.resolve_conflict(logistics_no, system_order_no, platform_order_no),
            input_func,
            output_func,
        )
    elif action == "cancel":
        reason = _read(input_func, "请输入取消原因：")
        if not reason:
            output_func("取消原因为空，未修改。")
            return False
        changed = _confirm_and_run(
            f"将取消自动标发任务 {logistics_no}。\n原因：{reason}",
            lambda: store.cancel(logistics_no, reason),
            input_func,
            output_func,
        )
    else:
        output_func("不支持的操作。")
        return False
    output_func("操作成功，已刷新队列。" if changed else "状态未变化，请检查当前任务状态。")
    return changed


def _confirm_and_run(
    preview: str,
    operation: Callable[[], bool],
    input_func: InputFunc,
    output_func: OutputFunc,
) -> bool:
    output_func("\n操作预览")
    output_func(preview)
    answer = _read(input_func, "输入 y 确认执行，其他输入取消：")
    if str(answer or "").strip().lower() != "y":
        output_func("已取消，本次未修改队列。")
        return False
    return bool(operation())


def _print_attention_list(rows: list[dict], output_func: OutputFunc) -> None:
    output_func("\n需要人工处理的自动标发任务")
    output_func("编号 | 系统单号 | 平台单号 | 物流单号 | 物流状态 | ERP状态 | 邮件状态 | 原因")
    for index, item in enumerate(rows, start=1):
        output_func(
            f"{index} | {item.get('system_order_no') or '-'} | {item.get('platform_order_no') or '-'} | "
            f"{item.get('logistics_no') or '-'} | {item.get('logistics_state') or '-'} | "
            f"{item.get('erp_state') or '-'} | {item.get('email_state') or '-'} | "
            f"{item.get('last_error') or '-'}"
        )


def _print_job_detail(item: dict, output_func: OutputFunc) -> None:
    output_func("\n任务详情")
    fields = (
        ("系统单号", "system_order_no"),
        ("平台单号", "platform_order_no"),
        ("物流单号", "logistics_no"),
        ("承运商", "carrier"),
        ("国际物流单号", "international_tracking_no"),
        ("身份状态", "identity_state"),
        ("物流状态", "logistics_state"),
        ("ERP 状态", "erp_state"),
        ("ERP 检查点", "erp_checkpoint"),
        ("邮件状态", "email_state"),
        ("错误原因", "last_error"),
    )
    for label, key in fields:
        output_func(f"{label}：{item.get(key) or '-'}")


def _print_history(store: ShipmentWorkflowStore, logistics_no: str, output_func: OutputFunc) -> None:
    output_func("\n最近事件")
    events = [asdict(event) for event in store.history(logistics_no)][-10:]
    if not events:
        output_func("- 无事件")
        return
    for event in events:
        output_func(
            f"- {event.get('created_at') or '-'} | {event.get('stage') or '-'} | "
            f"{event.get('event_type') or '-'} | {event.get('message') or '-'}"
        )


def _read(input_func: InputFunc, prompt: str) -> str | None:
    try:
        return str(input_func(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        return None
