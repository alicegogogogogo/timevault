from __future__ import annotations

import hashlib
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

ACTION_VERSION_APPENDED = "version_appended"
ACTION_WINDOW_TRUNCATED = "window_truncated"


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
            CREATE TABLE IF NOT EXISTS schemas (
              type TEXT NOT NULL,
              version INTEGER NOT NULL,
              effective_from INTEGER NOT NULL,
              recorded_at INTEGER NOT NULL,
              attributes TEXT NOT NULL,
              PRIMARY KEY (type, version)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              recorded_at INTEGER NOT NULL,
              action TEXT NOT NULL,
              type TEXT NOT NULL,
              id TEXT NOT NULL,
              attribute TEXT NOT NULL,
              version INTEGER NOT NULL,
              operation TEXT,
              value TEXT,
              valid_from INTEGER,
              declared_end INTEGER,
              valid_end INTEGER
            );
            CREATE INDEX IF NOT EXISTS audit_events_scan
              ON audit_events(recorded_at, event_id);
            """
        )
        self._backfill_audit()

    @staticmethod
    def audit_event_id(*parts: Any) -> str:
        """Deterministic, collision-resistant identity for one audit item.

        The digest is taken over the immutable ledger coordinates of the event
        — its action, entity coordinates, version, transaction instant, and the
        payload the write committed — so the same committed row always derives
        the same id, while two distinct events cannot share one.  A version
        later corrected carries different coordinates and therefore a different
        id, so an old audit item can never be retroactively rewritten.
        """
        joined = "\x1f".join("" if part is None else str(part) for part in parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _backfill_audit(self) -> None:
        """Project rows written by older releases into the audit ledger.

        Databases created before the audit entry point existed hold committed
        ``versions`` and ``truncations`` rows but no audit items.  Every such
        row is described once here, in the same deterministic order fresh
        writes use: transaction time first, then stable entity and version
        coordinates.

        Each projection runs in one transaction, so a crash can never leave it
        half applied; the UNIQUE index on ``event_id`` makes a re-run idempotent
        as well, so reopening a database can never double-fill it.
        """
        with self.lock:
            already = self.connection.execute(
                "SELECT 1 FROM audit_events WHERE action = ? LIMIT 1",
                (ACTION_VERSION_APPENDED,),
            ).fetchone()
            if already is None:
                version_rows = self.connection.execute(
                    """
                    SELECT type, id, attribute, version, operation, value,
                           valid_from, declared_end, recorded_at
                      FROM versions
                     ORDER BY recorded_at, type, id, attribute, version
                    """
                ).fetchall()
                legacy: list[tuple[Any, ...]] = []
                for row in version_rows:
                    event_id = self.audit_event_id(
                        ACTION_VERSION_APPENDED,
                        row["type"], row["id"], row["attribute"], row["version"],
                        row["recorded_at"], row["operation"], row["value"],
                        row["valid_from"], row["declared_end"],
                    )
                    legacy.append(
                        (
                            event_id,
                            int(row["recorded_at"]),
                            ACTION_VERSION_APPENDED,
                            row["type"], row["id"], row["attribute"], int(row["version"]),
                            row["operation"], row["value"],
                            int(row["valid_from"]), row["declared_end"], None,
                        )
                    )
                if legacy:
                    self.connection.execute("BEGIN")
                    try:
                        self.connection.executemany(
                            """
                            INSERT OR IGNORE INTO audit_events(
                                event_id, recorded_at, action, type, id, attribute, version,
                                operation, value, valid_from, declared_end, valid_end)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            legacy,
                        )
                    except Exception:
                        self.connection.execute("ROLLBACK")
                        raise
                    else:
                        self.connection.execute("COMMIT")
            already = self.connection.execute(
                "SELECT 1 FROM audit_events WHERE action = ? LIMIT 1",
                (ACTION_WINDOW_TRUNCATED,),
            ).fetchone()
            if already is None:
                truncation_rows = self.connection.execute(
                    """
                    SELECT rowid, type, id, attribute, version, recorded_at, valid_end
                      FROM truncations
                     ORDER BY recorded_at, type, id, attribute, version, valid_end, rowid
                    """
                ).fetchall()
                legacy = []
                for row in truncation_rows:
                    event_id = self.audit_event_id(
                        ACTION_WINDOW_TRUNCATED,
                        row["type"], row["id"], row["attribute"], row["version"],
                        row["recorded_at"], row["valid_end"], row["rowid"],
                    )
                    legacy.append(
                        (
                            event_id,
                            int(row["recorded_at"]),
                            ACTION_WINDOW_TRUNCATED,
                            row["type"], row["id"], row["attribute"], int(row["version"]),
                            None, None, None, None, int(row["valid_end"]),
                        )
                    )
                if legacy:
                    self.connection.execute("BEGIN")
                    try:
                        self.connection.executemany(
                            """
                            INSERT OR IGNORE INTO audit_events(
                                event_id, recorded_at, action, type, id, attribute, version,
                                operation, value, valid_from, declared_end, valid_end)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            legacy,
                        )
                    except Exception:
                        self.connection.execute("ROLLBACK")
                        raise
                    else:
                        self.connection.execute("COMMIT")

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
            # Insert one row at a time so the stable rowid of each truncation is
            # known: a version can be superseded more than once inside one
            # batch, which yields truncation rows identical on every column, and
            # only their persistent rowids tell those distinct events apart.
            audit_rows: list[tuple[Any, ...]] = []
            for event in events:
                cursor = self.connection.execute(
                    """
                    INSERT INTO truncations(type, id, attribute, version, recorded_at, valid_end)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    event,
                )
                audit_rows.append(
                    (
                        self.audit_event_id(
                            ACTION_WINDOW_TRUNCATED,
                            event[0], event[1], event[2], event[3], event[4], event[5],
                            cursor.lastrowid,
                        ),
                        event[4],
                        ACTION_WINDOW_TRUNCATED,
                        event[0], event[1], event[2], event[3], event[5],
                    )
                )
            self.connection.executemany(
                """
                INSERT INTO audit_events(
                    event_id, recorded_at, action, type, id, attribute, version,
                    operation, value, valid_from, declared_end, valid_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
                """,
                audit_rows,
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
        encoded = None if value is None else self.encode(value)
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
                encoded,
                valid_from,
                valid_end,
                valid_end,
                recorded_at,
            ),
        )
        event_id = self.audit_event_id(
            ACTION_VERSION_APPENDED,
            entity_type, entity_id, attribute, version, recorded_at,
            operation, encoded, valid_from, valid_end,
        )
        self.connection.execute(
            """
            INSERT INTO audit_events(
                event_id, recorded_at, action, type, id, attribute, version,
                operation, value, valid_from, declared_end, valid_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                event_id,
                recorded_at,
                ACTION_VERSION_APPENDED,
                entity_type,
                entity_id,
                attribute,
                version,
                operation,
                encoded,
                valid_from,
                valid_end,
            ),
        )

    # -- schemas ------------------------------------------------------------

    def insert_schema(
        self,
        entity_type: str,
        version: int,
        effective_from: int,
        recorded_at: int,
        attributes: dict[str, str],
    ) -> None:
        """Append one schema version; existing versions are never touched."""
        self.connection.execute(
            """
            INSERT INTO schemas(type, version, effective_from, recorded_at, attributes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, version, effective_from, recorded_at, self.encode(attributes)),
        )

    def schemas_for_type(self, entity_type: str) -> list[sqlite3.Row]:
        """Every committed schema version of the type, oldest first."""
        with self.lock:
            return list(
                self.connection.execute(
                    """
                    SELECT version, effective_from, recorded_at, attributes
                      FROM schemas
                     WHERE type = ?
                     ORDER BY version
                    """,
                    (entity_type,),
                ).fetchall()
            )

    def next_schema_version(self, entity_type: str) -> int:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS next
                  FROM schemas
                 WHERE type = ?
                """,
                (entity_type,),
            ).fetchone()
        return int(row["next"])

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

    # -- audit --------------------------------------------------------------

    def max_audit_seq(self) -> int:
        """The sequence assigned so far; the upper bound a first page pins."""
        with self.lock:
            row = self.connection.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM audit_events").fetchone()
        return int(row["m"])

    def audit_key_exists(self, recorded_at: int, event_id: str, upper_seq: int) -> bool:
        """Whether a cursor's last-returned item is a real row in this store.

        The ledger is append-only, so a cursor this store issued always finds
        its position here.  A token minted against another database (or one
        pointing past the pinned result set) does not, which is how such a
        cursor is reported as unknown instead of silently walking foreign
        data.
        """
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM audit_events WHERE recorded_at = ? AND event_id = ? AND seq <= ?",
                (recorded_at, event_id, upper_seq),
            ).fetchone()
        return row is not None

    def audit_page(
        self,
        *,
        after_recorded_at: int | None,
        after_event_id: str | None,
        upper_seq: int,
        limit: int,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        attribute: str | None = None,
        recorded_from: int | None = None,
        recorded_to: int | None = None,
    ) -> list[sqlite3.Row]:
        """One stable page of audit items.

        Ordering is ``(recorded_at, event_id)`` ascending: ``recorded_at``
        puts the items on the transaction axis, and the event id breaks ties
        inside one millisecond.  Event ids derive from the immutable
        coordinates of the committed event, so that tie-break is stable, never
        reused, and identical after a restart — the order lives in the rows,
        not in a clock reading.

        Pagination is keyset-based: the cursor carries the last seen
        ``(recorded_at, event_id)`` pair, which is what makes pages overlap-
        and gap-free even when several events share a millisecond.
        ``upper_seq`` independently pins the result set: items committed
        after the first page was opened (their ``seq`` is higher) never
        enter later pages, so a concurrent writer cannot move the window
        the cursor walks.
        """
        clauses = ["seq <= ?"]
        parameters: list[Any] = [upper_seq]
        if after_recorded_at is None:
            # Nothing has been consumed yet: the lower recorded-time bound, if
            # any, is the only starting line.
            if recorded_from is not None:
                clauses.append("recorded_at >= ?")
                parameters.append(recorded_from)
        else:
            # Strictly after the last returned key, on the same ordered pair.
            clauses.append("(recorded_at > ? OR (recorded_at = ? AND event_id > ?))")
            parameters.extend([after_recorded_at, after_recorded_at, after_event_id])
            if recorded_from is not None:
                clauses.append("recorded_at >= ?")
                parameters.append(recorded_from)
        if action is not None:
            clauses.append("action = ?")
            parameters.append(action)
        if entity_type is not None:
            clauses.append("type = ?")
            parameters.append(entity_type)
        if entity_id is not None:
            clauses.append("id = ?")
            parameters.append(entity_id)
        if attribute is not None:
            clauses.append("attribute = ?")
            parameters.append(attribute)
        if recorded_to is not None:
            clauses.append("recorded_at < ?")
            parameters.append(recorded_to)
        sql = (
            "SELECT seq, event_id, recorded_at, action, type, id, attribute, version, "
            "operation, value, valid_from, declared_end, valid_end FROM audit_events"
            " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY recorded_at, event_id LIMIT ?"
        )
        parameters.append(limit + 1)
        with self.lock:
            return list(self.connection.execute(sql, parameters).fetchall())
