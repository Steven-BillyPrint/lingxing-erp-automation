from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from erp_automation.coordination.store import CoordinationStore
from erp_automation.persistence.workflow_store import CustomWorkflowStore
from lingxing_automation.storage import sqlite_connection
from lingxing_automation.storage.sqlite_connection import ClosingConnection, connect_database
from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.queue_store import ShipmentWorkflowStore


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.fixture
def recorded_connections(monkeypatch):
    # Keep strong references so GC cannot make a missing close appear correct.
    connections = []
    original = sqlite3.connect

    def recording_connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite_connection.sqlite3, "connect", recording_connect)
    yield connections
    for connection in connections:
        try:
            connection.close()
        except sqlite3.ProgrammingError:
            pass


@pytest.mark.parametrize("store_kind", ["coordination", "custom", "shipment", "notification"])
def test_store_initialization_and_requests_close_every_connection(tmp_path, recorded_connections, store_kind):
    path = tmp_path / f"{store_kind}.sqlite3"
    if store_kind == "coordination":
        store = CoordinationStore(path)
        for _ in range(20):
            store.current_revision()
    elif store_kind == "custom":
        store = CustomWorkflowStore(path)
        for _ in range(20):
            store.list_workflow_page()
    elif store_kind == "shipment":
        store = ShipmentWorkflowStore(path)
        for _ in range(20):
            store.count_all_jobs()
    else:
        store = ShipmentNotificationStore(path)
        store.initialize()
        for _ in range(20):
            with store.connect() as connection:
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    assert recorded_connections
    for connection in recorded_connections:
        assert_closed(connection)


def test_success_early_return_and_failure_keep_transaction_contract(tmp_path):
    path = tmp_path / "transactions.sqlite3"
    with connect_database(path) as created:
        created.execute("CREATE TABLE changes (value TEXT UNIQUE)")
        created.execute("INSERT INTO changes VALUES ('committed')")
    assert_closed(created)

    def early_return():
        with connect_database(path) as connection:
            connection.execute("INSERT INTO changes VALUES ('early return')")
            return connection

    assert_closed(early_return())
    with pytest.raises(sqlite3.IntegrityError):
        with connect_database(path) as failed:
            failed.execute("INSERT INTO changes VALUES ('rolled back')")
            failed.execute("INSERT INTO changes VALUES ('committed')")
    assert_closed(failed)
    with connect_database(path) as reader:
        assert [row[0] for row in reader.execute("SELECT value FROM changes ORDER BY rowid")] == [
            "committed", "early return",
        ]


def test_failed_commit_rolls_back_and_closes(tmp_path):
    path = tmp_path / "deferred.sqlite3"
    with connect_database(path, pragmas=("PRAGMA foreign_keys = ON",)) as connection:
        connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);
        """)
    with pytest.raises(sqlite3.IntegrityError):
        with connect_database(path, pragmas=("PRAGMA foreign_keys = ON",)) as failed:
            failed.execute("INSERT INTO child VALUES (999)")
    assert_closed(failed)
    with connect_database(path) as reader:
        assert reader.execute("SELECT count(*) FROM child").fetchone()[0] == 0


def test_configuration_failure_closes_connection(tmp_path, recorded_connections):
    with pytest.raises(sqlite3.OperationalError):
        connect_database(tmp_path / "configuration.sqlite3", pragmas=("PRAGMA = invalid",))
    assert len(recorded_connections) == 1
    assert_closed(recorded_connections[0])


def test_busy_failure_closes_without_releasing_another_transaction(tmp_path):
    path = tmp_path / "busy.sqlite3"
    with connect_database(path) as owner:
        owner.execute("CREATE TABLE changes (value TEXT)")
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("INSERT INTO changes VALUES ('owner')")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with connect_database(path, timeout=0.01) as blocked:
                blocked.execute("INSERT INTO changes VALUES ('other')")
        assert_closed(blocked)
        assert owner.in_transaction
    with connect_database(path) as reader:
        assert [row[0] for row in reader.execute("SELECT value FROM changes")] == ["owner"]


def test_read_only_connection_remains_read_only_and_closes(tmp_path):
    path = tmp_path / "shipment.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    reader = ShipmentWorkflowStore(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with reader.connect() as connection:
            connection.execute("DELETE FROM shipment_jobs")
    assert_closed(connection)


def test_backup_failure_closes_both_connections(tmp_path, monkeypatch, recorded_connections):
    store = ShipmentWorkflowStore(tmp_path / "shipment.sqlite3")
    store.initialize()

    def failed_backup(self, target, *args, **kwargs):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(ClosingConnection, "backup", failed_backup)
    with pytest.raises(OSError, match="synthetic backup failure"):
        store._backup_before_version("resource-test")
    for connection in recorded_connections:
        assert_closed(connection)


def test_backup_target_open_failure_also_closes_source(tmp_path, monkeypatch, recorded_connections):
    store = ShipmentWorkflowStore(tmp_path / "shipment.sqlite3")
    store.initialize()
    original = sqlite3.connect

    def cannot_open_target(database, *args, **kwargs):
        if "pre_resource-test" in str(database):
            raise sqlite3.OperationalError("synthetic target failure")
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite_connection.sqlite3, "connect", cannot_open_target)
    with pytest.raises(sqlite3.OperationalError, match="synthetic target failure"):
        store._backup_before_version("resource-test")
    for connection in recorded_connections:
        assert_closed(connection)


def test_independent_thread_transactions_keep_all_writes(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    with connect_database(path, pragmas=("PRAGMA journal_mode = WAL",)) as connection:
        connection.execute("CREATE TABLE writes (id INTEGER PRIMARY KEY)")

    def write(value):
        with connect_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO writes VALUES (?)", (value,))
        assert_closed(connection)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(80)))
    with connect_database(path) as connection:
        assert connection.execute("SELECT count(*) FROM writes").fetchone()[0] == 80
