from __future__ import annotations

import sqlite3

from shipment_automation.alibaba_order_session import AlibabaOrderSessionStore


def test_session_store_persists_only_non_sensitive_handoff_data(tmp_path) -> None:
    path = tmp_path / "alibaba.sqlite3"
    store = AlibabaOrderSessionStore(path)

    store.save(
        instance_id="desktop-a",
        system_order_no="SYS-1",
        category="tent",
        baseline_draft_urls=("https://scm.alibaba.com/one",),
    )
    session = store.get("SYS-1", instance_id="desktop-a")

    assert session is not None
    assert session.instance_id == "desktop-a"
    assert session.system_order_no == "SYS-1"
    assert session.category == "tent"
    assert session.baseline_draft_urls == ("https://scm.alibaba.com/one",)
    database_bytes = path.read_bytes()
    assert b"recipient" not in database_bytes
    assert b"email" not in database_bytes


def test_same_order_sessions_are_isolated_by_desktop_instance(tmp_path) -> None:
    store = AlibabaOrderSessionStore(tmp_path / "alibaba.sqlite3")
    store.save(
        instance_id="desktop-a",
        system_order_no="SYS-1",
        category="tent",
        baseline_draft_urls=("https://scm.alibaba.com/a",),
    )
    store.save(
        instance_id="desktop-b",
        system_order_no="SYS-1",
        category="tent",
        baseline_draft_urls=("https://scm.alibaba.com/b",),
    )

    first = store.get("SYS-1", instance_id="desktop-a")
    second = store.get("SYS-1", instance_id="desktop-b")

    assert first is not None
    assert second is not None
    assert first.baseline_draft_urls == ("https://scm.alibaba.com/a",)
    assert second.baseline_draft_urls == ("https://scm.alibaba.com/b",)

    store.delete("SYS-1", instance_id="desktop-a")

    assert store.get("SYS-1", instance_id="desktop-a") is None
    assert store.get("SYS-1", instance_id="desktop-b") is not None


def test_session_store_releases_database_handle_after_operations(tmp_path) -> None:
    path = tmp_path / "alibaba.sqlite3"
    store = AlibabaOrderSessionStore(path)
    store.save(
        instance_id="desktop-a",
        system_order_no="SYS-1",
        category="tent",
        baseline_draft_urls=("https://scm.alibaba.com/a",),
    )

    assert store.get("SYS-1", instance_id="desktop-a") is not None
    store.delete("SYS-1", instance_id="desktop-a")

    # Windows refuses to unlink an SQLite file while any connection still has
    # it open, so this also guards the desktop runner's temporary workspaces.
    path.unlink()
    assert not path.exists()


def test_legacy_single_key_session_table_is_migrated(tmp_path) -> None:
    path = tmp_path / "alibaba.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE alibaba_order_sessions (
                system_order_no TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                baseline_draft_urls_json TEXT NOT NULL,
                prepared_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO alibaba_order_sessions (
                system_order_no,
                category,
                baseline_draft_urls_json,
                prepared_at
            ) VALUES (
                'SYS-OLD',
                'tent',
                '["https://scm.alibaba.com/old"]',
                '2099-01-01T00:00:00+00:00'
            )
            """
        )

    migrated = AlibabaOrderSessionStore(path).get("SYS-OLD")

    assert migrated is not None
    assert migrated.instance_id == ""
    assert migrated.baseline_draft_urls == (
        "https://scm.alibaba.com/old",
    )
