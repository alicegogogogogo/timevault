from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .model import epoch_millis

# One truncation event, as handed to :meth:`Store.insert_truncation`.
TruncationRecord = tuple[str, str, str, int, int, int]


class Store:
    """Append-only persistence for entity versions.

    Rows in ``versions`` are never deleted.  A correction appends its own row
    and closes the ``valid_end`` of the value it supersedes; every closing is
    also appended to ``truncations``, which records *when the system learned*
    the bound, so a transaction-time reader can tell a declared bound from one a
    later correction pulled back.
    """

    def __init__(self, path: str, clock: Callable[[], datetime] | None = None):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (type, id)
            );
            CREATE TABLE IF NOT EXISTS versions (
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              attribute TEXT NOT NULL,
              version INTEGER NOT NULL,
              operation TEXT NOT NULL,
              value TEXT,
              valid_from INTEGER NOT NULL,
              valid_end INTEGER,
              declared_end INTEGER,
              recorded_at INTEGER NOT NULL,
              PRIMARY KEY (type, id, attribute, version)
            );
            CREATE INDEX IF NOT EXISTS versions_by_attribute
              ON versions(type, id, attribute, valid_from);
            CREATE TABLE IF NOT EXISTS truncations (
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              attribute TEXT NOT NULL,
              version INTEGER NOT NULL,
              recorded_at INTEGER NOT NULL,
              valid_end INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS truncations_by_version
              ON truncations(type, id, attribute, version, recorded_at);
            CREATE TABLE IF NOT EXISTS idempotency (
              key TEXT PRIMARY KEY,
              operation TEXT NOT NULL,
              response TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def now(self) -> int:
        """Current instant as integer milliseconds since the Unix epoch."""
        return epoch_millis(self.clock().astimezone(timezone.utc))

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def decode(value: str) -> Any:
        return json.loads(value)

    # -- entity bookkeeping -------------------------------------------------

    def entity_row(self, entity_type: str, entity_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT created_at FROM entities WHERE type = ? AND id = ?",
            (entity_type, entity_id),
        ).fetchone()

    def insert_entity(self, entity_type: str, entity_id: str, created_at: int) -> None:
        self.connection.execute(
            "INSERT INTO entities(type, id, created_at) VALUES (?, ?, ?)",
            (entity_type, entity_id, created_at),
        )

    # -- versions -----------------------------------------------------------

    def versions_for_entity(self, entity_type: str, entity_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT attribute, version, operation, value, valid_from, valid_end,
                       declared_end, recorded_at
                  FROM versions
                 WHERE type = ? AND id = ?
                 ORDER BY attribute, valid_from, version
                """,
                (entity_type, entity_id),
            ).fetchall()
        )

    def versions_for_attribute(
        self, entity_type: str, entity_id: str, attribute: str
    ) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT attribute, version, operation, value, valid_from, valid_end,
                       declared_end, recorded_at
                  FROM versions
                 WHERE type = ? AND id = ? AND attribute = ?
                 ORDER BY valid_from, version
                """,
                (entity_type, entity_id, attribute),
            ).fetchall()
        )

    def next_version(self, entity_type: str, entity_id: str, attribute: str) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next
              FROM versions
             WHERE type = ? AND id = ? AND attribute = ?
            """,
            (entity_type, entity_id, attribute),
        ).fetchone()
        return int(row["next"])

    # -- truncations --------------------------------------------------------

    def truncations_for_entity(self, entity_type: str, entity_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT attribute, version, valid_end, recorded_at
                  FROM truncations
                 WHERE type = ? AND id = ?
                 ORDER BY recorded_at, attribute, version
                """,
                (entity_type, entity_id),
            ).fetchall()
        )

    def truncate_attribute(
        self,
        entity_type: str,
        entity_id: str,
        attribute: str,
        at: int,
        effective: int,
        recorded_at: int,
    ) -> list[TruncationRecord]:
        """Apply a correction landing at ``at`` that takes effect at ``effective``.

        The version straddling ``at`` keeps its value and only has its
        ``valid_end`` pulled back to ``at``; ``declared_end`` keeps the bound the
        assertion itself declared, so a reader before this write can still see
        the window as it stood when the row was written.

        Every version that starts at or after ``at`` is superseded: it holds no
        valid time from ``at`` onwards.  Its own declared ``valid_end`` stays
        where it was, because a bounded assertion keeps its own bound; the
        truncation records the instant the new value took the window over.

        ``effective`` differs from ``at`` only for a backdated correction with an
        explicit ``valid_from``: the correction is placed in the past but takes
        effect at the instant the version it supersedes did, which is what lets a
        restated past win from ``valid_from`` onwards while the value it
        displaced collapses.

        Every window that this correction closes gets a row in ``truncations``,
        stamped with ``recorded_at`` — the transaction instant the bound was
        learned at, never a business instant.  A truncation is knowledge, so it
        must be datable on the axis a reader filters on: a reader whose
        ``known_at`` is earlier than this write must not see the end it pulled
        back.  Recording one row per event, rather than one stamp per version, is
        what keeps that exact when a later correction pulls the same window back
        twice.

        Only versions recorded no later than this request are touched, so the
        versions inserted by the correction that calls this method cannot
        supersede one another, while a previous correction recorded at the same
        millisecond is still superseded correctly.
        """
        rows = self.connection.execute(
            """
            SELECT version, valid_from, valid_end, declared_end
              FROM versions
             WHERE type = ? AND id = ? AND attribute = ? AND recorded_at <= ?
            """,
            (entity_type, entity_id, attribute, recorded_at),
        ).fetchall()
        events: list[TruncationRecord] = []
        for row in rows:
            version = int(row["version"])
            if int(row["valid_from"]) >= at:
                # A version starting at or after the landing point holds no valid
                # time from there on: it is superseded, not trimmed.
                events.append(
                    (entity_type, entity_id, attribute, version, recorded_at, effective)
                )
                continue
            end = None if row["valid_end"] is None else int(row["valid_end"])
            if end is not None and end <= at:
                # A bound the version declared itself already stops before the
                # landing point, so the correction takes no valid time from it.
                continue
            declared = None if row["declared_end"] is None else int(row["declared_end"])
            self.connection.execute(
                """
                UPDATE versions
                   SET valid_end = ?, declared_end = ?
                 WHERE type = ? AND id = ? AND attribute = ? AND version = ?
                """,
                (
                    at,
                    declared,
                    entity_type,
                    entity_id,
                    attribute,
                    version,
                ),
            )
            events.append(
                (entity_type, entity_id, attribute, version, recorded_at, at)
            )
        if events:
            self.connection.executemany(
                """
                INSERT INTO truncations(type, id, attribute, version, recorded_at, valid_end)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                events,
            )
        return events

    def insert_version(
        self,
        entity_type: str,
        entity_id: str,
        attribute: str,
        version: int,
        operation: str,
        value: Any,
        valid_from: int,
        valid_end: int | None,
        recorded_at: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO versions(type, id, attribute, version, operation, value,
                                 valid_from, valid_end, declared_end, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                attribute,
                version,
                operation,
                None if value is None else self.encode(value),
                valid_from,
                valid_end,
                valid_end,
                recorded_at,
            ),
        )

    # -- idempotency --------------------------------------------------------

    def idempotent_response(self, key: str, operation: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT operation, response FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row["operation"] != operation:
            return None
        return self.decode(row["response"])

    def remembered_operation(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT operation FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["operation"])

    def remember(self, key: str, operation: str, response: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO idempotency(key, operation, response) VALUES (?, ?, ?)",
            (key, operation, self.encode(response)),
        )
