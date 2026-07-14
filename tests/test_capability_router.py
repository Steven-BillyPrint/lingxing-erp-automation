from __future__ import annotations

import asyncio

import pytest

from erp_automation.application import (
    Capability,
    CapabilityMode,
    CapabilityRouter,
    ManualReviewRequired,
    MutationResult,
    MutationState,
    RollbackManager,
    SCRIPT_BASELINE_BRANCH,
)


def test_read_api_failure_can_fall_back_to_browser():
    router = CapabilityRouter()

    async def api():
        raise TimeoutError("timeout")

    result = asyncio.run(
        router.execute_read(
            Capability.LIST_ORDERS,
            api=api,
            browser=lambda: "browser-orders",
        )
    )

    assert result == "browser-orders"


def test_unknown_write_never_repeats_through_browser():
    router = CapabilityRouter()
    browser_called = False

    async def api():
        return MutationResult(MutationState.UNKNOWN, "api")

    async def browser():
        nonlocal browser_called
        browser_called = True
        return MutationResult(MutationState.SUCCEEDED, "browser")

    with pytest.raises(ManualReviewRequired):
        asyncio.run(router.execute_write(Capability.SPLIT_ORDER, api=api, browser=browser))
    assert browser_called is False


def test_write_guard_is_rechecked_before_each_mutation() -> None:
    state = {"enabled": True}
    router = CapabilityRouter(writes_enabled=lambda: state["enabled"])

    first = asyncio.run(
        router.execute_write(
            Capability.UPDATE_REMARK,
            api=lambda: MutationResult(MutationState.SUCCEEDED, "api"),
            browser=None,
        )
    )
    state["enabled"] = False
    second = asyncio.run(
        router.execute_write(
            Capability.UPDATE_REMARK,
            api=lambda: MutationResult(MutationState.SUCCEEDED, "api"),
            browser=None,
        )
    )

    assert first.state is MutationState.SUCCEEDED
    assert second.state is MutationState.DISABLED


def test_definitive_api_failure_requires_approval_before_browser_write():
    router = CapabilityRouter({Capability.UPDATE_REMARK: CapabilityMode.API_PREFERRED})

    result = asyncio.run(
        router.execute_write(
            Capability.UPDATE_REMARK,
            api=lambda: MutationResult(
                MutationState.FAILED,
                "api",
                definitely_not_executed=True,
            ),
            browser=lambda: MutationResult(MutationState.SUCCEEDED, "browser"),
            approve_browser_fallback=lambda _capability, _result: True,
        )
    )

    assert result.state == MutationState.SUCCEEDED
    assert result.source == "browser"


def test_rollback_points_to_frozen_git_baseline_without_runtime_legacy_launcher():
    assert SCRIPT_BASELINE_BRANCH == "codex/script-baseline-20260714"


def test_rollback_snapshot_copies_state_and_writes_manifest(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "data" / "processed.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")
    manager = RollbackManager(workspace, tmp_path / "backups")

    snapshot = manager.create_snapshot([source], reason="升级前")

    assert (snapshot / "files" / "data" / "processed.json").read_text(encoding="utf-8") == "{}"
    assert "升级前" in (snapshot / "manifest.json").read_text(encoding="utf-8")

    source.write_text('{"changed": true}', encoding="utf-8")
    manager.restore_snapshot(snapshot)
    assert source.read_text(encoding="utf-8") == "{}"
