import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from timevault.errors import ConflictError, NotFoundError, ValidationError
from timevault.server import make_handler
from timevault.service import TimeVault

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class AuditTests(unittest.TestCase):
    """GET /audit semantics at the service layer."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = str(Path(self.directory.name) / "vault.db")
        self.vault = TimeVault(self.path, self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    def key(self) -> str:
        self.keys += 1
        return f"k-{self.keys}"

    def create(self, entity_id="acct-1", entity_type="account", **attributes):
        payload = {
            "id": entity_id,
            "attributes": attributes or {"status": "active", "tier": "gold"},
            "valid_from": "2024-01-01T00:00:00Z",
        }
        return self.vault.create_entity(entity_type, payload, self.key())

    def correct(self, facts, as_of=None, entity_id="acct-1", entity_type="account"):
        body = {"facts": facts}
        if as_of is not None:
            body["as_of"] = as_of
        return self.vault.apply_correction(entity_type, entity_id, body, self.key())

    def audit(self, **kwargs):
        return self.vault.audit(**kwargs)

    def all_items(self, **kwargs):
        kwargs.setdefault("limit", 500)
        pages = []
        cursor = None
        while True:
            page = self.audit(cursor=cursor, **kwargs) if cursor else self.audit(**kwargs)
            pages.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return pages

    # -- item shape ---------------------------------------------------------

    def test_creation_appends_one_version_item_per_attribute(self):
        self.create()
        items = self.all_items()
        self.assertEqual(2, len(items))
        for item in items:
            self.assertEqual("version_appended", item["action"])
            self.assertEqual("account", item["type"])
            self.assertEqual("acct-1", item["id"])
            self.assertEqual(1, item["version"])
            self.assertEqual("assert", item["operation"])
            self.assertEqual("2024-01-01T00:00:00.000Z", item["valid_from"])
            self.assertIsNone(item["declared_end"])
            self.assertEqual("2024-05-01T00:00:00.000Z", item["recorded_at"])
            self.assertNotIn("valid_end", item)
            self.assertEqual(64, len(item["event_id"]))
        by_attribute = {item["attribute"]: item for item in items}
        self.assertEqual("gold", by_attribute["tier"]["value"])
        self.assertEqual("active", by_attribute["status"]["value"])

    def test_correction_appends_truncation_items_and_the_new_version(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        items = self.all_items()
        # Two version-1 appends at creation, one truncation of gold, one append
        # of platinum; the untouched status attribute has no truncation.
        actions = [(item["action"], item["attribute"], item["version"]) for item in items]
        self.assertIn(("window_truncated", "tier", 1), actions)
        self.assertIn(("version_appended", "tier", 2), actions)
        self.assertNotIn(("window_truncated", "status", 1), actions)

        truncation = next(
            item for item in items
            if item["action"] == "window_truncated" and item["attribute"] == "tier"
        )
        self.assertEqual(
            {"event_id", "recorded_at", "type", "id", "attribute", "version", "action", "valid_end"},
            set(truncation),
        )
        self.assertEqual("2024-03-01T00:00:00.000Z", truncation["valid_end"])
        self.assertEqual("2024-05-31T00:00:00.000Z", truncation["recorded_at"])

        appended = next(
            item for item in items
            if item["action"] == "version_appended" and item["attribute"] == "tier"
            and item["version"] == 2
        )
        self.assertEqual("platinum", appended["value"])
        self.assertEqual("assert", appended["operation"])

    def test_retraction_is_an_append_with_empty_window_and_null_value(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "status", "deleted": True}], as_of="2024-03-01T00:00:00Z"
        )
        item = next(
            item for item in self.all_items()
            if item["action"] == "version_appended"
            and item["attribute"] == "status" and item["version"] == 2
        )
        self.assertEqual("retract", item["operation"])
        self.assertIsNone(item["value"])
        self.assertEqual(item["valid_from"], item["declared_end"])
        self.assertEqual("2024-03-01T00:00:00.000Z", item["valid_from"])

    def test_bounded_assertion_records_its_declared_end_at_commit_time(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{
                "attribute": "region", "value": "emea",
                "valid_from": "2024-02-01T00:00:00Z",
                "valid_end": "2024-04-01T00:00:00Z",
            }]
        )
        item = next(
            item for item in self.all_items() if item["attribute"] == "region"
        )
        self.assertEqual("2024-02-01T00:00:00.000Z", item["valid_from"])
        self.assertEqual("2024-04-01T00:00:00.000Z", item["declared_end"])

    # -- ordering -----------------------------------------------------------

    def test_items_are_ordered_by_recorded_at_then_event_id(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct([{"attribute": "region", "value": "emea"}])
        items = self.all_items()
        recorded = [item["recorded_at"] for item in items]
        self.assertEqual(recorded, sorted(recorded))
        # Within one millisecond the tie-break is the event id, ascending.
        for earlier, later in zip(items, items[1:]):
            if earlier["recorded_at"] == later["recorded_at"]:
                self.assertLess(earlier["event_id"], later["event_id"])

    def test_order_and_event_ids_survive_a_restart(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        before = [(item["recorded_at"], item["event_id"]) for item in self.all_items()]
        reopened = TimeVault(self.path, self.clock)
        after = [(item["recorded_at"], item["event_id"]) for item in reopened.audit(limit=500)["items"]]
        self.assertEqual(before, after)

    def test_event_ids_are_unique_and_never_reused(self):
        self.create()
        for day in (30, 60, 90):
            self.clock.advance(days=30)
            self.correct(
                [{"attribute": "tier", "value": f"v{day}"}],
                as_of=f"2024-0{day // 30 + 1}-01T00:00:00Z",
            )
        ids = [item["event_id"] for item in self.all_items()]
        self.assertEqual(len(ids), len(set(ids)))

    # -- immutability and traceability -------------------------------------

    def test_later_corrections_do_not_rewrite_earlier_audit_items(self):
        self.create()
        first = self.all_items()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        second = self.all_items()
        # Every item from before the correction is byte-for-byte unchanged.
        first_ids = {item["event_id"]: item for item in first}
        for item in second:
            if item["event_id"] in first_ids:
                self.assertEqual(first_ids[item["event_id"]], item)
        # The gold append still reports the open-ended window it committed
        # with, even though the version row itself has since been trimmed.
        gold = first_ids[next(iter(first_ids))]
        gold_now = next(
            item for item in second
            if item["action"] == "version_appended" and item["attribute"] == "tier"
            and item["version"] == 1
        )
        self.assertIsNone(gold_now["declared_end"])
        self.assertEqual(gold, gold_now)

    def test_items_locate_ordinary_reads_and_history(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        history = {
            (section["attribute"], version["version"]): version
            for section in self.vault.history("account", "acct-1")["attributes"]
            for version in section["versions"]
        }
        for item in self.all_items():
            if item["action"] != "version_appended":
                continue
            version = history[(item["attribute"], item["version"])]
            self.assertEqual(item["operation"], version["operation"])
            self.assertEqual(item["value"], version["value"])
            self.assertEqual(item["valid_from"], version["valid_from"])
            self.assertEqual(item["recorded_at"], version["recorded_at"])

    # -- filters ------------------------------------------------------------

    def test_filters_type_id_attribute_and_action(self):
        self.create("acct-1")
        self.create("acct-2", tier="silver")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.assertEqual(
            {"acct-1"}, {item["id"] for item in self.all_items(entity_type="account", entity_id="acct-1")}
        )
        self.assertEqual(
            {"acct-2"}, {item["id"] for item in self.all_items(entity_type="account", entity_id="acct-2")}
        )
        self.assertTrue(
            all(item["attribute"] == "tier" for item in self.all_items(attribute="tier"))
        )
        truncated = self.all_items(action="window_truncated")
        self.assertTrue(truncated)
        self.assertTrue(all(item["action"] == "window_truncated" for item in truncated))
        appended = self.all_items(action="version_appended")
        self.assertTrue(all(item["action"] == "version_appended" for item in appended))

    def test_recorded_range_is_half_open(self):
        self.create()  # recorded 2024-05-01
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )  # recorded 2024-05-31
        middle = self.all_items(
            recorded_from="2024-05-01T00:00:00Z", recorded_to="2024-05-31T00:00:00Z"
        )
        self.assertTrue(middle)
        self.assertTrue(all(item["recorded_at"] == "2024-05-01T00:00:00.000Z" for item in middle))
        # The upper bound is exclusive: equal instants are rejected as a
        # degenerate (empty) interval, and the boundary itself is excluded.
        with self.assertRaises(ValidationError):
            self.all_items(
                recorded_from="2024-05-31T00:00:00Z", recorded_to="2024-05-31T00:00:00Z"
            )
        at_end = self.all_items(
            recorded_from="2024-05-31T00:00:00Z", recorded_to="2024-06-01T00:00:00Z"
        )
        self.assertTrue(at_end)
        self.assertTrue(all(item["recorded_at"] == "2024-05-31T00:00:00.000Z" for item in at_end))
        # Accepts the same instant forms the other transaction-time inputs use.
        numeric = self.all_items(recorded_from=1714521600000, recorded_to=1717113600000)
        self.assertEqual(
            [item["event_id"] for item in middle], [item["event_id"] for item in numeric]
        )

    def test_a_legal_range_with_no_matches_is_an_empty_page(self):
        self.create()
        document = self.audit(
            entity_type="ghost", recorded_from="2026-01-01T00:00:00Z",
            recorded_to="2026-02-01T00:00:00Z",
        )
        self.assertEqual([], document["items"])
        self.assertIsNone(document["next_cursor"])

    def test_empty_store_is_an_empty_result(self):
        document = self.audit()
        self.assertEqual({"items": [], "next_cursor": None}, document)

    # -- pagination ----------------------------------------------------------

    def test_pagination_walks_every_item_once_with_limit_one(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        expected = self.all_items()
        seen = []
        document = self.audit(limit=1)
        while True:
            seen.extend(document["items"])
            if document["next_cursor"] is None:
                break
            document = self.audit(limit=1, cursor=document["next_cursor"])
        self.assertEqual([item["event_id"] for item in expected],
                         [item["event_id"] for item in seen])

    def test_first_cursor_pins_the_result_set_against_concurrent_writes(self):
        self.create()
        pinned_ids = {item["event_id"] for item in self.all_items()}
        self.assertEqual(2, len(pinned_ids))
        # First page pins the result set to what has committed so far.
        first = self.audit(limit=1)
        self.assertEqual(1, len(first["items"]))
        self.assertIsNotNone(first["next_cursor"])

        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        new_ids = {item["event_id"] for item in self.all_items()} - pinned_ids
        self.assertTrue(new_ids)

        # Walking the pinned cursor never shows the later commit and ends
        # cleanly with a null cursor, even though the ledger grew.
        walked = list(first["items"])
        cursor = first["next_cursor"]
        while cursor is not None:
            page = self.audit(limit=2, cursor=cursor)
            walked.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(pinned_ids, {item["event_id"] for item in walked})

        # A fresh first page over the same filter sees the later write.
        fresh = self.audit(limit=500)
        self.assertTrue(new_ids <= {item["event_id"] for item in fresh["items"]})

    def test_limit_may_change_between_pages_but_filters_may_not(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        first = self.audit(limit=2)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        # Different limit on the same cursor is allowed.
        page = self.audit(limit=1, cursor=cursor)
        self.assertEqual(1, len(page["items"]))
        # Repeating any other filter alongside a cursor is rejected.
        for kwargs in (
            {"entity_type": "account"},
            {"entity_id": "acct-1"},
            {"attribute": "tier"},
            {"action": "version_appended"},
            {"recorded_from": "2024-01-01T00:00:00Z"},
            {"recorded_to": "2025-01-01T00:00:00Z"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    self.audit(cursor=cursor, **kwargs)

    def test_cursor_preserves_its_filters_across_pages(self):
        self.create("acct-1")
        self.create("acct-2", tier="silver")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        first = self.audit(entity_type="account", entity_id="acct-2", limit=1)
        walked = list(first["items"])
        cursor = first["next_cursor"]
        while cursor is not None:
            page = self.audit(limit=1, cursor=cursor)
            walked.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertTrue(walked)
        self.assertTrue(all(item["id"] == "acct-2" for item in walked))

    # -- validation ---------------------------------------------------------

    def test_id_requires_type(self):
        with self.assertRaisesRegex(ValidationError, "requires a type"):
            self.audit(entity_id="acct-1")

    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "version_appended or window_truncated"):
            self.audit(action="deleted")

    def test_time_bounds_must_be_ordered(self):
        with self.assertRaisesRegex(ValidationError, "strictly earlier"):
            self.audit(
                recorded_from="2024-06-01T00:00:00Z",
                recorded_to="2024-05-01T00:00:00Z",
            )

    def test_limit_bounds_and_shape(self):
        self.assertEqual(100, self.vault._parse_audit_limit(None))
        for value in (0, -1, 501, 1000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError, "between 1 and 500"):
                    self.audit(limit=value)
        for value in ("abc", "1.5", 1.5, True, [1]):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.audit(limit=value)
        self.create()
        document = self.audit(limit=1)
        self.assertEqual(1, len(document["items"]))
        document = self.audit(limit="2")
        self.assertEqual(2, len(document["items"]))
        self.audit(limit=500)

    def test_malformed_and_unknown_cursors_are_rejected(self):
        for token in ("", "garbage", "aaa.bbb", "...", "a" * 100):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValidationError, "cursor is unknown or malformed"):
                    self.audit(cursor=token)
        # A corrupted signature is rejected.
        from timevault.service import _encode_cursor, AuditFilters
        foreign = _encode_cursor(AuditFilters(), 1, None)
        with self.assertRaises(ValidationError):
            self.audit(cursor=foreign[:-4] + "xxxx")
        # A structurally valid token whose pinned position belongs to a
        # different ledger is unknown to this one.
        other_directory = tempfile.TemporaryDirectory()
        try:
            other = TimeVault(
                str(Path(other_directory.name) / "other.db"), self.clock
            )
            other.create_entity(
                "account",
                {"id": "foreign", "attributes": {"tier": "gold", "status": "on"},
                 "valid_from": "2024-01-01T00:00:00Z"},
                "k",
            )
            first = other.audit(limit=1)
            self.assertIsNotNone(first["next_cursor"])
            with self.assertRaisesRegex(ValidationError, "cursor is unknown or malformed"):
                self.audit(limit=1, cursor=first["next_cursor"])
        finally:
            other_directory.cleanup()

    def test_bad_filter_shapes_are_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.audit(entity_type="bad type")
        with self.assertRaises(ValidationError):
            self.audit(entity_type="account", entity_id="bad id")
        with self.assertRaises(ValidationError):
            self.audit(attribute="bad attr")
        with self.assertRaises(ValidationError):
            self.audit(recorded_from="yesterday")
        with self.assertRaises(ValidationError):
            self.audit(recorded_to=True)

    # -- write behaviour ----------------------------------------------------

    def test_failed_and_rolled_back_writes_emit_nothing(self):
        self.create()
        before = self.all_items()
        # A correction on a missing entity rolls back.
        with self.assertRaises(NotFoundError):
            self.correct([{"attribute": "tier", "value": "x"}], entity_id="ghost")
        # A correction that predates the entity conflicts and rolls back.
        self.clock.advance(days=-2)
        with self.assertRaises(ConflictError):
            self.correct([{"attribute": "tier", "value": "x"}])
        self.assertEqual(before, self.all_items())

    def test_idempotent_replay_emits_no_new_items(self):
        self.create()
        payload = {"id": "acct-2", "attributes": {"tier": "gold"},
                   "valid_from": "2024-01-01T00:00:00Z"}
        self.vault.create_entity("account", payload, "same")
        first = self.all_items()
        self.clock.advance(days=20)
        self.vault.create_entity("account", payload, "same")
        self.assertEqual(first, self.all_items())

    def test_a_failed_batch_emits_nothing_and_a_batch_shares_recorded_at(self):
        with self.assertRaises(ConflictError):
            self.vault.run_batch(
                {"operations": [
                    {"operation": "create", "type": "account", "id": "ok",
                     "attributes": {"tier": "gold"}},
                    {"operation": "create", "type": "account", "id": "ok",
                     "attributes": {"tier": "silver"}},
                ]},
                "batch-bad",
            )
        self.assertEqual([], self.all_items())

        self.clock.advance(days=3)
        result = self.vault.run_batch(
            {"operations": [
                {"operation": "create", "type": "account", "id": "acct-1",
                 "attributes": {"tier": "gold"}, "valid_from": "2024-01-01T00:00:00Z"},
                {"operation": "correct", "type": "account", "id": "acct-1",
                 "as_of": "2024-03-01T00:00:00Z",
                 "facts": [{"attribute": "tier", "value": "platinum"}]},
            ]},
            "batch-1",
        )
        items = self.all_items()
        self.assertTrue(items)
        self.assertEqual(
            {result["recorded_at"]}, {item["recorded_at"] for item in items}
        )
        # Replaying the batch adds no audit items.
        self.clock.advance(days=40)
        self.vault.run_batch(
            {"operations": [
                {"operation": "create", "type": "account", "id": "acct-1",
                 "attributes": {"tier": "gold"}, "valid_from": "2024-01-01T00:00:00Z"},
                {"operation": "correct", "type": "account", "id": "acct-1",
                 "as_of": "2024-03-01T00:00:00Z",
                 "facts": [{"attribute": "tier", "value": "platinum"}]},
            ]},
            "batch-1",
        )
        self.assertEqual(len(items), len(self.all_items()))

    def test_identical_truncations_in_one_batch_are_distinct_items(self):
        """A version superseded twice in one batch logs two identical trims.

        Consecutive backdated corrections in one transaction can close the same
        version at the same effective instant, producing truncation rows equal
        on every business column.  They are still two distinct events and must
        get two distinct, stable event ids rather than collapsing on the
        uniqueness constraint.
        """
        self.clock.advance(days=3)
        self.vault.run_batch(
            {"operations": [
                {"operation": "create", "type": "account", "id": "a",
                 "attributes": {"tier": "gold"},
                 "valid_from": "2024-02-01T00:00:00Z"},
                {"operation": "correct", "type": "account", "id": "a",
                 "facts": [{"attribute": "tier", "value": "platinum",
                            "valid_from": "2024-03-01T00:00:00Z"}]},
                {"operation": "correct", "type": "account", "id": "a",
                 "facts": [{"attribute": "tier", "value": "silver",
                            "valid_from": "2024-02-15T00:00:00Z"}]},
            ]},
            "batch-1",
        )
        items = self.all_items()
        ids = [item["event_id"] for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({"2024-05-04T00:00:00.000Z"}, {item["recorded_at"] for item in items})
        # Within the single shared millisecond ordering is by event id.
        self.assertEqual(ids, sorted(ids))

    def test_audit_is_read_only(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        before = self.all_items()
        history_before = self.vault.history("account", "acct-1")
        for _ in range(5):
            self.audit()
            self.audit(limit=1)
        self.assertEqual(before, self.all_items())
        self.assertEqual(history_before, self.vault.history("account", "acct-1"))

    # -- legacy database ----------------------------------------------------

    def test_versions_and_truncations_from_an_upgraded_database_are_auditable(self):
        # Build the schema a deployment before the audit entry point had, in a
        # database TimeVault itself has never opened.
        legacy_path = str(Path(self.directory.name) / "legacy.db")
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            """
            CREATE TABLE entities (
              type TEXT NOT NULL, id TEXT NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY (type, id));
            CREATE TABLE versions (
              type TEXT NOT NULL, id TEXT NOT NULL, attribute TEXT NOT NULL,
              version INTEGER NOT NULL, operation TEXT NOT NULL, value TEXT,
              valid_from INTEGER NOT NULL, valid_end INTEGER,
              declared_end INTEGER, recorded_at INTEGER NOT NULL,
              PRIMARY KEY (type, id, attribute, version));
            CREATE TABLE truncations (
              type TEXT NOT NULL, id TEXT NOT NULL, attribute TEXT NOT NULL,
              version INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
              valid_end INTEGER NOT NULL);
            CREATE TABLE idempotency (
              key TEXT PRIMARY KEY, operation TEXT NOT NULL, response TEXT NOT NULL);
            """
        )
        legacy.execute(
            "INSERT INTO entities VALUES (?, ?, ?)",
            ("account", "acct-1", 1714521600000),
        )
        legacy.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("account", "acct-1", "tier", 1, "assert", '"gold"',
             1704067200000, 1709251200000, None, 1714521600000),
        )
        legacy.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("account", "acct-1", "tier", 2, "assert", '"platinum"',
             1709251200000, None, None, 1717113600000),
        )
        legacy.execute(
            "INSERT INTO truncations VALUES (?, ?, ?, ?, ?, ?)",
            ("account", "acct-1", "tier", 1, 1717113600000, 1709251200000),
        )
        legacy.commit()
        legacy.close()

        reopened = TimeVault(legacy_path, self.clock)
        items = reopened.audit(limit=500)["items"]
        self.assertEqual(3, len(items))
        self.assertEqual(
            ["2024-05-01T00:00:00.000Z",
             "2024-05-31T00:00:00.000Z",
             "2024-05-31T00:00:00.000Z"],
            [item["recorded_at"] for item in items],
        )
        # Within one millisecond the truncation and the version-2 append keep
        # a stable event-id order.
        self.assertEqual(
            sorted((item["recorded_at"], item["event_id"]) for item in items),
            [(item["recorded_at"], item["event_id"]) for item in items],
        )
        truncation = next(item for item in items if item["action"] == "window_truncated")
        self.assertEqual(1, truncation["version"])
        self.assertEqual("2024-03-01T00:00:00.000Z", truncation["valid_end"])
        appended_gold = next(
            item for item in items
            if item["action"] == "version_appended" and item["version"] == 1
        )
        self.assertEqual("gold", appended_gold["value"])
        self.assertIsNone(appended_gold["declared_end"])

        # Reopening again must not double-project the legacy rows.
        again = TimeVault(legacy_path, self.clock)
        self.assertEqual(3, len(again.audit(limit=500)["items"]))


class AuditHttpTests(unittest.TestCase):
    """The real socket server: routing, status codes, envelope."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.vault))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def call(self, method: str, path: str, body=None, key: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if key:
            request.add_header("Idempotency-Key", key)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def seed(self):
        self.call(
            "POST", "/entities/account",
            {"id": "acct-1", "attributes": {"tier": "gold"},
             "valid_from": "2024-01-01T00:00:00Z"},
            "create-1",
        )
        self.clock.advance(days=30)
        self.call(
            "PUT", "/entities/account/acct-1",
            {"as_of": "2024-03-01T00:00:00Z",
             "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )

    def test_empty_audit_is_200_with_empty_items_and_null_cursor(self):
        status, document = self.call("GET", "/audit")
        self.assertEqual(200, status)
        self.assertEqual({"items": [], "next_cursor": None}, document)

    def test_audit_over_http_filters_and_paginates(self):
        self.seed()
        status, document = self.call("GET", "/audit?limit=1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(document["items"]))
        self.assertIsNotNone(document["next_cursor"])
        seen = list(document["items"])
        cursor = document["next_cursor"]
        while cursor is not None:
            from urllib.parse import quote
            status, page = self.call("GET", f"/audit?limit=1&cursor={quote(cursor, safe='')}")
            self.assertEqual(200, status)
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        status, truncated = self.call("GET", "/audit?action=window_truncated")
        self.assertEqual(200, status)
        self.assertTrue(truncated["items"])
        self.assertTrue(all(i["action"] == "window_truncated" for i in truncated["items"]))

        status, empty = self.call("GET", "/audit?type=account&id=acct-2")
        self.assertEqual(200, status)
        self.assertEqual([], empty["items"])
        self.assertIsNone(empty["next_cursor"])

        status, ranged = self.call(
            "GET",
            "/audit?recorded_from=2024-05-31T00:00:00Z&recorded_to=2024-06-01T00:00:00Z",
        )
        self.assertEqual(200, status)
        self.assertTrue(ranged["items"])
        self.assertTrue(
            all(i["recorded_at"] == "2024-05-31T00:00:00.000Z" for i in ranged["items"])
        )

    def test_audit_validation_errors_over_http(self):
        cases = [
            "/audit?id=acct-1",
            "/audit?action=deleted",
            "/audit?recorded_from=2024-06-01T00:00:00Z&recorded_to=2024-05-01T00:00:00Z",
            "/audit?limit=0",
            "/audit?limit=501",
            "/audit?limit=abc",
            "/audit?nope=1",
            "/audit?cursor=garbage",
            "/audit?type=bad%20type",
            "/audit?recorded_from=yesterday",
        ]
        for path in cases:
            with self.subTest(path=path):
                status, body = self.call("GET", path)
                self.assertEqual(400, status)
                self.assertEqual("validation_error", body["error"]["code"])

    def test_audit_rejects_post(self):
        status, body = self.call("POST", "/audit")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
