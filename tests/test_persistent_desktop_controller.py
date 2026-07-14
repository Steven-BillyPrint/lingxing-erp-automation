from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading

from erp_automation.configuration import (
    ConfigurationDecryptionError,
    EncryptedConfigurationStore,
    PortableEncryptedData,
    PortableMigrationService,
)
from erp_automation.ui import (
    Capability,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LogLevel,
    PersistentBackgroundTaskController,
    TaskArea,
    TaskCommand,
    TaskStatus,
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


def _controller(workspace, *, key=b"machine-one"):
    store = EncryptedConfigurationStore(
        workspace / "data/config.enc",
        backend=LocalBackend(key),
    )
    service = PortableMigrationService(backend=PortableBackend())
    return PersistentBackgroundTaskController(
        workspace,
        config_store=store,
        migration_service=service,
    )


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


def test_settings_are_encrypted_and_repr_does_not_disclose_secrets(tmp_path):
    controller = _controller(tmp_path)
    settings = DesktopSettings(
        lingxing_app_id="app-id",
        lingxing_app_secret="secret-value",
        lingxing_password="web-password",
        alibaba_password="alibaba-password",
        amazon_lwa_client_secret="amazon-secret",
        amazon_refresh_token="refresh-secret",
    )

    result = controller.save_settings(settings)

    assert result.accepted
    encoded = (tmp_path / "data/config.enc").read_bytes()
    for secret in (
        b"secret-value",
        b"web-password",
        b"alibaba-password",
        b"amazon-secret",
        b"refresh-secret",
    ):
        assert secret not in encoded
        assert secret.decode() not in repr(settings)
    assert controller.snapshot().settings.payment_window_hours == 96
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


def test_persistent_controller_runs_one_background_task_and_updates_visible_rows(tmp_path):
    controller = _controller(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def runner(_command):
        started.set()
        assert release.wait(2)
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
    assert started.wait(2)
    assert controller.snapshot().tasks[0].status is TaskStatus.RUNNING

    future = controller._futures[submitted.task_id]
    release.set()
    future.result(timeout=2)
    snapshot = controller.snapshot()
    assert snapshot.tasks[0].status is TaskStatus.SUCCEEDED
    assert snapshot.custom_orders[0].platform_order_no == "111-2222222-3333333"
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
    assert second_task.status is TaskStatus.BLOCKED
    assert calls == ["first-order"]
    assert not controller.set_emergency_stop_writes(False).accepted
    first_future = controller._futures[first.task_id]
    release.set()
    first_future.result(timeout=2)
    assert calls == ["first-order"]
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


def test_uncertain_custom_write_is_persisted_as_blocked_until_reopened(tmp_path):
    controller = _controller(tmp_path)
    assert controller.set_emergency_stop_writes(False).accepted

    def runner(_command):
        return {
            "status": "blocked",
            "message": "API 返回结果不明确，必须人工读回。",
            "workflow_blocked_stage": "contact",
        }

    controller.attach_task_runner(runner)
    submitted = controller.submit_task(_write_command("uncertain-order"))
    assert submitted.accepted and submitted.task_id
    controller._futures[submitted.task_id].result(timeout=2)

    workflow = controller._get_custom_store().get_workflow("uncertain-order")
    assert workflow is not None
    assert workflow["workflow_status"] == "blocked"
    contact = next(stage for stage in workflow["stages"] if stage["stage"] == "contact")
    assert contact["state"] == "BLOCKED"
    assert "人工读回" in contact["last_error"]

    rejected = controller.submit_task(_write_command("uncertain-order"))
    assert not rejected.accepted
    assert "已阻止阶段" in rejected.message
    assert controller.reopen_custom_workflow(
        "uncertain-order",
        "contact",
        reason="已经人工读回并确认未写入",
    ).accepted
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


def test_persistent_controller_writes_redacted_application_log_and_reads_by_task(tmp_path):
    controller = _controller(tmp_path)
    controller._append_log(
        LogLevel.ERROR,
        "customization",
        "订单 112-1999004-7905025 失败，联系 +1 555 123 4567，token=should-not-appear",
        task_id="task-log-1",
    )

    event_files = list((tmp_path / "logs/app_events").glob("*.jsonl"))
    assert len(event_files) == 1
    raw = event_files[0].read_text(encoding="utf-8")
    assert "should-not-appear" not in raw
    assert "token=<redacted>" in raw
    assert "112-1999004-7905025" in raw
    assert "+1 555 123 4567" not in raw
    assert "<redacted-phone>" in raw
    title, content = controller.full_log_text("task-log-1")
    assert "task-log-1" in title
    assert "112-1999004-7905025" in content
    assert "should-not-appear" not in content
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
    audit_path = tmp_path / "logs/api_scan/2026-07-14/task-audit-1.json"
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
