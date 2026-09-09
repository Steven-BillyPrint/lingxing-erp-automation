"""Short-lived SQLite connections shared by the persistence adapters.

This module owns resource lifetime only. Each store still chooses its PRAGMAs,
transaction boundaries and schema; there are no UI or business dependencies.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback normally, then close when the owning context exits.

    A connection belongs to one operation. Callers using it without a context
    must close it themselves; it cannot be reused after leaving a context.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_database(
    database: str | os.PathLike[str],
    *,
    timeout: float = 5.0,
    uri: bool = False,
    pragmas: Iterable[str] = (),
) -> ClosingConnection:
    """Open an owned row connection and release it if configuration fails.

    PRAGMA statements are constants selected by the calling store, never user
    input. Legacy sqlite transaction semantics are deliberately unchanged.
    """

    connection = sqlite3.connect(
        database, timeout=timeout, uri=uri, factory=ClosingConnection,
    )
    try:
        connection.row_factory = sqlite3.Row
        for pragma in pragmas:
            connection.execute(pragma)
    except BaseException:
        connection.close()
        raise
    return connection
