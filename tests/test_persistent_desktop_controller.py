from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone

from erp_automation.configuration import (
    ConfigurationDecryptionError,
    EncryptedConfigurationStore,
    PortableEncryptedData,
    PortableMigrationService,
)
from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState
from erp_automation.ui import (
    Capability,
    CapabilityMode,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopInteractionOption,
    DesktopInteractionResponse,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LogLevel,
    InMemoryBackgroundTaskController,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    PersistentBackgroundTaskController,
    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)
from erp_automation.coordination.access import OperatorIdentity
from erp_automation.coordination.service import (
    CoordinatedControllerService,
    CoordinationSettings,
)
from erp_automation.coordination.store import CoordinationStore
from erp_automation.ui.models import (
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    notification_confirmation_order_no,
)


class LocalBackend:
    name = "test-local"

    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, plaintext: bytes, *, purpose: bytes) -> bytes:
        nonce = os.urandom(16)
        stream = hashlib.sha256(self.key + purpose + nonce).digest()
        body = bytes(value ^ stream[index % len(stream)] for index, value in enumerate(plaintext))
        return nonce + hmac.new(self.key, purpose + nonce + body, hashlib.sha256).digest() + body

    def decrypt(self, ciphertext: bytes, *, purpose: bytes) -> bytes:
        nonce, tag, body = ciphertext[:16], ciphertext[16:48], ciphertext[48:]
        expected = hmac.new(self.key, purpose + nonce + body, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ConfigurationDecryptionError("test authentication failed")
        stream = hashlib.sha256(self.key + purpose + nonce).digest()
        return bytes(value ^ stream[index % len(stream)] for index, value in enumerate(body))


class PortableBackend:
    name = "test-portable"

    @staticmethod
    def _key(passphrase: str, purpose: bytes) -> bytes:
        return hashlib.sha256(passphrase.encode() + purpose).digest()

    def encrypt(self, plaintext: bytes, passphrase: str, *, purpose: bytes) -> PortableEncryptedData:
        nonce = os.urandom(16)
        key = self._key(passphrase, purpose)
        stream = hashlib.sha256(key + nonce).digest()
        body = bytes(value ^ stream[index % len(stream)] for index, value in enumerate(plaintext))
        tag = hmac.new(key, purpose + nonce + body, hashlib.sha256).digest()
        return PortableEncryptedData(
            self.name,
            {"nonce": base64.b64encode(nonce).decode(), "tag": base64.b64encode(tag).decode()},
            body,
        )

    def decrypt(
        self,
        encrypted: PortableEncryptedData,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> bytes:
        nonce = base64.b64decode(encrypted.parameters["nonce"])
        tag = base64.b64decode(encrypted.parameters["tag"])
        key = self._key(passphrase, purpose)
        expected = hmac.new(key, purpose + nonce + encrypted.ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ConfigurationDecryptionError("test package authentication failed")
        stream = hashlib.sha256(key + nonce).digest()
        return bytes(
            value ^ stream[index % len(stream)]
            for index, value in enumerate(encrypted.ciphertext)
        )


def test_scheduled_scan_log_summaries_are_compact_and_keep_task_lookup() -> None:
    custom = PersistentBackgroundTaskController._scheduled_scan_summary(
        TaskCommand(
            "定制自动扫描",
            TaskArea.CUSTOMIZATION,
            Capability.LIST_ORDERS,
        ),
        {
            "candidate_count": 12,
            "buyer_cancel_reconciled_count": 2,
            "buyer_cancel_clear_observed_count": 1,
            "buyer_cancel_reactivated_count": 2,
            "folder_reconciled_completed_count": 3,
            "folder_reconciled_pending_count": 4,
            "folder_reconciliation_error_preserved_count": 2,
            "audit_log_path": "logs/api_scan/detail.json",
        },
        failed=False,
        task_id="custom-task-id",
    )
    shipment = PersistentBackgroundTaskController._scheduled_scan_summary(
        TaskCommand(
            "标发自动扫描",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
        ),
        {"candidate_count": 8, "enqueued_count": 5, "manual_review_count": 1},
        failed=False,
        task_id="shipment-task-id",
    )
    failed = PersistentBackgroundTaskController._scheduled_scan_summary(
        TaskCommand(
            "定制自动扫描",
            TaskArea.CUSTOMIZATION,
            Capability.LIST_ORDERS,
        ),
        {},
        failed=True,
        task_id="failed-task-id",
    )

    assert custom == (
        "定制订单后台扫描完成：候选 12，买家取消转不需要 2，"
        "取消撤销待再次确认 1，取消申请已撤销，订单已重新入队 2，"
        "消失候选文件夹对账：完成 3、待处理 4、保留报错 2。"
    )
    assert shipment == (
        "自动标发后台扫描完成：候选 8，新增队列 5，查询物流 0，"
        "可标发 0，需复核 1，待重试 0。"
    )
    assert "failed-task-id" in failed
    assert "详细扫描日志" in failed


def _controller(workspace, *, key=b"machine-one", **controller_kwargs):
    store = EncryptedConfigurationStore(
        workspace / "data/config.enc",
        backend=LocalBackend(key),
    )
    service = PortableMigrationService(backend=PortableBackend())
    return PersistentBackgroundTaskController(
        workspace,
        config_store=store,
        migration_service=service,
        **controller_kwargs,
    )


def test_summary_snapshot_does_not_materialize_legacy_queue_rows(
    tmp_path,
    monkeypatch,
) -> None:
    class ExplodingLegacyRow:
        def __deepcopy__(self, _memo):
            raise AssertionError("summary snapshot copied legacy queue rows")

    controller = _controller(tmp_path)
    controller._state.custom_orders = [ExplodingLegacyRow()]
    refresh_calls = 0
    original_refresh = controller._refresh_persistent_rows

    def record_refresh(*args, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(controller, "_refresh_persistent_rows", record_refresh)
    try:
        summary = controller.summary_snapshot()

        assert refresh_calls == 0
        assert summary.custom_orders == []
        assert summary.shipments == []

        controller._state.custom_orders = []
        controller.snapshot()
        assert refresh_calls == 1
    finally:
        controller.close()


def test_operator_controllers_own_and_reclaim_independent_executor_resources(
    tmp_path,
) -> None:
    first = _controller(
        tmp_path / "operator-a",
        key=b"operator-a",
    )
    second = _controller(
        tmp_path / "operator-b",
        key=b"operator-b",
    )
    lane_names = (
        "_executor",
        "_custom_scan_executor",
        "_shipment_scan_executor",
        "_notification_scan_executor",
        "_custom_order_executor",
        "_shipment_order_executor",
        "_shipment_logistics_executor",
        "_notification_executor",
        "_maintenance_executor",
    )

    first_executors = first._active_executors()
    second_executors = second._active_executors()
    try:
        assert len(first_executors) == 9
        assert len(second_executors) == 9
        assert all(
            getattr(first, name) is not getattr(second, name) for name in lane_names
        )
        assert first.save_settings(
            DesktopSettings(folder_root=r"Z:\Operator-A")
        ).accepted
        assert second.save_settings(
            DesktopSettings(folder_root=r"Z:\Operator-B")
        ).accepted
        assert first.snapshot().settings.folder_root == r"Z:\Operator-A"
        assert second.snapshot().settings.folder_root == r"Z:\Operator-B"

        first.close()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and any(
            executor._thread.is_alive() for executor in first_executors
        ):
            time.sleep(0.01)
        assert not any(executor._thread.is_alive() for executor in first_executors)
        assert second._executor.submit(lambda: "still-running").result(timeout=1) == (
            "still-running"
        )
    finally:
        first.close()
        second.close()


def test_idle_reclamation_closes_real_operator_threads_and_recreates_controller(
    tmp_path,
) -> None:
    operator_workspace = tmp_path / "operator-alice"
    created: list[PersistentBackgroundTaskController] = []

    def factory(_identity: OperatorIdentity) -> PersistentBackgroundTaskController:
        controller = _controller(
            operator_workspace,
            key=b"operator-alice",
        )
        created.append(controller)
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
        settings=CoordinationSettings(
            operator_controller_idle_seconds=60,
            monitor_interval_seconds=60,
        ),
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    service.register("alice-pc", "PC", identity=identity)
    service.snapshot_payload("alice-pc", identity=identity)
    first = created[0]
    first_executors = first._active_executors()
    assert first.save_settings(
        DesktopSettings(folder_root=r"Z:\Persisted-Alice")
    ).accepted
    service.store.deregister("alice-pc")
    service._operator_controller_last_used[identity.email] -= 61

    try:
        assert service._evict_idle_operator_controllers(set()) == 1
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and any(
            executor._thread.is_alive() for executor in first_executors
        ):
            time.sleep(0.01)
        assert not any(executor._thread.is_alive() for executor in first_executors)

        service.register("alice-pc-reconnected", "PC", identity=identity)
        replacement_snapshot = service.snapshot_payload(
            "alice-pc-reconnected",
            identity=identity,
        )
        assert len(created) == 2
        assert created[1] is not first
        assert replacement_snapshot["snapshot"]["settings"]["folder_root"] == (
            r"Z:\Persisted-Alice"
        )
        assert len(created[1]._active_executors()) == 9
        assert all(
            replacement is not original
            for replacement, original in zip(
                created[1]._active_executors(),
                first_executors,
                strict=True,
            )
        )
    finally:
        service.close()


def test_server_side_outbound_diagnostic_is_read_only_and_audited(tmp_path) -> None:
    controller = _controller(tmp_path)

    class _DiagnosticServices:
        async def diagnose_shipment_notification_outbound(
            self,
            settings,
            platform_order_no,
        ):
            assert settings == controller.snapshot().settings
            assert platform_order_no == "113-1753631-6040206-1"
            return {
                "platform_order_no": platform_order_no,
                "outbound_state": "WAITING",
                "wms_rows": [{"raw_status": 2}],
                "read_only": True,
            }

    controller.api_services = _DiagnosticServices()

    result = controller.diagnose_shipment_notification_outbound(
        "113-1753631-6040206-1"
    )

    assert result["read_only"] is True
    assert result["outbound_state"] == "WAITING"
    assert any(
        entry.source == "shipment_notification_diagnostic"
        and "未修改订单、通知队列或任务状态" in entry.message
        for entry in controller.snapshot().logs
    )


def test_server_side_outbound_diagnostic_rejects_invalid_identifier(tmp_path) -> None:
    controller = _controller(tmp_path)

    try:
        controller.diagnose_shipment_notification_outbound("../secret")
    except ValueError as exc:
        assert "平台单号格式无效" in str(exc)
    else:  # pragma: no cover - validation invariant
        raise AssertionError("invalid diagnostic identifier was accepted")


def test_application_log_query_filters_all_rows_before_paging(tmp_path) -> None:
    controller = _controller(tmp_path)
    for index in range(125):
        controller._append_log(  # noqa: SLF001 - durable logging contract
            LogLevel.INFO,
            "paging-test",
            f"paged-event-{index:03d}",
        )

    first = controller.list_log_entries(page=1, page_size=100, query="paged-event")
    second = controller.list_log_entries(page=2, page_size=100, query="paged-event")

    assert first.total == 125
    assert len(first.items) == 100
    assert len(second.items) == 25
    assert first.items[0].message == "paged-event-124"
    assert second.items[-1].message == "paged-event-000"


def test_manual_log_cleanup_supports_one_or_three_months_and_stays_in_logs(tmp_path) -> None:
    controller = _controller(tmp_path)
    old_log = tmp_path / "logs" / "shipment_scan" / "old.json"
    recent_log = tmp_path / "logs" / "shipment_scan" / "recent.json"
    business_file = tmp_path / "data" / "automation.sqlite3"
    old_log.parent.mkdir(parents=True, exist_ok=True)
    business_file.parent.mkdir(parents=True, exist_ok=True)
    old_log.write_text("old", encoding="utf-8")
    recent_log.write_text("recent", encoding="utf-8")
    business_file.write_text("business", encoding="utf-8")
    now = datetime.now(timezone.utc).timestamp()
    os.utime(old_log, (now - 45 * 86400, now - 45 * 86400))
    os.utime(recent_log, (now - 20 * 86400, now - 20 * 86400))

    result = controller.delete_logs_older_than(30)

    assert result.accepted is True
    assert result.details["retention_days"] == 30
    assert result.details["deleted_count"] == 1
    assert not old_log.exists()
    assert recent_log.exists()
    assert business_file.read_text(encoding="utf-8") == "business"
    assert not controller.delete_logs_older_than(60).accepted


def test_today_task_history_survives_controller_restart(tmp_path) -> None:
    first = _controller(tmp_path)
    task = TaskRecord(
        "today-task",
        "今日跨重启任务",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        status=TaskStatus.BLOCKED,
        message="需要人工处理",
    )
    first._write_task_snapshot(task)  # noqa: SLF001 - task journal contract

    second = _controller(tmp_path)
    snapshot = second.snapshot()

    restored = next(item for item in snapshot.today_tasks if item.task_id == task.task_id)
    assert restored.status is TaskStatus.BLOCKED
    assert restored.message == "需要人工处理"
    assert snapshot.tasks == []


def test_today_task_history_cache_updates_incrementally_without_reparsing(
    tmp_path,
    monkeypatch,
) -> None:
    controller = _controller(tmp_path)
    history_path = (
        tmp_path
        / "logs"
        / "app_events"
        / f"{datetime.now().astimezone():%Y-%m-%d}.jsonl"
    )
    path_type = type(history_path)
    original_read_text = path_type.read_text
    reads = 0

    def counted_read_text(path, *args, **kwargs):
        nonlocal reads
        if path == history_path:
            reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", counted_read_text)
    task = TaskRecord(
        "incremental-history-task",
        "增量历史任务",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        status=TaskStatus.SUCCEEDED,
        message="已完成",
    )

    controller._write_task_snapshot(task)  # noqa: SLF001 - cache contract
    first = controller.snapshot()
    second = controller.snapshot()

    assert reads == 0
    assert any(item.task_id == task.task_id for item in first.today_tasks)
    assert any(item.task_id == task.task_id for item in second.today_tasks)
    controller.close()


def test_notification_store_is_reused_across_queue_reads(tmp_path) -> None:
    controller = _controller(tmp_path)

    first_store, first_configuration = (
        controller._shipment_notification_context()  # noqa: SLF001
    )
    second_store, second_configuration = (
        controller._shipment_notification_context()  # noqa: SLF001
    )

    assert second_store is first_store
    assert second_configuration == first_configuration
    controller.close()


def test_interrupted_task_journal_raises_emergency_stop_and_review_lock(tmp_path) -> None:
    first = _controller(tmp_path)
    task = TaskRecord(
        "interrupted-task",
        "断电前任务",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_REMARK,
        status=TaskStatus.RUNNING,
        message="正在执行",
        order_no="RECOVERY-ORDER-1",
    )
    first._write_task_snapshot(task)  # noqa: SLF001 - crash journal contract
    first.close()

    second = _controller(tmp_path)
    snapshot = second.snapshot()
    restored = next(
        item for item in snapshot.today_tasks if item.task_id == task.task_id
    )

    assert restored.status is TaskStatus.PAUSED
    assert "意外中断" in restored.message
    assert restored.payload["_manual_review_lock"] is True
    assert snapshot.policy.execution_paused is False
    assert snapshot.policy.emergency_stop_writes is True
    second.attach_task_runner(lambda _command: {"status": "completed"})
    read_only = second.submit_task(
        TaskCommand("恢复前任务", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    )
    assert read_only.accepted is True
    duplicate = second.submit_task(
        TaskCommand(
            "重复写入",
            TaskArea.CUSTOMIZATION,
            Capability.UPDATE_REMARK,
            order_no="RECOVERY-ORDER-1",
        )
    )
    assert duplicate.accepted is False
    assert duplicate.details["manual_review_lock"] is True
    second.close()


def test_shared_operator_controller_does_not_recover_another_live_task_journal(
    tmp_path,
) -> None:
    first = _controller(tmp_path)
    first.set_emergency_stop_writes(False)
    task = TaskRecord(
        "other-operator-live-task",
        "其他操作员正在处理的任务",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        status=TaskStatus.RUNNING,
        message="正在执行",
        order_no="LIVE-ORDER-1",
    )
    first._write_task_snapshot(task)  # noqa: SLF001 - shared journal contract
    first.close()

    second = _controller(
        tmp_path,
        recover_interrupted_task_journal=False,
    )
    snapshot = second.snapshot()

    # Shared coordination mode decides whether a worker was orphaned from its
    # durable lease.  A lazily created per-operator controller must not infer
    # that every non-local journal entry was interrupted.
    assert snapshot.tasks == []
    assert snapshot.policy.emergency_stop_writes is False
    history = next(
        item for item in snapshot.today_tasks if item.task_id == task.task_id
    )
    assert history.status is TaskStatus.PAUSED
    assert "_manual_review_lock" not in history.payload
    second.close()


def _write_legacy_state(workspace) -> None:
    path = workspace / "data/processed_platform_orders.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "orders": {
                    "111-1111111-1111111": {
                        "platform_order_no": "111-1111111-1111111",
                        "system_order_no": "103700000000000001",
                        "product_type": "tent",
                        "contact_writeback_complete": True,
                        "folder_complete": True,
                        "sku_adjustment_required": False,
                        "sku_adjustment_complete": False,
                        "package_split_required": False,
                        "package_split_complete": False,
                        "instruction_remark_required": False,
                        "instruction_remark_complete": False,
                        "workflow_status": "completed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_command(
    order_no: str,
    *,
    area: TaskArea = TaskArea.CUSTOMIZATION,
    logistics_no: str = "",
) -> TaskCommand:
    action = (
        DesktopWriteAction.PROCESS_CUSTOM_ORDER
        if area is TaskArea.CUSTOMIZATION
        else DesktopWriteAction.EXECUTE_ERP_MARK
    )
    confirmation = DesktopWriteConfirmation.create(
        action,
        order_no,
        logistics_no=logistics_no,
    )
    capability = (
        Capability.UPDATE_CONTACT
        if area is TaskArea.CUSTOMIZATION
        else Capability.OUTBOUND_ORDER
    )
    return TaskCommand(
        "写入任务",
        area,
        capability,
        order_no=order_no,
        payload={
            "logistics_no": logistics_no,
            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
        },
    )


def _notification_write_command(notification_id: int) -> TaskCommand:
    order_no = notification_confirmation_order_no((notification_id,))
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
        order_no,
    )
    return TaskCommand(
        "发送客户通知",
        TaskArea.SHIPMENT,
        Capability.SEND_NOTIFICATION,
        order_no=order_no,
        payload={
            "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
            "notification_ids": [notification_id],
            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
        },
    )


def test_snapshot_uses_one_cached_custom_summary_query_until_database_changes(
    tmp_path, monkeypatch
):
    database = tmp_path / "data/automation.sqlite3"
    store = CustomWorkflowStore(database)
    store.mutate_legacy_record(
        "111-1111111-1111111",
        lambda _current: {
            "platform_order_no": "111-1111111-1111111",
            "contact_writeback_complete": False,
            "folder_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    calls = 0
    original = CustomWorkflowStore.list_workflow_summaries

    def counted(self, *, limit=2000):
        nonlocal calls
        calls += 1
        return original(self, limit=limit)

    monkeypatch.setattr(CustomWorkflowStore, "list_workflow_summaries", counted)
    monkeypatch.setattr(
        CustomWorkflowStore,
        "get_workflow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("desktop list refresh must not issue per-order detail queries")
        ),
    )
    controller = _controller(tmp_path)

    controller.snapshot()
    controller.snapshot()
    assert calls == 1

    store.set_stage_state(
        "111-1111111-1111111",
        "contact",
        WorkflowStageState.COMPLETED,
        reason="触发数据库变化",
    )
    controller.snapshot()
    assert calls == 2
    controller.close()


def test_empty_scan_candidates_do_not_replace_the_persistent_custom_queue(tmp_path):
    controller = _controller(tmp_path)
    store = CustomWorkflowStore(tmp_path / "data/automation.sqlite3")
    store.mutate_legacy_record(
        "111-1111111-1111111",
        lambda _current: {
            "platform_order_no": "111-1111111-1111111",
            "contact_writeback_complete": False,
            "folder_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    assert [row.platform_order_no for row in controller.snapshot().custom_orders] == [
        "111-1111111-1111111"
    ]

    controller._apply_task_payload(
        {
            "status": "completed",
            "candidate_count": 0,
            "custom_orders": [],
        }
    )

    assert [row.platform_order_no for row in controller.snapshot().custom_orders] == [
        "111-1111111-1111111"
    ]
    controller.close()


def test_settings_are_encrypted_and_repr_does_not_disclose_secrets(tmp_path):
    controller = _controller(tmp_path)
    settings = DesktopSettings(
        lingxing_app_id="app-id",
        lingxing_app_secret="secret-value",
        lingxing_password="web-password",
        alibaba_password="alibaba-password",
        alibaba_logistics_query_account="query@example.com",
        alibaba_logistics_query_password="query-password",
        amazon_lwa_client_secret="amazon-secret",
        amazon_refresh_token="refresh-secret",
        shipment_tag_name="客户待标发",
        custom_order_review_enabled=True,
        shipment_review_enabled=True,
    )

    result = controller.save_settings(settings)

    assert result.accepted
    encoded = (tmp_path / "data/config.enc").read_bytes()
    for secret in (
        b"secret-value",
        b"web-password",
        b"alibaba-password",
        b"query-password",
        b"amazon-secret",
        b"refresh-secret",
    ):
        assert secret not in encoded
        assert secret.decode() not in repr(settings)
    assert controller.snapshot().settings.payment_window_hours == 96
    assert controller.snapshot().settings.shipment_tag_name == "客户待标发"
    assert controller.snapshot().settings.custom_order_review_enabled is True
    assert controller.snapshot().settings.shipment_review_enabled is True
    assert controller.snapshot().settings.log_retention_days == 90


def test_env_import_and_json_to_sqlite_migration_are_visible(tmp_path):
    controller = _controller(tmp_path)
    (tmp_path / ".env").write_text(
        "LINGXING_ACCOUNT=user@example.com\nLINGXING_PASSWORD=plain-before-import\n",
        encoding="utf-8",
    )
    _write_legacy_state(tmp_path)

    assert controller.import_legacy_env(".env").accepted
    assert controller.run_migrations(dry_run=True).accepted
    assert controller.run_migrations(dry_run=False).accepted

    snapshot = controller.snapshot()
    assert snapshot.settings.lingxing_account == "user@example.com"
    assert [row.platform_order_no for row in snapshot.custom_orders] == ["111-1111111-1111111"]
    assert (tmp_path / "data/automation.sqlite3").exists()
    assert list((tmp_path / "data").glob("processed_platform_orders.json.pre_sqlite_*.bak"))


def test_cross_computer_package_reencrypts_config_and_moves_optional_state(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    source_controller = _controller(source, key=b"source-machine")
    source_controller.save_settings(
        DesktopSettings(lingxing_app_id="portable-app", lingxing_app_secret="portable-secret")
    )
    assert source_controller.set_emergency_stop_writes(False).accepted
    _write_legacy_state(source)
    assert source_controller.run_migrations(dry_run=False).accepted

    package = tmp_path / "move.erp-migrate"
    exported = source_controller.export_portable_migration(
        str(package),
        "a sufficiently long password",
        include_state=True,
    )
    assert exported.accepted
    assert b"portable-secret" not in package.read_bytes()

    destination_controller = _controller(destination, key=b"destination-machine")
    imported = destination_controller.import_portable_migration(
        str(package),
        "a sufficiently long password",
        overwrite=True,
    )

    assert imported.accepted
    snapshot = destination_controller.snapshot()
    assert snapshot.settings.lingxing_app_id == "portable-app"
    assert snapshot.settings.lingxing_app_secret == "portable-secret"
    assert snapshot.policy.emergency_stop_writes is True
    assert [row.platform_order_no for row in snapshot.custom_orders] == ["111-1111111-1111111"]
    assert b"portable-secret" not in (destination / "data/config.enc").read_bytes()


def test_portable_configuration_reports_safe_stats_and_readback_fingerprint(
    tmp_path,
):
    source = tmp_path / "source-summary"
    destination = tmp_path / "destination-summary"
    source.mkdir()
    destination.mkdir()
    source_controller = _controller(source, key=b"source-summary-machine")

    default_snapshot = source_controller.snapshot()
    assert default_snapshot.configuration_is_default is True
    assert default_snapshot.configured_secret_field_count == 0
    assert len(default_snapshot.configuration_fingerprint) == 64

    source_controller.save_settings(
        DesktopSettings(
            lingxing_app_id="portable-app",
            lingxing_app_secret="portable-secret",
        )
    )
    configured_snapshot = source_controller.snapshot()
    assert configured_snapshot.configuration_is_default is False
    assert configured_snapshot.configured_non_sensitive_field_count >= 1
    assert configured_snapshot.configured_secret_field_count == 1

    package = tmp_path / "settings-only.erp-migrate"
    exported = source_controller.export_portable_migration(
        str(package),
        "a sufficiently long password",
        include_state=False,
    )
    assert exported.accepted is True
    assert exported.details["configuration_fingerprint"] == (
        configured_snapshot.configuration_fingerprint
    )
    assert "portable-secret" not in repr(exported)

    destination_controller = _controller(
        destination,
        key=b"destination-summary-machine",
    )
    imported = destination_controller.import_portable_migration(
        str(package),
        "a sufficiently long password",
        overwrite=True,
        configuration_only=True,
    )
    imported_snapshot = destination_controller.snapshot()
    assert imported.accepted is True
    assert imported.details["configuration_fingerprint"] == (
        imported_snapshot.configuration_fingerprint
    )
    assert imported.details["configured_secret_field_count"] == 1
    assert imported_snapshot.settings.lingxing_app_id == "portable-app"
    assert imported_snapshot.settings.lingxing_app_secret == "portable-secret"


def test_desktop_state_changes_require_a_reason_and_are_audited(tmp_path):
    controller = _controller(tmp_path)
    _write_legacy_state(tmp_path)
    assert controller.run_migrations(dry_run=False).accepted

    rejected = controller.set_custom_stage_state(
        "111-1111111-1111111",
        "sku",
        "PENDING",
        reason="",
    )
    accepted = controller.reopen_custom_workflow(
        "111-1111111-1111111",
        "sku",
        reason="人工重新核验配件数量",
    )

    assert not rejected.accepted
    assert accepted.accepted
    history = controller._get_custom_store().history("111-1111111-1111111")
    assert any(event["reason"] == "人工重新核验配件数量" for event in history)


def test_desktop_can_batch_complete_custom_workflows_and_report_skips(tmp_path):
    controller = _controller(tmp_path)
    _write_legacy_state(tmp_path)
    assert controller.run_migrations(dry_run=False).accepted
    order_no = "111-1111111-1111111"
    store = controller._get_custom_store()
    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="等待人工处理",
    )
    store.set_stage_state(
        order_no,
        "folder",
        WorkflowStageState.BLOCKED,
        reason="等待人工处理",
        last_error="历史错误",
    )

    rejected = controller.complete_custom_workflows([order_no], reason="")
    accepted = controller.complete_custom_workflows(
        [order_no, order_no],
        reason="已在线下人工完成",
    )

    assert not rejected.accepted
    assert accepted.accepted
    assert "1 张订单标记为 completed" in accepted.message
    assert "2 个阶段" in accepted.message
    assert "未请求 ERP" in accepted.message
    snapshot_row = next(
        row for row in controller.snapshot().custom_orders if row.platform_order_no == order_no
    )
    assert snapshot_row.workflow_stage == "completed"
    repeated = controller.complete_custom_workflows([order_no], reason="再次确认")
    assert repeated.accepted
    assert "已经是 completed" in repeated.message
    history = store.history(order_no)
    assert sum(event["event_type"] == "workflow_manually_completed" for event in history) == 1
    controller.close()


def test_desktop_can_batch_change_and_reopen_custom_workflow_stages(tmp_path):
    controller = _controller(tmp_path)
    _write_legacy_state(tmp_path)
    assert controller.run_migrations(dry_run=False).accepted
    order_no = "111-1111111-1111111"

    changed = controller.set_custom_stage_states(
        [order_no, order_no],
        "contact",
        "BLOCKED",
        reason="人工批量阻止",
    )
    repeated = controller.set_custom_stage_states(
        [order_no],
        "contact",
        "BLOCKED",
        reason="重复确认",
    )
    reopened = controller.reopen_custom_workflows(
        [order_no, order_no],
        "contact",
        reason="人工批量重开",
    )

    assert changed.accepted
    assert "1 张订单" in changed.message
    assert "未请求 ERP" in changed.message
    assert repeated.accepted
    assert "未重复修改" in repeated.message
    assert reopened.accepted
    assert "1 张订单" in reopened.message
    assert "未请求 ERP" in reopened.message
    workflow = controller._get_custom_store().get_workflow(order_no)
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert stages["contact"]["state"] == "PENDING"
    history = controller._get_custom_store().history(order_no)
    assert any(event["reason"] == "人工批量阻止" for event in history)
    assert any(event["reason"] == "人工批量重开" for event in history)
    controller.close()


def test_runtime_policy_and_write_stop_are_persisted(tmp_path):
    first = _controller(tmp_path)
    assert first.snapshot().policy.emergency_stop_writes is True

    assert first.set_emergency_stop_writes(False).accepted
    assert first.update_capability_mode(Capability.UPDATE_REMARK, "disabled").accepted
    first.close()

    second = _controller(tmp_path)
    snapshot = second.snapshot()
    assert snapshot.policy.emergency_stop_writes is False
    assert snapshot.policy.configured_mode_for(Capability.UPDATE_REMARK).value == "disabled"
    second.close()


def test_contact_capability_normalizes_legacy_api_first_to_browser(tmp_path):
    controller = _controller(tmp_path)
    controller.set_emergency_stop_writes(False)

    result = controller.update_capability_mode(Capability.UPDATE_CONTACT, "api_first")

    assert result.accepted
    assert (
        controller.snapshot().policy.configured_mode_for(Capability.UPDATE_CONTACT)
        is CapabilityMode.BROWSER
    )
    controller.close()

    restored = _controller(tmp_path)
    assert (
        restored.snapshot().policy.configured_mode_for(Capability.UPDATE_CONTACT)
        is CapabilityMode.BROWSER
    )
    restored.close()


def test_persistent_controller_runs_one_background_task_and_updates_visible_rows(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        CustomWorkflowStore(tmp_path / "data/automation.sqlite3").mutate_legacy_record(
            "111-2222222-3333333",
            lambda _current: {
                "platform_order_no": "111-2222222-3333333",
                "system_order_no": "103700000000000099",
                "product_type": "tent",
                "contact_writeback_complete": False,
                "folder_complete": False,
            },
            event_type="test_scan_persisted",
            actor="test",
        )
        return {
            "status": "completed",
            "message": "API 扫描完成",
            "custom_orders": [
                {
                    "platform_order_no": "111-2222222-3333333",
                    "system_order_no": "103700000000000099",
                    "product_type": "tent",
                }
            ],
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    # The whole CI suite can temporarily delay the single worker thread even
    # though the controller is healthy. Keep this assertion bounded, but avoid
    # treating a short loaded-runner scheduling delay as a product regression.
    assert started.wait(5)
    assert controller.snapshot().tasks[0].status is TaskStatus.RUNNING

    future = controller._futures[submitted.task_id]
    release.set()
    future.result(timeout=5)
    snapshot = controller.snapshot()
    assert snapshot.tasks[0].status is TaskStatus.SUCCEEDED
    assert snapshot.custom_orders[0].platform_order_no == "111-2222222-3333333"
    controller.close()


def test_hidden_notification_compensation_runs_parallel_with_logistics_task(
    tmp_path,
) -> None:
    controller = _controller(tmp_path)
    logistics_started = threading.Event()
    release_logistics = threading.Event()
    compensation_started = threading.Event()

    def runner(command):
        if command.capability is Capability.ALIBABA_LOGISTICS:
            logistics_started.set()
            assert release_logistics.wait(3)
            return {"status": "completed", "message": "logistics complete"}
        if (
            command.capability is Capability.LIST_ORDERS
            and command.payload.get("trigger")
            == SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER
        ):
            compensation_started.set()
            return {"status": "completed", "message": "compensation complete"}
        raise AssertionError(f"unexpected command: {command}")

    controller.attach_task_runner(runner)
    logistics = controller.submit_task(
        TaskCommand(
            "查询阿里物流",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_LOGISTICS,
            order_no="111-LOGISTICS",
            payload={"logistics_no": "ALS-LOGISTICS"},
        )
    )
    assert logistics.accepted and logistics.task_id
    assert logistics_started.wait(2)

    compensation = controller.submit_task(
        TaskCommand(
            "客户通知增量补偿",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER},
        )
    )
    assert compensation.accepted and compensation.task_id
    assert compensation_started.wait(2)
    controller._futures[compensation.task_id].result(timeout=2)

    release_logistics.set()
    controller._futures[logistics.task_id].result(timeout=2)
    controller.close()


def test_post_scan_logistics_page_runs_parallel_with_other_visible_browser_workflow(
    tmp_path,
) -> None:
    controller = _controller(tmp_path)
    ordering_started = threading.Event()
    logistics_started = threading.Event()
    release = threading.Event()

    def runner(command):
        if command.capability is Capability.ALIBABA_ORDER_PREPARE:
            ordering_started.set()
        elif command.capability is Capability.ALIBABA_LOGISTICS:
            logistics_started.set()
        else:
            raise AssertionError(f"unexpected command: {command}")
        assert release.wait(3)
        return {"status": "completed", "message": "browser workflow complete"}

    controller.attach_task_runner(runner)
    ordering = controller.submit_task(
        TaskCommand(
            "准备阿里物流下单",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
        )
    )
    assert ordering.accepted and ordering.task_id
    assert ordering_started.wait(2)

    logistics = controller.submit_task(
        TaskCommand(
            "领星扫描后在本机查询阿里物流",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_LOGISTICS,
            payload={"trigger": "after_shipment_scan"},
        )
    )
    assert logistics.accepted and logistics.task_id
    assert logistics_started.wait(2)

    ordering_future = controller._futures[ordering.task_id]
    logistics_future = controller._futures[logistics.task_id]
    release.set()
    ordering_future.result(timeout=2)
    logistics_future.result(timeout=2)
    controller.close()


def test_custom_and_shipment_scans_run_in_parallel_lanes(tmp_path) -> None:
    controller = _controller(tmp_path)
    custom_started = threading.Event()
    shipment_started = threading.Event()
    release = threading.Event()

    def runner(command):
        if command.area is TaskArea.CUSTOMIZATION:
            custom_started.set()
        elif command.area is TaskArea.SHIPMENT:
            shipment_started.set()
        else:
            raise AssertionError(f"unexpected command: {command}")
        assert release.wait(3)
        return {"status": "completed", "message": "scan complete"}

    controller.attach_task_runner(runner)
    custom = controller.submit_task(
        TaskCommand("定制扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    shipment = controller.submit_task(
        TaskCommand("标发扫描", TaskArea.SHIPMENT, Capability.LIST_ORDERS)
    )

    assert custom.accepted and custom.task_id
    assert shipment.accepted and shipment.task_id
    assert custom_started.wait(2)
    assert shipment_started.wait(2)

    custom_future = controller._futures[custom.task_id]
    shipment_future = controller._futures[shipment.task_id]
    release.set()
    custom_future.result(timeout=2)
    shipment_future.result(timeout=2)
    controller.close()


def test_scans_and_three_business_workflows_all_run_in_parallel_lanes(
    tmp_path,
) -> None:
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    release = threading.Event()
    command_names = (
        "定制扫描",
        "标发扫描",
        "客户通知扫描",
        "定制订单处理",
        "自动标发处理",
        "客户通知处理",
    )
    started = {name: threading.Event() for name in command_names}

    def runner(command):
        started[command.name].set()
        assert release.wait(3)
        return {"status": "completed", "message": f"{command.name} complete"}

    controller.attach_task_runner(runner)
    custom_write = _write_command("111-custom-write")
    custom_write = TaskCommand(
        "定制订单处理",
        custom_write.area,
        custom_write.capability,
        payload=custom_write.payload,
        order_no=custom_write.order_no,
    )
    shipment_write = _write_command(
        "112-shipment-write",
        area=TaskArea.SHIPMENT,
        logistics_no="ALS-PARALLEL",
    )
    shipment_write = TaskCommand(
        "自动标发处理",
        shipment_write.area,
        shipment_write.capability,
        payload=shipment_write.payload,
        order_no=shipment_write.order_no,
    )
    notification_write = _notification_write_command(101)
    notification_write = TaskCommand(
        "客户通知处理",
        notification_write.area,
        notification_write.capability,
        payload=notification_write.payload,
        order_no=notification_write.order_no,
    )
    commands = (
        TaskCommand("定制扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS),
        TaskCommand(
            "标发扫描",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": "three_hour_timer"},
        ),
        TaskCommand(
            "客户通知扫描",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
        ),
        custom_write,
        shipment_write,
        notification_write,
    )

    submitted = [controller.submit_task(command) for command in commands]
    futures = [
        controller._futures[result.task_id]
        for result in submitted
        if result.task_id
    ]
    try:
        assert all(result.accepted and result.task_id for result in submitted)
        assert all(event.wait(2) for event in started.values())
    finally:
        release.set()
        for future in futures:
            future.result(timeout=2)
        controller.close()


def test_each_business_workflow_keeps_its_own_orders_serial(tmp_path) -> None:
    cases = (
        (
            "custom",
            _write_command("111-custom-first"),
            _write_command("112-custom-second"),
        ),
        (
            "shipment",
            _write_command(
                "111-shipment-first",
                area=TaskArea.SHIPMENT,
                logistics_no="ALS-FIRST",
            ),
            _write_command(
                "112-shipment-second",
                area=TaskArea.SHIPMENT,
                logistics_no="ALS-SECOND",
            ),
        ),
        (
            "notification",
            _notification_write_command(201),
            _notification_write_command(202),
        ),
    )

    for case_name, first_command, second_command in cases:
        controller = _controller(tmp_path / case_name)
        assert controller.set_emergency_stop_writes(False).accepted
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        invocation_count = 0
        invocation_lock = threading.Lock()

        def runner(_command):
            nonlocal invocation_count
            with invocation_lock:
                invocation_count += 1
                invocation = invocation_count
            if invocation == 1:
                first_started.set()
                assert release_first.wait(3)
            else:
                second_started.set()
                assert release_second.wait(3)
            return {"status": "completed", "message": "done"}

        controller.attach_task_runner(runner)
        first = controller.submit_task(first_command)
        second = controller.submit_task(second_command)
        first_future = controller._futures[first.task_id] if first.task_id else None
        second_future = controller._futures[second.task_id] if second.task_id else None
        try:
            assert first.accepted and first.task_id
            assert second.accepted and second.task_id
            assert first_started.wait(2)
            assert not second_started.wait(0.1)
            second_task = next(
                task
                for task in controller.snapshot().tasks
                if task.task_id == second.task_id
            )
            assert second_task.status is TaskStatus.QUEUED
            release_first.set()
            assert first_future is not None
            first_future.result(timeout=2)
            assert second_started.wait(2)
        finally:
            release_first.set()
            release_second.set()
            if first_future is not None:
                first_future.result(timeout=2)
            if second_future is not None:
                second_future.result(timeout=2)
            controller.close()


def test_background_task_waits_for_desktop_interaction_and_resumes(tmp_path):
    controller = _controller(tmp_path)

    def runner(command):
        import asyncio

        response = asyncio.run(
            controller.request_interaction(
                task_id=str(command.execution_id),
                stage="contact_writeback",
                title="写入联系方式前确认",
                message="sensitive transient details",
                options=(DesktopInteractionOption("candidate-1", "候选 1"),),
                display_data={"destination_postal_code": "SENSITIVE-POSTAL"},
            )
        )
        assert response.accepted
        assert response.selected_value == "candidate-1"
        return {"status": "completed", "message": "interaction complete"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("交互任务", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id

    deadline = time.monotonic() + 2
    requests = ()
    while time.monotonic() < deadline and not requests:
        requests = controller.pending_interactions()
        time.sleep(0.01)
    assert len(requests) == 1
    request = requests[0]
    assert request.display_data == {
        "destination_postal_code": "SENSITIVE-POSTAL"
    }
    task = next(item for item in controller.snapshot().tasks if item.task_id == submitted.task_id)
    assert task.status is TaskStatus.WAITING_USER
    future = controller._futures[submitted.task_id]

    responded = controller.respond_interaction(
        DesktopInteractionResponse(request.request_id, True, "candidate-1")
    )
    assert responded.accepted
    assert controller.pending_interactions() == ()
    future.result(timeout=2)
    task = next(item for item in controller.snapshot().tasks if item.task_id == submitted.task_id)
    assert task.status is TaskStatus.SUCCEEDED

    raw_events = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
    )
    assert "sensitive transient details" not in raw_events
    assert "SENSITIVE-POSTAL" not in raw_events
    controller.close()


def test_non_blocking_display_interaction_never_pauses_the_task(tmp_path):
    controller = _controller(tmp_path)

    def runner(command):
        import asyncio

        response = asyncio.run(
            controller.request_interaction(
                task_id=str(command.execution_id),
                stage="alibaba_order:quote_details",
                title="阿里查价资料已准备",
                message="transient details",
                display_data={"destination_postal_code": "N2R 1A6"},
                target_instance_id="desktop-a",
                non_blocking=True,
            )
        )
        assert response.accepted is True
        return {"status": "completed", "message": "quote details published"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("查价资料", TaskArea.SHIPMENT, Capability.ALIBABA_ORDER_PREPARE)
    )
    assert submitted.accepted and submitted.task_id
    controller._futures[submitted.task_id].result(timeout=2)

    task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    requests = controller.pending_interactions()
    assert task.status is TaskStatus.SUCCEEDED
    assert len(requests) == 1
    assert requests[0].non_blocking is True
    assert requests[0].target_instance_id == "desktop-a"

    responded = controller.respond_interaction(
        DesktopInteractionResponse(requests[0].request_id, True)
    )

    assert responded.accepted is True
    assert controller.pending_interactions() == ()
    raw_events = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
    )
    assert "N2R 1A6" not in raw_events
    controller.close()


def test_automatic_local_browser_action_stays_running_and_keeps_payload_ephemeral(
    tmp_path,
):
    controller = _controller(tmp_path)

    def runner(command):
        import asyncio

        response = asyncio.run(
            controller.request_interaction(
                task_id=str(command.execution_id),
                stage="alibaba_order:fill_local_browser",
                title="本机填写阿里草稿",
                message="transient action details",
                target_instance_id="desktop-a",
                automatic_action="alibaba_order_fill",
                action_payload={"password": "ephemeral-password"},
            )
        )
        assert response.result_data == {"route_name": "Express"}
        return {"status": "completed", "message": "local action complete"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("本机草稿", TaskArea.SHIPMENT, Capability.ALIBABA_ORDER_PREPARE)
    )
    assert submitted.accepted and submitted.task_id
    deadline = time.monotonic() + 2
    requests = ()
    while time.monotonic() < deadline and not requests:
        requests = controller.pending_interactions()
        time.sleep(0.01)
    assert len(requests) == 1
    task = next(
        item for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.RUNNING

    responded = controller.respond_interaction(
        DesktopInteractionResponse(
            requests[0].request_id,
            True,
            result_data={"route_name": "Express"},
        )
    )
    assert responded.accepted
    controller._futures[submitted.task_id].result(timeout=2)

    raw_events = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
    )
    assert "ephemeral-password" not in raw_events
    assert "transient action details" not in raw_events
    controller.close()


def test_prepare_close_rejects_waiting_interaction_and_pauses_task(tmp_path):
    controller = _controller(tmp_path)
    interaction_rejected = threading.Event()
    release_worker = threading.Event()

    def runner(command):
        import asyncio

        response = asyncio.run(
            controller.request_interaction(
                task_id=str(command.execution_id),
                stage="folder_creation",
                title="confirm",
                message="transient details",
            )
        )
        interaction_rejected.set()
        assert release_worker.wait(2)
        return {
            "status": "completed" if response.accepted else "cancelled",
            "message": "interaction resolved",
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("interaction", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not controller.pending_interactions():
        time.sleep(0.01)
    assert controller.pending_interactions()
    future = controller._futures[submitted.task_id]

    closing = controller.prepare_close()

    assert closing.accepted
    assert closing.details["immediate_non_atomic_paused"] == 1
    assert interaction_rejected.wait(1)
    assert not future.done()
    assert submitted.task_id not in controller._futures
    assert submitted.task_id in controller._abandoned_futures
    task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.PAUSED
    assert "立即暂停并释放占用" in task.message

    release_worker.set()
    future.result(timeout=2)
    task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.PAUSED
    assert controller.prepare_close().accepted
    controller.close()


def test_emergency_stop_rejects_waiting_write_interaction(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    observed: list[bool] = []

    def runner(command):
        import asyncio

        response = asyncio.run(
            controller.request_interaction(
                task_id=str(command.execution_id),
                stage="erp_mark:tracking",
                title="审核运单填写信息",
                message="transient waybill details",
            )
        )
        observed.append(response.accepted)
        return {
            "status": "completed" if response.accepted else "cancelled",
            "message": "interaction resolved",
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        _write_command(
            "waiting-waybill-review",
            area=TaskArea.SHIPMENT,
            logistics_no="ALS-WAITING-REVIEW",
        )
    )
    assert submitted.accepted and submitted.task_id
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not controller.pending_interactions():
        time.sleep(0.01)
    assert controller.pending_interactions()

    stopped = controller.set_emergency_stop_writes(True)

    assert stopped.accepted
    assert "拒绝 1 个等待中的写入确认" in stopped.message
    controller._futures[submitted.task_id].result(timeout=2)
    assert observed == [False]
    task = next(item for item in controller.snapshot().tasks if item.task_id == submitted.task_id)
    assert task.status is TaskStatus.CANCELLED
    controller.close()


def test_generated_task_id_flows_to_worker_and_success_log(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()
    observed_execution_ids: list[str | None] = []

    def runner(command):
        observed_execution_ids.append(command.execution_id)
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "任务链路成功。"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("扫描链路", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(2)
    future = controller._futures[submitted.task_id]
    release.set()
    future.result(timeout=2)

    snapshot = controller.snapshot()
    terminal_logs = [
        entry
        for entry in snapshot.logs
        if entry.task_id == submitted.task_id and entry.message == "任务链路成功。"
    ]
    task = next(item for item in snapshot.tasks if item.task_id == submitted.task_id)

    assert observed_execution_ids == [submitted.task_id]
    assert task.status is TaskStatus.SUCCEEDED
    assert len(terminal_logs) == 1
    assert terminal_logs[0].level.value == "INFO"
    queued_logs = [
        entry
        for entry in snapshot.logs
        if entry.task_id == submitted.task_id
        and entry.message == "任务“扫描链路”已进入后台任务队列。"
    ]
    assert len(queued_logs) == 1
    raw_events = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
    )
    assert "桌面骨架队列" not in raw_events
    queued_events = [
        json.loads(line)
        for line in raw_events.splitlines()
        if line.strip()
    ]
    assert any(
        event.get("task_id") == submitted.task_id
        and event.get("message") == "任务“扫描链路”已进入后台任务队列。"
        for event in queued_events
    )
    controller.close()


def test_generated_task_id_flows_to_worker_and_failure_log(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()
    observed_execution_ids: list[str | None] = []

    def runner(command):
        observed_execution_ids.append(command.execution_id)
        started.set()
        assert release.wait(2)
        raise RuntimeError("untrusted failure detail")

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("失败链路", TaskArea.SHIPMENT, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(2)
    future = controller._futures[submitted.task_id]
    release.set()
    future.result(timeout=2)

    snapshot = controller.snapshot()
    failure_logs = [
        entry
        for entry in snapshot.logs
        if entry.task_id == submitted.task_id and entry.message.startswith("后台任务失败：")
    ]
    task = next(item for item in snapshot.tasks if item.task_id == submitted.task_id)

    assert observed_execution_ids == [submitted.task_id]
    assert task.status is TaskStatus.FAILED
    assert len(failure_logs) == 1
    assert failure_logs[0].level.value == "ERROR"
    assert "untrusted failure detail" not in failure_logs[0].message
    controller.close()


def test_retry_scan_preserves_original_task_id_as_execution_id(tmp_path):
    controller = _controller(tmp_path)
    first_started = threading.Event()
    first_release = threading.Event()
    retry_started = threading.Event()
    retry_release = threading.Event()
    observed_execution_ids: list[str | None] = []

    def runner(command):
        observed_execution_ids.append(command.execution_id)
        if len(observed_execution_ids) == 1:
            first_started.set()
            assert first_release.wait(2)
            return {"status": "failed", "message": "首次扫描失败。"}
        retry_started.set()
        assert retry_release.wait(2)
        return {"status": "completed", "message": "重试扫描完成。"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("重试扫描链路", TaskArea.SHIPMENT, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    assert first_started.wait(2)
    first_future = controller._futures[submitted.task_id]
    first_release.set()
    first_future.result(timeout=2)
    assert next(
        item for item in controller.snapshot().tasks if item.task_id == submitted.task_id
    ).status is TaskStatus.FAILED

    retried = controller.retry_task(submitted.task_id)

    assert retried.accepted
    assert retry_started.wait(2)
    retry_future = controller._futures[submitted.task_id]
    retry_release.set()
    retry_future.result(timeout=2)

    snapshot = controller.snapshot()
    task = next(item for item in snapshot.tasks if item.task_id == submitted.task_id)
    assert observed_execution_ids == [submitted.task_id, submitted.task_id]
    assert task.status is TaskStatus.SUCCEEDED
    assert any(
        entry.task_id == submitted.task_id and entry.message == "重试扫描完成。"
        for entry in snapshot.logs
    )
    controller.close()


def test_active_worker_blocks_migration_import_and_manual_state_changes(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "done"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(2)

    assert not controller.run_migrations(dry_run=True).accepted
    assert not controller.save_settings(controller.snapshot().settings).accepted
    assert not controller.update_capability_mode(Capability.UPDATE_REMARK, "disabled").accepted
    assert not controller.import_legacy_env("missing.env").accepted
    assert not controller.import_portable_migration(
        str(tmp_path / "missing.erp-migrate"),
        "a sufficiently long password",
        overwrite=False,
    ).accepted
    assert not controller.set_custom_stage_state(
        "missing-order",
        "contact",
        "PENDING",
        reason="并发测试",
    ).accepted
    assert not controller.set_custom_stage_states(
        ["missing-order"],
        "contact",
        "PENDING",
        reason="并发测试",
    ).accepted
    assert not controller.complete_custom_workflows(
        ["missing-order"],
        reason="并发测试",
    ).accepted
    assert not controller.reopen_custom_workflows(
        ["missing-order"],
        "contact",
        reason="并发测试",
    ).accepted

    future = controller._futures[submitted.task_id]
    release.set()
    future.result(timeout=2)
    controller.close()


def test_emergency_stop_cancels_queued_writes_before_the_runner_starts(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def runner(command):
        calls.append(str(command.order_no))
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "done"}

    controller.attach_task_runner(runner)
    first = controller.submit_task(
        _write_command("first-order", area=TaskArea.SHIPMENT, logistics_no="LOG-1")
    )
    assert first.accepted and first.task_id
    assert started.wait(2)
    second = controller.submit_task(
        _write_command("second-order", area=TaskArea.SHIPMENT, logistics_no="LOG-2")
    )
    assert second.accepted and second.task_id

    stopped = controller.set_emergency_stop_writes(True)

    assert stopped.accepted
    second_task = next(task for task in controller.snapshot().tasks if task.task_id == second.task_id)
    assert second_task.status is TaskStatus.CANCELLED
    assert calls == ["first-order"]
    assert not controller.set_emergency_stop_writes(False).accepted
    first_future = controller._futures[first.task_id]
    release.set()
    first_future.result(timeout=2)
    assert calls == ["first-order"]
    controller.close()


def test_running_read_only_task_is_cancelled_immediately(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()

    def runner(command):
        started.set()
        deadline = time.monotonic() + 2
        while (
            time.monotonic() < deadline
            and not controller.cancellation_requested(str(command.execution_id or ""))
        ):
            time.sleep(0.01)
        return {
            "status": "cancelled",
            "message": "stopped after current safe step",
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand(
            "cooperative read",
            TaskArea.MAINTENANCE,
            Capability.LIST_ORDERS,
        )
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(1)

    cancelled = controller.cancel_task(submitted.task_id)

    assert cancelled.accepted is True
    assert cancelled.details["immediate_non_atomic_stop"] is True
    controller._futures[submitted.task_id].result(timeout=2)
    task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.CANCELLED
    controller.close()


def test_prepare_close_pauses_queued_work_and_waits_for_running_confirmed_write(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "write finished"}

    controller.attach_task_runner(runner)
    running = controller.submit_task(
        _write_command("running-order", area=TaskArea.SHIPMENT, logistics_no="LOG-1")
    )
    assert running.accepted and running.task_id
    assert started.wait(2)
    queued = controller.submit_task(
        _write_command("queued-order", area=TaskArea.SHIPMENT, logistics_no="LOG-2")
    )
    assert queued.accepted and queued.task_id

    closing = controller.prepare_close()

    assert not closing.accepted
    tasks = {task.task_id: task for task in controller.snapshot().tasks}
    assert tasks[queued.task_id].status is TaskStatus.PAUSED
    assert tasks[running.task_id].status is TaskStatus.STOPPING
    assert not controller.submit_task(TaskCommand(
        "late scan", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS
    )).accepted

    running_future = controller._futures[running.task_id]
    release.set()
    running_future.result(timeout=2)
    tasks = {task.task_id: task for task in controller.snapshot().tasks}
    assert tasks[running.task_id].status is TaskStatus.PAUSED
    assert controller.prepare_close().accepted
    controller.close()


def test_targeted_pause_migrates_unrelated_queued_task_to_replacement_lane(tmp_path):
    controller = _controller(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    second_completed = threading.Event()

    def runner(command):
        if command.name == "first read":
            first_started.set()
            assert release_first.wait(2)
            return {"status": "completed", "message": "late first result"}
        second_completed.set()
        return {"status": "completed", "message": "second completed"}

    controller.attach_task_runner(runner)
    first = controller.submit_task(
        TaskCommand("first read", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    )
    assert first.accepted and first.task_id
    assert first_started.wait(1)
    second = controller.submit_task(
        TaskCommand("second read", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    )
    assert second.accepted and second.task_id

    paused = controller.pause_tasks([first.task_id], "pause only first host task")

    assert paused.accepted is True
    assert paused.details["target_count"] == 1
    assert second_completed.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        tasks = {task.task_id: task for task in controller.snapshot().tasks}
        if tasks[second.task_id].status is TaskStatus.SUCCEEDED:
            break
        time.sleep(0.01)
    tasks = {task.task_id: task for task in controller.snapshot().tasks}
    assert tasks[first.task_id].status is TaskStatus.PAUSED
    assert tasks[second.task_id].status is TaskStatus.SUCCEEDED
    release_first.set()
    controller._abandoned_futures[first.task_id].result(timeout=2)
    assert next(
        task for task in controller.snapshot().tasks if task.task_id == first.task_id
    ).status is TaskStatus.PAUSED
    controller.close()


def test_targeted_pause_keeps_write_stopping_until_timeout_then_blocks(tmp_path):
    controller = _controller(tmp_path, pause_grace_seconds=0.05)
    assert controller.set_emergency_stop_writes(False).accepted
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "late write result"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        _write_command("uncertain-write", area=TaskArea.SHIPMENT, logistics_no="U-1")
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(1)
    old_future = controller._futures[submitted.task_id]

    paused = controller.pause_tasks([submitted.task_id], "stop this host")

    assert paused.accepted is True
    assert next(
        task for task in controller.snapshot().tasks if task.task_id == submitted.task_id
    ).status is TaskStatus.STOPPING
    assert submitted.task_id in controller._futures
    assert controller.snapshot().policy.emergency_stop_writes is False
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        task = next(
            task for task in controller.snapshot().tasks if task.task_id == submitted.task_id
        )
        if task.status is TaskStatus.BLOCKED:
            break
        time.sleep(0.01)
    task = next(
        task for task in controller.snapshot().tasks if task.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.BLOCKED
    assert "人工复核" in task.message
    assert submitted.task_id not in controller._futures
    assert controller._abandoned_futures[submitted.task_id] is old_future
    release.set()
    old_future.result(timeout=2)
    assert next(
        task for task in controller.snapshot().tasks if task.task_id == submitted.task_id
    ).status is TaskStatus.BLOCKED
    duplicate = controller.submit_task(
        _write_command("uncertain-write", area=TaskArea.SHIPMENT, logistics_no="U-1")
    )
    assert duplicate.accepted is False
    assert duplicate.details["manual_review_lock"] is True
    controller.close()


def test_local_pause_force_interrupts_and_fences_a_stuck_worker(tmp_path) -> None:
    controller = _controller(tmp_path, pause_grace_seconds=0.05)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "late result"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand("stuck read", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(1)
    old_future = controller._futures[submitted.task_id]

    paused = controller.set_execution_paused(True, "故障注入暂停。")

    assert paused.accepted
    deadline = time.monotonic() + 2
    task = None
    while time.monotonic() < deadline:
        task = next(
            item
            for item in controller.snapshot().tasks
            if item.task_id == submitted.task_id
        )
        if (
            task.status is TaskStatus.PAUSED
            and submitted.task_id in controller._abandoned_futures
        ):
            break
        time.sleep(0.01)
    assert task is not None and task.status is TaskStatus.PAUSED
    assert submitted.task_id not in controller._futures
    assert submitted.task_id in controller._abandoned_futures
    assert controller._retired_executors
    assert len(controller._retired_executors) == 1
    assert all(
        executor._thread.daemon
        for executor in controller._retired_executors
    )
    assert not controller.retry_task(submitted.task_id).accepted
    assert not controller.submit_task(
        TaskCommand("late task", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    ).accepted

    release.set()
    old_future.result(timeout=2)
    task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == submitted.task_id
    )
    assert task.status is TaskStatus.PAUSED
    assert task.message != "late result"

    resumed = controller.set_execution_paused(False)
    assert resumed.accepted
    followup = controller.submit_task(
        TaskCommand("fresh task", TaskArea.MAINTENANCE, Capability.LIST_ORDERS)
    )
    assert followup.accepted and followup.task_id
    controller._futures[followup.task_id].result(timeout=2)
    followup_task = next(
        item
        for item in controller.snapshot().tasks
        if item.task_id == followup.task_id
    )
    assert followup_task.status is TaskStatus.SUCCEEDED
    controller.close()


def test_local_pause_does_not_depend_on_policy_persistence(
    tmp_path,
    monkeypatch,
) -> None:
    controller = _controller(tmp_path, pause_grace_seconds=0.02)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "late result"}

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(
        TaskCommand(
            "persistence failure task",
            TaskArea.MAINTENANCE,
            Capability.LIST_ORDERS,
        )
    )
    assert submitted.accepted and submitted.task_id
    assert started.wait(1)
    old_future = controller._futures[submitted.task_id]

    def fail_persistence() -> None:
        raise OSError("configuration unavailable")

    monkeypatch.setattr(controller, "_persist_runtime_policy", fail_persistence)
    paused = controller.set_execution_paused(True, "persistence failure safety")

    assert paused.accepted is True
    assert paused.details["execution_paused"] is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = next(
            item
            for item in controller.snapshot().tasks
            if item.task_id == submitted.task_id
        )
        if task.status is TaskStatus.PAUSED:
            break
        time.sleep(0.01)
    assert task.status is TaskStatus.PAUSED

    release.set()
    old_future.result(timeout=2)
    controller.close()


def test_same_custom_order_cannot_be_queued_twice(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "done"}

    controller.attach_task_runner(runner)
    command = TaskCommand(
        "读取单个订单",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="duplicate-order",
    )
    first = controller.submit_task(command)
    assert first.accepted and first.task_id
    assert started.wait(2)
    duplicate = controller.submit_task(command)

    assert not duplicate.accepted
    assert "不能重复排队" in duplicate.message
    future = controller._futures[first.task_id]
    release.set()
    future.result(timeout=2)
    controller.close()


def test_same_shipment_logistics_cannot_be_queued_twice(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
        return {"status": "completed", "message": "done"}

    controller.attach_task_runner(runner)
    first = controller.submit_task(
        _write_command(
            "111-duplicate-shipment",
            area=TaskArea.SHIPMENT,
            logistics_no="ALS-DUPLICATE",
        )
    )
    assert first.accepted and first.task_id
    assert started.wait(2)

    duplicate = controller.submit_task(
        _write_command(
            "112-same-logistics",
            area=TaskArea.SHIPMENT,
            logistics_no="ALS-DUPLICATE",
        )
    )

    assert not duplicate.accepted
    assert "不能重复排队" in duplicate.message
    future = controller._futures[first.task_id]
    release.set()
    future.result(timeout=2)
    controller.close()


def test_failed_custom_order_does_not_stop_next_queued_order(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []

    def runner(command):
        order_no = str(command.order_no)
        calls.append(order_no)
        if order_no == "first-order":
            first_started.set()
            assert release_first.wait(2)
            return {"status": "cancelled", "message": "用户取消当前订单阶段。"}
        second_started.set()
        assert release_second.wait(2)
        return {"status": "completed", "message": "下一张订单完成。"}

    controller.attach_task_runner(runner)
    first = controller.submit_task(_write_command("first-order"))
    assert first.accepted and first.task_id
    assert first_started.wait(2)
    second = controller.submit_task(_write_command("second-order"))
    assert second.accepted and second.task_id

    release_first.set()
    controller._futures[first.task_id].result(timeout=2)
    assert second_started.wait(2)
    second_future = controller._futures[second.task_id]
    release_second.set()
    second_future.result(timeout=2)

    tasks = {task.order_no: task for task in controller.snapshot().tasks}
    assert calls == ["first-order", "second-order"]
    assert tasks["first-order"].status is TaskStatus.CANCELLED
    assert tasks["second-order"].status is TaskStatus.SUCCEEDED
    controller.close()


def test_shared_prerequisite_failure_blocks_remaining_queued_orders(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    def runner(command):
        order_no = str(command.order_no)
        calls.append(order_no)
        if order_no == "first-order":
            first_started.set()
            assert release_first.wait(2)
            return {
                "status": "failed",
                "message": "领星服务器浏览器需要完成一次设备验证。",
                "shared_prerequisite_error": "lingxing_browser_session",
            }
        return {"status": "completed", "message": "不应执行。"}

    controller.attach_task_runner(runner)
    first = controller.submit_task(_write_command("first-order"))
    assert first.accepted and first.task_id
    assert first_started.wait(2)
    second = controller.submit_task(_write_command("second-order"))
    assert second.accepted and second.task_id

    release_first.set()
    controller._futures[first.task_id].result(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        tasks = {task.order_no: task for task in controller.snapshot().tasks}
        if tasks["second-order"].status.terminal:
            break
        time.sleep(0.01)

    assert calls == ["first-order"]
    assert tasks["first-order"].status is TaskStatus.FAILED
    assert tasks["second-order"].status is TaskStatus.BLOCKED
    assert "共享前置条件不可用" in tasks["second-order"].message
    controller.close()


def test_uncertain_custom_write_is_pending_with_manual_review_lock(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted

    def runner(_command):
        return {
            "status": "manual_review",
            "message": "API 返回结果不明确，必须人工读回。",
            "workflow_paused_stage": "contact",
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(_write_command("uncertain-order"))
    assert submitted.accepted and submitted.task_id
    controller._futures[submitted.task_id].result(timeout=2)

    workflow = controller._get_custom_store().get_workflow("uncertain-order")
    assert workflow is not None
    assert workflow["workflow_status"] == "pending"
    contact = next(stage for stage in workflow["stages"] if stage["stage"] == "contact")
    assert contact["state"] == "PENDING"
    assert "人工读回" in contact["last_error"]
    assert controller._get_custom_store().get_pending_retry_review("uncertain-order")["stage"] == "contact"

    retry = controller.submit_task(_write_command("uncertain-order"))
    assert retry.accepted
    controller._futures[retry.task_id].result(timeout=2)
    controller.close()


def test_desktop_snapshot_includes_customer_shipping_service_scan_error(tmp_path):
    from shipment_automation.queue_store import ShipmentWorkflowStore

    controller = _controller(tmp_path)
    store = ShipmentWorkflowStore(controller._shipment_state_path())
    store.reconcile_customer_shipping_service_scan_issues(
        [
            {
                "system_order_no": "103000000000009901",
                "platform_order_no": "112-0000000-0009901",
                "shipment_tag_name": "自动标发",
                "tag_text": "自动标发",
                "source_status_text": "待审核",
                "error_message": "领星订单列表未返回客选物流字段。",
            }
        ],
        snapshot_complete=True,
    )

    shipment = controller.snapshot().shipments[0]
    assert shipment.system_order_no == "103000000000009901"
    assert shipment.platform_order_no == "112-0000000-0009901"
    assert shipment.logistics_no == ""
    assert shipment.scan_issue_code == "customer_shipping_service_unavailable"
    assert shipment.scan_issue_key.startswith("scan-issue:")
    assert shipment.last_error == "领星订单列表未返回客选物流字段。"

    changed = controller.change_shipment_statuses(
        [shipment.scan_issue_key],
        "manual_cancel",
        reason="业务确认该订单不再自动标发",
    )
    assert changed.accepted
    assert changed.details["changed_logistics_nos"] == (shipment.scan_issue_key,)
    managed = controller.snapshot().shipments[0]
    assert managed.scan_issue_state == "MANUALLY_CANCELLED"
    assert managed.scan_issue_reason == "业务确认该订单不再自动标发"
    controller.close()


def test_desktop_can_manually_add_shipment_and_show_identity_state(tmp_path):
    controller = _controller(tmp_path)

    result = controller.add_shipment_order(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="API 未返回，人工核对后补入。",
    )

    assert result.accepted
    shipment = controller.snapshot().shipments[0]
    assert shipment.system_order_no == "103710434633847501"
    assert shipment.platform_order_no == "112-1165824-9982644"
    assert shipment.logistics_no == "ALS01781406025"
    assert shipment.identity_state == "ACTIVE"
    assert shipment.logistics_state == "PENDING"
    assert shipment.erp_state == "WAITING"

    from lingxing_automation.products.catalog import (
        PRODUCT_IDENTITY_CATALOG_VERSION,
    )
    from shipment_automation.queue_store import ShipmentWorkflowStore

    store = ShipmentWorkflowStore(tmp_path / "data" / "shipment_queue.sqlite3")
    store.apply_product_identity_backfill(
        [
            {
                "system_order_no": shipment.system_order_no,
                "platform_order_no": shipment.platform_order_no,
                "product_types": (),
                "observed_asins": (),
                "evidence_scope": "sibling_aggregate",
            }
        ],
        catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
    )

    checked = controller.snapshot().shipments[0]
    assert checked.product_type == ""
    assert checked.product_identity_status_text == (
        "同平台兄弟单已完整核验，无 ASIN"
    )
    controller.close()


def test_controller_startup_reconciles_rules_before_any_feature_page_reads_state(
    tmp_path,
):
    from shipment_automation.alibaba_logistics import (
        tracking_number_mismatch_reason,
    )
    from shipment_automation.models import (
        LOGISTICS_BLOCKED,
        LOGISTICS_RETRYABLE,
        LogisticsDetail,
        ShipmentCandidate,
    )
    from shipment_automation.queue_store import ShipmentWorkflowStore

    queue_path = tmp_path / "data" / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(queue_path)
    candidate = ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="111-4492497-1964249",
        logistics_no="ALS01844600001",
        shipment_tag_name="自动标发",
    )
    store.upsert_candidate(candidate)
    detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="YWE",
        international_tracking_no="YWNJC010158019848",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        detail,
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason(
            detail.carrier,
            detail.international_tracking_no,
        ),
    )
    # Simulate the durable BLOCKED state written by an older release.  Current
    # programmatic failures are coerced to RETRYABLE before they reach SQLite.
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_logistics SET state = ?",
            (LOGISTICS_BLOCKED,),
        )

    controller = _controller(tmp_path)
    try:
        shipment = controller.snapshot().shipments[0]
        assert shipment.logistics_state == LOGISTICS_RETRYABLE
        assert shipment.logistics_last_error == ""
        assert any(
            entry.source == "automation_rule_reconciliation"
            for entry in controller.snapshot().logs
        )
    finally:
        controller.close()


def test_controller_does_not_create_automation_databases_only_to_run_reconciliation(
    tmp_path,
):
    controller = _controller(tmp_path)
    try:
        assert not controller._custom_state_path().exists()
        assert not controller._shipment_state_path().exists()
        assert not any(
            entry.source == "automation_rule_reconciliation"
            for entry in controller.snapshot().logs
        )
    finally:
        controller.close()


def test_rule_reconciliation_runs_once_per_database_path(
    tmp_path,
    monkeypatch,
) -> None:
    from shipment_automation.queue_store import ShipmentWorkflowStore

    CustomWorkflowStore(tmp_path / "data" / "automation.sqlite3").initialize()
    ShipmentWorkflowStore(
        tmp_path / "data" / "shipment_queue.sqlite3"
    ).initialize()
    custom_calls = 0
    tracking_calls = 0
    logistics_calls = 0
    original_custom = CustomWorkflowStore.repair_automated_blocked_stages
    original_tracking = (
        ShipmentWorkflowStore.requeue_tracking_mismatches_resolved_by_current_rules
    )
    original_logistics = ShipmentWorkflowStore.requeue_automated_logistics_blocks

    def counted_custom(store):
        nonlocal custom_calls
        custom_calls += 1
        return original_custom(store)

    def counted_tracking(store, *, run_id=""):
        nonlocal tracking_calls
        tracking_calls += 1
        return original_tracking(store, run_id=run_id)

    def counted_logistics(store, *, run_id=""):
        nonlocal logistics_calls
        logistics_calls += 1
        return original_logistics(store, run_id=run_id)

    monkeypatch.setattr(
        CustomWorkflowStore,
        "repair_automated_blocked_stages",
        counted_custom,
    )
    monkeypatch.setattr(
        ShipmentWorkflowStore,
        "requeue_tracking_mismatches_resolved_by_current_rules",
        counted_tracking,
    )
    monkeypatch.setattr(
        ShipmentWorkflowStore,
        "requeue_automated_logistics_blocks",
        counted_logistics,
    )

    controller = _controller(tmp_path)
    controller._refresh_persistent_rows(force=True)  # noqa: SLF001
    controller._refresh_persistent_rows(force=True)  # noqa: SLF001

    assert custom_calls == 1
    assert tracking_calls == 1
    assert logistics_calls == 1
    controller.close()


def test_desktop_shows_tag_removed_pause_as_clear_chinese_status(tmp_path):
    from shipment_automation.queue_store import ShipmentWorkflowStore

    controller = _controller(tmp_path)
    assert controller.add_shipment_order(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="建立待暂停任务",
    ).accepted
    store = ShipmentWorkflowStore(controller._shipment_state_path())

    result = store.reconcile_shipment_tag_snapshot(
        {"103710434633847501": False},
        snapshot_complete=True,
        run_id="desktop-pause-display",
    )

    assert result.paused_count == 1
    shipment = controller.snapshot().shipments[0]
    assert shipment.identity_state == "PAUSED_TAG_REMOVED"
    assert shipment.identity_status_text == "标签已移除/自动暂停"
    controller.close()


def test_desktop_manual_shipment_validation_does_not_create_invalid_row(tmp_path):
    controller = _controller(tmp_path)

    result = controller.add_shipment_order(
        system_order_no="bad",
        platform_order_no="also-bad",
        logistics_no="?",
        reason="测试非法输入",
    )

    assert not result.accepted
    assert controller.snapshot().shipments == []
    controller.close()


def test_desktop_manual_shipment_reports_logistics_identity_conflict(tmp_path):
    controller = _controller(tmp_path)
    assert controller.add_shipment_order(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="首次加入",
    ).accepted

    conflict = controller.add_shipment_order(
        system_order_no="103710434633847599",
        platform_order_no="113-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="测试相同物流号",
    )

    assert not conflict.accepted
    assert "身份冲突" in conflict.message
    assert controller.snapshot().shipments[0].identity_state == "CONFLICT"
    controller.close()


def test_desktop_guarded_shipment_status_changes_preserve_history(tmp_path):
    controller = _controller(tmp_path)
    assert controller.add_shipment_order(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="人工补入",
    ).accepted

    assert controller.change_shipment_status(
        "ALS01781406025",
        "mark_manual_done",
        reason="已人工核对领星完成",
    ).accepted
    assert controller.snapshot().shipments[0].erp_state == "DONE"
    assert controller.change_shipment_status(
        "ALS01781406025",
        "undo_manual_done",
        reason="操作录入错误，撤销",
    ).accepted
    assert controller.snapshot().shipments[0].erp_state == "WAITING"
    assert controller.change_shipment_status(
        "ALS01781406025",
        "cancel",
        reason="暂不处理",
    ).accepted
    assert controller.snapshot().shipments[0].identity_state == "CANCELLED"
    assert controller.change_shipment_status(
        "ALS01781406025",
        "restore_cancelled",
        reason="重新确认处理",
    ).accepted
    assert controller.snapshot().shipments[0].identity_state == "ACTIVE"
    controller.close()


def test_desktop_rejects_unknown_or_impossible_shipment_status_change(tmp_path):
    controller = _controller(tmp_path)
    assert controller.add_shipment_order(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="人工补入",
    ).accepted

    assert not controller.change_shipment_status(
        "ALS01781406025",
        "unknown",
        reason="测试",
    ).accepted
    # ERP cannot be retried before logistics reaches READY.
    assert not controller.change_shipment_status(
        "ALS01781406025",
        "retry_erp",
        reason="测试前置条件",
    ).accepted
    controller.close()


def test_persistent_controller_keeps_business_values_and_filters_credentials(tmp_path):
    controller = _controller(tmp_path)
    task_id = "82da8f446d3d4bc787210584bfa83acf"
    audit_day = "2001-02-03"
    audit_path = (
        rf"C:\safe\custom_order_scan\{audit_day}"
        rf"\custom_order_scan_20010203_040506_{task_id}.json"
    )
    error_id = "1234567890abcdef1234567890abcdef"
    controller._append_log(
        LogLevel.ERROR,
        "customization",
        (
            "订单 112-1999004-7905025 失败，联系 +1 555 123 4567，"
            f"token=should-not-appear；审计任务 ID：{task_id}；"
            f"日志：{audit_path}；错误编号：{error_id}。"
        ),
        task_id=task_id,
    )

    event_files = list((tmp_path / "logs/app_events").glob("*.jsonl"))
    assert len(event_files) == 1
    events = [
        json.loads(line)
        for line in event_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event = next(item for item in events if item.get("task_id") == task_id)
    raw = json.dumps(event, ensure_ascii=False)
    assert "should-not-appear" not in raw
    assert "token=<redacted>" in raw
    assert "112-1999004-7905025" in raw
    assert "+1 555 123 4567" in raw
    assert task_id in event["message"]
    assert audit_path in event["message"]
    assert audit_day in event["message"]
    assert error_id in event["message"]
    title, content = controller.full_log_text(task_id)
    assert task_id in title
    assert task_id in content
    assert audit_path in content
    assert "112-1999004-7905025" in content
    assert "should-not-appear" not in content

    failed_task_id = "1234567890abcdef82da8f446d3d4bc7"
    controller._append_log(
        LogLevel.ERROR,
        "customization",
        f"安全审计日志写入失败。任务 ID：{failed_task_id}；请检查 logs。",
        task_id=failed_task_id,
    )
    events = [
        json.loads(line)
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed_event = next(item for item in events if item.get("task_id") == failed_task_id)
    assert failed_task_id in failed_event["message"]
    controller.close()


def test_persistent_controller_keeps_only_verified_company_operator_email(
    tmp_path,
):
    controller = _controller(tmp_path)
    controller._append_log(
        LogLevel.INFO,
        "operator-audit",
        "operator event",
        operator_name="Alice",
        operator_email="Alice@BillyPrint.com",
    )
    controller._append_log(
        LogLevel.INFO,
        "operator-audit",
        "untrusted operator event",
        operator_name="External",
        operator_email="external@example.com",
    )

    events = [
        json.loads(line)
        for path in (tmp_path / "logs/app_events").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    trusted = next(item for item in events if item["message"] == "operator event")
    untrusted = next(
        item for item in events if item["message"] == "untrusted operator event"
    )

    assert trusted["operator_email"] == "alice@billyprint.com"
    assert untrusted["operator_email"] == ""
    controller.close()


def test_full_application_log_includes_the_verified_operator_account(tmp_path):
    controller = _controller(tmp_path)
    controller._append_log(
        LogLevel.INFO,
        "operator-audit",
        "manual operation",
        operator_name="Alice",
        operator_email="alice@billyprint.com",
    )

    _title, content = controller.full_log_text()

    assert "[账号：Alice（alice@billyprint.com）]" in content
    assert "manual operation" in content
    controller.close()


def test_persistent_controller_keeps_contact_and_identifier_contexts(tmp_path):
    controller = _controller(tmp_path)
    task_id = "82da8f446d3d4bc787210584bfa83acf"
    date_fragment = "2026-07-14"
    order_no = "112-1999004-7905025"
    separated_numbers = [
        f"+86/{order_no}",
        f"+86:{order_no}",
        f"+86_{order_no}",
        rf"+86\{order_no}",
        f"+86|{order_no}",
        f"+86／{order_no}",
    ]
    controller._append_log(
        LogLevel.ERROR,
        "customization",
        (
            f"审计任务 ID：{task_id}@example.com；日期邮箱片段 {date_fragment}@example.com；"
            f"号码片段 +86 {date_fragment}；组合号码 +86 {order_no}；"
            f"其他组合号码 {'；'.join(separated_numbers)}；"
            "原文占位符：<safe-log-id-0>"
        ),
        task_id=task_id,
    )

    event_files = list((tmp_path / "logs/app_events").glob("*.jsonl"))
    events = [
        json.loads(line)
        for line in event_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    message = next(item["message"] for item in events if item.get("task_id") == task_id)
    assert f"{task_id}@example.com" in message
    assert f"{date_fragment}@example.com" in message
    assert f"+86 {date_fragment}" in message
    assert f"+86 {order_no}" in message
    for separated_number in separated_numbers:
        assert separated_number in message
    assert "<redacted-email>" not in message
    assert "<redacted-phone>" not in message
    assert "<safe-log-id-0>" in message
    controller.close()


def test_persistent_controller_captures_concurrent_event_before_another_insert(
    tmp_path,
    monkeypatch,
):
    original_append = InMemoryBackgroundTaskController._append_log

    def delayed_append(self, level, source, message, *, task_id=None):
        original_append(self, level, source, message, task_id=task_id)
        time.sleep(0.003)

    monkeypatch.setattr(InMemoryBackgroundTaskController, "_append_log", delayed_append)
    controller = _controller(tmp_path)
    worker_count = 12
    start = threading.Barrier(worker_count)

    def write_event(index: int) -> None:
        start.wait()
        controller._append_log(
            LogLevel.INFO,
            "concurrency-test",
            f"并发消息 {index}",
            task_id=f"concurrent-{index}",
        )

    threads = [threading.Thread(target=write_event, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    event_files = list((tmp_path / "logs/app_events").glob("*.jsonl"))
    events = [
        json.loads(line)
        for line in event_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    concurrent_events = {
        item["task_id"]: item["message"]
        for item in events
        if str(item.get("task_id") or "").startswith("concurrent-")
    }
    assert len(
        [item for item in events if str(item.get("task_id") or "").startswith("concurrent-")]
    ) == worker_count
    assert concurrent_events == {
        f"concurrent-{index}": f"并发消息 {index}" for index in range(worker_count)
    }
    controller.close()


def test_persistent_controller_logs_connected_backend_instead_of_skeleton_warning(tmp_path):
    controller = _controller(tmp_path)

    startup = controller.snapshot().logs[-1]
    assert startup.level is LogLevel.INFO
    assert startup.message == "桌面程序已连接加密配置和 SQLite 状态库。"

    event_files = list((tmp_path / "logs/app_events").glob("*.jsonl"))
    assert len(event_files) == 1
    raw = event_files[0].read_text(encoding="utf-8")
    assert "桌面程序已连接加密配置和 SQLite 状态库" in raw
    assert "桌面骨架尚未连接实际后台 Worker" not in raw
    controller.close()


def test_full_log_view_prefers_structured_scan_audit_for_selected_task(tmp_path):
    controller = _controller(tmp_path)
    audit_path = (
        tmp_path
        / "logs"
        / "custom_order_scan"
        / "2026-07-14"
        / "custom_order_scan_20260714_143000_task-audit-1.json"
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "task_id": "task-audit-1",
                "summary": {"candidate_count": 1},
                "order_decisions": [
                    {
                        "platform_order_no": "112-1999004-7905025",
                        "decision": "candidate",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    title, content = controller.full_log_text("task-audit-1")

    assert str(audit_path.resolve()) in title
    assert "112-1999004-7905025" in content
    assert "candidate_count" in content
    controller.close()


def test_scan_log_text_reads_server_hosted_custom_and_shipment_audits(tmp_path):
    controller = _controller(tmp_path)
    custom_path = (
        tmp_path
        / "logs/custom_order_scan/2026-08-10"
        / "custom_order_scan_20260810_140000_custom-task.json"
    )
    shipment_path = (
        tmp_path
        / "logs/shipment_scan/2026-08-10"
        / "shipment_scan_20260810_140500_shipment-task.json"
    )
    custom_path.parent.mkdir(parents=True)
    shipment_path.parent.mkdir(parents=True)
    custom_path.write_text('{"kind":"customization"}', encoding="utf-8")
    shipment_path.write_text('{"kind":"shipment"}', encoding="utf-8")

    custom_title, custom_content = controller.scan_log_text("customization")
    shipment_title, shipment_content = controller.scan_log_text("shipment")
    invalid_title, invalid_content = controller.scan_log_text("unknown")

    assert custom_title == "定制订单扫描日志"
    assert custom_path.name in custom_content
    assert '"kind":"customization"' in custom_content
    assert shipment_title == "自动标发扫描日志"
    assert shipment_path.name in shipment_content
    assert '"kind":"shipment"' in shipment_content
    assert invalid_title == "扫描日志"
    assert invalid_content == "扫描日志类型无效。"
    controller.close()


def test_full_log_view_combines_retry_attempts_without_prefix_collision(tmp_path):
    controller = _controller(tmp_path)
    audit_dir = tmp_path / "logs/api_scan/2026-07-14"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task-retry.attempt-001.json").write_text(
        '{"attempt": 1}', encoding="utf-8"
    )
    (audit_dir / "task-retry.json").write_text('{"attempt": 2}', encoding="utf-8")
    (audit_dir / "task-retry-other.json").write_text(
        '{"attempt": "wrong-task"}', encoding="utf-8"
    )

    title, content = controller.full_log_text("task-retry")

    assert "共 2 次" in title
    assert '"attempt": 1' in content
    assert '"attempt": 2' in content
    assert "wrong-task" not in content
    controller.close()
