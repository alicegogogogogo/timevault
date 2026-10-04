from __future__ import annotations

import json
import sqlite3
import threading
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
        # One connection is shared across request threads, so every use of it is
        # serialised by this lock.  A write holds it across its whole
        # transaction, which is what commits different batches one after another
        # (continuous versions, non-overlapping windows) and stops any reader on
        # another thread observing a half-applied batch.  It is reentrant so the
        # read helpers a write calls while holding the lock can take it again.
        self.lock = threading.RLock()
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
              event_seq INTEGER,
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
              valid_end INTEGER NOT NULL,
              event_seq INTEGER
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
        self._migrate_audit()

    def _migrate_audit(self) -> None:
        """Give every ledger event a stable, unique, never-reused event sequence.

        ``event_seq`` orders audit items inside one ``recorded_at`` millisecond:
        every committed version append and every window truncation draws one
        value from one process-wide counter, in commit order, so the audit trail
        has a total order that survives a restart.  A database written before
        the audit entry point existed has no sequences yet; its rows are
        backfilled once here in a deterministic order that matches the order
        they were committed in, and a partial backfill (interrupted rows carry
        NULL) is finished before any new sequence is handed out, so every value
        stays unique and the same database always maps to the same sequence.
        """
        with self.lock:
            columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(versions)")
            }
            if "event_seq" not in columns:
                self.connection.execute("ALTER TABLE versions ADD COLUMN event_seq INTEGER")
            columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(truncations)")
            }
            if "event_seq" not in columns:
                self.connection.execute("ALTER TABLE truncations ADD COLUMN event_seq INTEGER")
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) AS high FROM (
                  SELECT MAX(event_seq) AS seq FROM versions
                  UNION ALL
                  SELECT MAX(event_seq) AS seq FROM truncations
                )
                """
            ).fetchone()
            next_seq = int(row["high"]) + 1
            # Versions committed in one write share one ``recorded_at``; the
            # per-attribute version counter and the stable attribute name fix
            # their order inside that millisecond, matching the order the
            # commit inserted them in.  ``rowid`` is the final tie-break: it is
            # assigned in insertion order, so rows that look identical on every
            # other key still keep their true commit order.
            next_seq = self._backfill_event_seq(
                """
                SELECT rowid AS rid
                  FROM versions
                 WHERE event_seq IS NULL
                 ORDER BY recorded_at,
                          type, id,
                          version,
                          attribute,
                          rowid
                """,
                "versions",
                next_seq,
            )
            # Several trims of one version commit in the order the corrections
            # landed; a later trim pulls the window to an earlier instant, so
            # among trims sharing one millisecond the larger bound is the
            # earlier commit.  Same-millisecond supersede rows can carry the
            # same bound, where ``rowid`` preserves the true insertion order.
            next_seq = self._backfill_event_seq(
                """
                SELECT rowid AS rid
                  FROM truncations
                 WHERE event_seq IS NULL
                 ORDER BY recorded_at,
                          type, id, version,
                          valid_end DESC,
                          attribute,
                          rowid
                """,
                "truncations",
                next_seq,
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS versions_by_event "
                "ON versions(recorded_at, event_seq)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS truncations_by_event "
                "ON truncations(recorded_at, event_seq)"
            )

    def _backfill_event_seq(
        self, select_sql: str, table: str, next_seq: int
    ) -> int:
        rows = self.connection.execute(select_sql).fetchall()
        for offset, row in enumerate(rows):
            self.connection.execute(
                f"UPDATE {table} SET event_seq = ? WHERE rowid = ?",
                (next_seq + offset, int(row["rid"])),
            )
        return next_seq + len(rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Serialise whole transactions: BEGIN IMMEDIATE takes the write lock at
        # once, and the RLock keeps every other thread (readers included) off the
        # shared connection until COMMIT, so a batch is atomic to the outside.
        with self.lock:
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
        with self.lock:
            return self.connection.execute(
                "SELECT created_at FROM entities WHERE type = ? AND id = ?",
                (entity_type, entity_id),
            ).fetchone()

    def insert_entity(self, entity_type: str, entity_id: str, created_at: int) -> None:
        self.connection.execute(
            "INSERT INTO entities(type, id, created_at) VALUES (?, ?, ?)",
            (entity_type, entity_id, created_at),
        )

    def entity_keys(
        self, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[tuple[str, str, int]]:
        """Every entity in scope, with its record instant, in stable order.

        A ``type`` alone selects every entity of that type, a ``type``/``id``
        pair one entity, and no argument the whole store.  Rows come back
        ordered by the stable identifier ``(type, id)``, so a caller walking
        the result always visits entities in the same sequence.
        """
        clauses = []
        parameters = []
        if entity_type is not None:
            clauses.append("type = ?")
            parameters.append(entity_type)
        if entity_id is not None:
            clauses.append("id = ?")
            parameters.append(entity_id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT type, id, created_at FROM entities{where} ORDER BY type, id",
                parameters,
            ).fetchall()
        return [(str(row["type"]), str(row["id"]), int(row["created_at"])) for row in rows]

    # -- versions -----------------------------------------------------------

    def versions_for_entity(self, entity_type: str, entity_id: str) -> list[sqlite3.Row]:
        with self.lock:
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
        with self.lock:
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
        with self.lock:
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
        with self.lock:
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
            # Draw one base and number the batch locally: the rows are only
            # inserted after every sequence is chosen, so asking the ledger for
            # the next value per event would read the same maximum each time.
            base = self.next_event_seq()
            numbered = [(*event, base + offset) for offset, event in enumerate(events)]
            self.connection.executemany(
                """
                INSERT INTO truncations(type, id, attribute, version, recorded_at, valid_end,
                                        event_seq)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                numbered,
            )
        return events

    def next_event_seq(self) -> int:
        """Draw the next process-wide audit event sequence.

        Called only inside a write transaction, so the values are handed out in
        commit order with no gaps; they never wrap and a rolled-back transaction
        discards the values it drew, which is safe because no committed row ever
        saw them.
        """
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM (
              SELECT MAX(event_seq) AS seq FROM versions
              UNION ALL
              SELECT MAX(event_seq) AS seq FROM truncations
            )
            """
        ).fetchone()
        return int(row["next"])

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
        event_seq: int | None = None,
    ) -> None:
        if event_seq is None:
            event_seq = self.next_event_seq()
        self.connection.execute(
            """
            INSERT INTO versions(type, id, attribute, version, operation, value,
                                 valid_from, valid_end, declared_end, recorded_at,
                                 event_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event_seq,
            ),
        )

    # -- audit --------------------------------------------------------------

    def max_event_seq(self) -> int:
        """Highest event sequence committed so far (0 in an empty ledger)."""
        with self.lock:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) AS high FROM (
                  SELECT MAX(event_seq) AS seq FROM versions
                  UNION ALL
                  SELECT MAX(event_seq) AS seq FROM truncations
                )
                """
            ).fetchone()
        return int(row["high"])

    def audit_events(
        self,
        *,
        high_seq: int,
        limit: int,
        after_recorded_at: int | None = None,
        after_event_seq: int | None = None,
        recorded_from: int | None = None,
        recorded_to: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        attribute: str | None = None,
        action: str | None = None,
    ) -> list[sqlite3.Row]:
        """One page of audit items, ordered by ``(recorded_at, event_seq)``.

        The audit trail is the union of committed version appends and window
        truncations.  ``high_seq`` fixes the result set: no event whose
        ``event_seq`` is greater (a concurrent write landing while a client is
        paging) can enter the pages, so pagination never repeats or skips an
        item.  Paging itself is keyset on ``(recorded_at, event_seq)``, the
        columns the pages are ordered by.
        """
        wanted_actions = {
            "version_appended": ["version_appended"],
            "window_truncated": ["window_truncated"],
            None: ["version_appended", "window_truncated"],
        }[action]
        branches = []
        parameters: list[Any] = []
        if "version_appended" in wanted_actions:
            branches.append(
                """
                SELECT 'version_appended' AS action, event_seq, recorded_at, type, id,
                       attribute, version, operation, value, valid_from, declared_end,
                       NULL AS trunc_valid_end
                  FROM versions
                """
            )
        if "window_truncated" in wanted_actions:
            branches.append(
                """
                SELECT 'window_truncated' AS action, event_seq, recorded_at, type, id,
                       attribute, version, NULL AS operation, NULL AS value,
                       NULL AS valid_from, NULL AS declared_end, valid_end AS trunc_valid_end
                  FROM truncations
                """
            )
        union = " UNION ALL ".join(branches)
        clauses = ["event_seq <= ?"]
        parameters.append(high_seq)
        if recorded_from is not None:
            clauses.append("recorded_at >= ?")
            parameters.append(recorded_from)
        if recorded_to is not None:
            clauses.append("recorded_at < ?")
            parameters.append(recorded_to)
        if entity_type is not None:
            clauses.append("type = ?")
            parameters.append(entity_type)
        if entity_id is not None:
            clauses.append("id = ?")
            parameters.append(entity_id)
        if attribute is not None:
            clauses.append("attribute = ?")
            parameters.append(attribute)
        if after_recorded_at is not None:
            clauses.append(
                "(recorded_at > ? OR (recorded_at = ? AND event_seq > ?))"
            )
            parameters.extend((after_recorded_at, after_recorded_at, after_event_seq))
        where = " WHERE " + " AND ".join(clauses)
        # Fetch one extra row so the caller knows whether another page exists
        # without counting the whole (possibly large) trail.
        sql = (
            f"SELECT * FROM ({union}){where} "
            "ORDER BY recorded_at, event_seq LIMIT ?"
        )
        parameters.append(limit + 1)
        with self.lock:
            return list(self.connection.execute(sql, parameters).fetchall())

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
