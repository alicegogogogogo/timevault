import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from timevault.errors import ConflictError, NotFoundError, ValidationError
from timevault.server import make_handler
from timevault.service import TimeVault

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)


class Clock:
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class AuditTests(unittest.TestCase):
    """The read-only audit trail at the service layer."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    def key(self) -> str:
        self.keys += 1
        return f"key-{self.keys}"

    def create(self, entity_id="acct-1", entity_type="account", **attributes):
        return self.vault.create_entity(
            entity_type,
            {
                "id": entity_id,
                "attributes": attributes or {"status": "active", "tier": "gold"},
                "valid_from": "2024-01-01T00:00:00Z",
            },
            self.key(),
        )

    def correct(self, facts, as_of=None, entity_id="acct-1", entity_type="account"):
        body = {"facts": facts}
        if as_of is not None:
            body["as_of"] = as_of
        return self.vault.apply_correction(entity_type, entity_id, body, self.key())

    def ids(self, items) -> list[str]:
        return [item["event_id"] for item in items]

    # -- event shape and ordering ------------------------------------------

    def test_creation_appends_one_version_event_per_attribute(self):
        self.create()
        items = self.vault.audit(limit=500)["items"]
        self.assertEqual(2, len(items))
        by_attribute = {item["attribute"]: item for item in items}
        tier = by_attribute["tier"]
        self.assertEqual(
            {
                "event_id",
                "recorded_at",
                "type",
                "id",
                "attribute",
                "version",
                "action",
                "operation",
                "value",
                "valid_from",
                "declared_end",
            },
            set(tier),
        )
        self.assertEqual("version_appended", tier["action"])
        self.assertEqual("assert", tier["operation"])
        self.assertEqual("gold", tier["value"])
        self.assertEqual(1, tier["version"])
        self.assertEqual("2024-01-01T00:00:00.000Z", tier["valid_from"])
        self.assertIsNone(tier["declared_end"])
        self.assertEqual("2024-05-01T00:00:00.000Z", tier["recorded_at"])
        self.assertEqual("account", tier["type"])
        self.assertEqual("acct-1", tier["id"])
        # Stable unique ids, ascending inside the one commit millisecond.
        self.assertEqual(["evt_00000000000000000001", "evt_00000000000000000002"], self.ids(items))
        self.assertEqual(["status", "tier"], [item["attribute"] for item in items])

    def test_correction_appends_a_truncation_then_the_new_version(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        items = self.vault.audit(limit=500)["items"]
        self.assertEqual(4, len(items))
        trim, appended = items[2], items[3]
        self.assertEqual("window_truncated", trim["action"])
        self.assertEqual(
            {"event_id", "recorded_at", "type", "id", "attribute", "version", "action", "valid_end"},
            set(trim),
        )
        self.assertEqual(1, trim["version"])
        self.assertEqual("2024-03-01T00:00:00.000Z", trim["valid_end"])
        self.assertEqual("2024-05-31T00:00:00.000Z", trim["recorded_at"])
        self.assertEqual("version_appended", appended["action"])
        self.assertEqual(2, appended["version"])
        self.assertEqual("platinum", appended["value"])
        self.assertEqual("assert", appended["operation"])
        # Trim first, then the append, inside their shared millisecond.
        self.assertLess(trim["event_id"], appended["event_id"])

    def test_retraction_is_appended_with_operation_retract_and_null_value(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "status", "deleted": True}], as_of="2024-03-01T00:00:00Z"
        )
        items = self.vault.audit(action="version_appended", limit=500)["items"]
        retraction = [item for item in items if item["version"] == 2 and item["attribute"] == "status"][0]
        self.assertEqual("retract", retraction["operation"])
        self.assertIsNone(retraction["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", retraction["valid_from"])
        # A retraction declares no end even though its stored window is empty.
        self.assertIsNone(retraction["declared_end"])

    def test_declared_end_is_reported_as_asserted_including_an_explicit_null_value(self):
        self.vault.create_entity(
            "account",
            {
                "id": "a",
                "attributes": {
                    "tier": "gold",
                    "note": None,
                    "level": 3,
                    "flag": True,
                },
                "valid_from": "2024-01-01T00:00:00Z",
            },
            self.key(),
        )
        self.correct(
            [
                {
                    "attribute": "tier",
                    "value": "trial",
                    "valid_from": "2024-02-01T00:00:00Z",
                    "valid_end": "2024-03-01T00:00:00Z",
                }
            ],
            entity_id="a",
        )
        items = self.vault.audit(action="version_appended", limit=500)["items"]
        by_key = {(item["attribute"], item["version"]): item for item in items}
        self.assertIsNone(by_key[("note", 1)]["value"])
        self.assertEqual(3, by_key[("level", 1)]["value"])
        self.assertIs(by_key[("flag", 1)]["value"], True)
        self.assertEqual(
            "2024-03-01T00:00:00.000Z", by_key[("tier", 2)]["declared_end"]
        )
        self.assertIsNone(by_key[("tier", 1)]["declared_end"])

    def test_events_are_ordered_by_recorded_at_then_event_id(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}]
        )
        items = self.vault.audit(limit=500)["items"]
        stamps = [item["recorded_at"] for item in items]
        self.assertEqual(stamps, sorted(stamps))
        ids = self.ids(items)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        # Repeating the query over the same data yields exactly the same trail.
        self.assertEqual(items, self.vault.audit(limit=500)["items"])

    def test_writes_in_the_same_millisecond_keep_a_total_order(self):
        # The clock does not advance: commit order alone separates the events.
        self.create()
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        items = self.vault.audit(limit=500)["items"]
        self.assertEqual(1, len({item["recorded_at"] for item in items}))
        self.assertEqual(self.ids(items), sorted(self.ids(items)))

    def test_every_event_of_a_batch_shares_the_batch_recorded_at(self):
        result = self.vault.run_batch(
            {
                "operations": [
                    {
                        "operation": "create",
                        "type": "account",
                        "id": "b1",
                        "attributes": {"a": 1, "b": 2},
                        "valid_from": "2024-01-01T00:00:00Z",
                    },
                    {
                        "operation": "correct",
                        "type": "account",
                        "id": "b1",
                        "as_of": "2024-03-01T00:00:00Z",
                        "facts": [{"attribute": "a", "value": 9}],
                    },
                ]
            },
            self.key(),
        )
        items = self.vault.audit(entity_type="account", entity_id="b1", limit=500)["items"]
        # Two appends from the create, one trim and one append from the correct.
        self.assertEqual(4, len(items))
        self.assertEqual({result["recorded_at"]}, {item["recorded_at"] for item in items})
        self.assertEqual(
            ["version_appended", "version_appended", "window_truncated", "version_appended"],
            [item["action"] for item in items],
        )

    # -- immutability -------------------------------------------------------

    def test_old_audit_items_are_not_rewritten_by_later_corrections(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        first = {
            item["event_id"]: item
            for item in self.vault.audit(limit=500)["items"]
        }
        # Trim the gold window a second time with a backdated restatement.
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}]
        )
        second = {
            item["event_id"]: item
            for item in self.vault.audit(limit=500)["items"]
        }
        for event_id, item in first.items():
            self.assertEqual(item, second[event_id])
        # The two trims of gold are separate items, each keeping the bound IT
        # learned; the older trim is not overwritten by the newer one.
        trims = [
            item
            for item in second.values()
            if item["action"] == "window_truncated"
            and item["attribute"] == "tier"
            and item["version"] == 1
        ]
        self.assertEqual(["2024-03-01T00:00:00.000Z", "2024-02-01T00:00:00.000Z"],
                         [item["valid_end"] for item in trims])

    def test_audit_items_locate_the_rows_of_plain_reads_and_history(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        projected = self.vault.entity_as_of("account", "acct-1", "2024-04-01T00:00:00Z")
        history = {
            (item["attribute"], item["version"]): item
            for section in self.vault.history("account", "acct-1")["attributes"]
            for item in section["versions"]
        }
        appended = [
            item
            for item in self.vault.audit(action="version_appended", limit=500)["items"]
            if item["attribute"] == "tier"
        ]
        for item in appended:
            row = history[(item["attribute"], item["version"])]
            self.assertEqual(item["value"], row["value"])
            self.assertEqual(item["operation"], row["operation"])
            self.assertEqual(item["valid_from"], row["valid_from"])
            self.assertEqual(item["recorded_at"], row["recorded_at"])
        live = projected["attributes"]["tier"]
        self.assertEqual(2, live["version"])
        self.assertEqual(
            "platinum",
            [item for item in appended if item["version"] == live["version"]][0]["value"],
        )

    # -- filters ------------------------------------------------------------

    def test_filters_narrow_the_trail(self):
        self.create(entity_id="acct-1")
        self.create(entity_id="acct-2", entity_type="device", name="router")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )

        self.assertEqual(
            {("account", "acct-1")},
            {(i["type"], i["id"]) for i in self.vault.audit(entity_type="account", limit=500)["items"]},
        )
        self.assertEqual(
            {("device", "acct-2")},
            {(i["type"], i["id"])
             for i in self.vault.audit(entity_type="device", entity_id="acct-2", limit=500)["items"]},
        )
        tier_only = self.vault.audit(attribute="tier", limit=500)["items"]
        self.assertTrue(tier_only)
        self.assertTrue(all(i["attribute"] == "tier" for i in tier_only))
        appends = self.vault.audit(action="version_appended", limit=500)["items"]
        self.assertTrue(all(i["action"] == "version_appended" for i in appends))
        trims = self.vault.audit(action="window_truncated", limit=500)["items"]
        self.assertEqual(1, len(trims))
        self.assertEqual("window_truncated", trims[0]["action"])

    def test_recorded_bounds_are_half_open(self):
        self.create()
        self.clock.advance(days=30)
        correction_at = "2024-05-31T00:00:00.000Z"
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        # recorded_from is inclusive: the correction's own millisecond is in.
        from_boundary = self.vault.audit(recorded_from=correction_at, limit=500)["items"]
        self.assertEqual(2, len(from_boundary))
        self.assertTrue(all(i["recorded_at"] >= correction_at for i in from_boundary))
        # recorded_to is exclusive: the same millisecond is out.
        to_boundary = self.vault.audit(recorded_to=correction_at, limit=500)["items"]
        self.assertEqual(2, len(to_boundary))
        self.assertTrue(all(i["recorded_at"] < correction_at for i in to_boundary))
        # Both forms accepted: RFC 3339 text and an integer of milliseconds.
        millis = self.vault.audit(recorded_from=1717113600000, limit=500)["items"]
        self.assertEqual(self.ids(from_boundary), self.ids(millis))
        # A legal interval that matches nothing is simply empty.
        self.assertEqual(
            [],
            self.vault.audit(
                recorded_from="2030-01-01T00:00:00Z", recorded_to="2030-02-01T00:00:00Z"
            )["items"],
        )

    def test_empty_result_is_200_shape_with_items_empty_and_null_cursor(self):
        # Nothing committed yet: the whole trail is empty, not an error.
        self.assertEqual({"items": [], "next_cursor": None}, self.vault.audit())
        self.create()
        # A legal scope that nothing ever fell in is empty too.
        self.assertEqual(
            {"items": [], "next_cursor": None},
            self.vault.audit(entity_type="account", entity_id="ghost"),
        )

    # -- pagination ----------------------------------------------------------

    def test_pagination_walks_every_item_once_with_a_null_final_cursor(self):
        self.vault.create_entity(
            "account",
            {"id": "a", "attributes": {"a": 1, "b": 2, "c": 3},
             "valid_from": "2024-01-01T00:00:00Z"},
            self.key(),
        )
        seen: list = []
        page = self.vault.audit(limit=2)
        seen.extend(page["items"])
        pages = 1
        while page["next_cursor"] is not None:
            # limit may change between pages.
            page = self.vault.audit(cursor=page["next_cursor"], limit=100)
            seen.extend(page["items"])
            pages += 1
        self.assertEqual(2, pages)
        self.assertEqual(["evt_%020d" % i for i in (1, 2, 3)], self.ids(seen))

    def test_first_cursor_pins_the_result_set_against_concurrent_writes(self):
        self.vault.create_entity(
            "account",
            {"id": "a", "attributes": {"a": 1, "b": 2, "c": 3},
             "valid_from": "2024-01-01T00:00:00Z"},
            self.key(),
        )
        first = self.vault.audit(limit=2)
        self.assertEqual(2, len(first["items"]))
        # A writer commits after the first page; its events must not enter the
        # pages the cursor leads to.
        self.clock.advance(days=10)
        self.correct([{"attribute": "a", "value": 9}], entity_id="a")
        rest = self.vault.audit(cursor=first["next_cursor"], limit=500)
        self.assertIsNone(rest["next_cursor"])
        self.assertEqual(
            ["evt_00000000000000000003"], self.ids(first["items"] + rest["items"])[2:]
        )
        self.assertEqual(
            ["evt_00000000000000000001", "evt_00000000000000000002", "evt_00000000000000000003"],
            self.ids(first["items"] + rest["items"]),
        )
        # A fresh query sees the committed write as part of a new result set.
        self.assertEqual(5, len(self.vault.audit(limit=500)["items"]))

    # -- validation ----------------------------------------------------------

    def test_id_requires_type(self):
        with self.assertRaisesRegex(ValidationError, "type"):
            self.vault.audit(entity_id="acct-1")

    def test_action_must_be_one_of_the_two_known_values(self):
        for bad in ("append", "VERSION_APPENDED", "trim"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.vault.audit(action=bad)

    def test_recorded_from_must_be_earlier_than_recorded_to(self):
        with self.assertRaisesRegex(ValidationError, "earlier"):
            self.vault.audit(
                recorded_from="2024-06-01T00:00:00Z", recorded_to="2024-05-01T00:00:00Z"
            )
        with self.assertRaisesRegex(ValidationError, "earlier"):
            self.vault.audit(
                recorded_from="2024-05-01T00:00:00Z", recorded_to="2024-05-01T00:00:00Z"
            )

    def test_limit_defaults_and_bounds(self):
        self.create()
        self.assertEqual(2, len(self.vault.audit()["items"]))  # fewer than the default page
        # More than 100 events: the default page size is exactly 100 with a cursor.
        self.vault.create_entity(
            "account",
            {"id": "many", "attributes": {f"a{i:03d}": i for i in range(101)}},
            self.key(),
        )
        default = self.vault.audit(entity_type="account", entity_id="many")
        self.assertEqual(100, len(default["items"]))
        self.assertIsNotNone(default["next_cursor"])
        self.assertEqual(1, len(self.vault.audit(limit=1)["items"]))
        # 101 fresh appends plus the two from the first entity fit inside 500.
        self.assertEqual(103, len(self.vault.audit(limit=500)["items"]))
        for bad in (0, 501, -1, "two", 1.5, True, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.vault.audit(limit=bad)

    def test_cursor_rejects_every_filter_but_limit(self):
        self.create()
        page = self.vault.audit(limit=1)
        for kwargs in (
            {"entity_type": "account"},
            {"entity_id": "acct-1"},
            {"attribute": "tier"},
            {"action": "version_appended"},
            {"recorded_from": "2024-01-01T00:00:00Z"},
            {"recorded_to": "2030-01-01T00:00:00Z"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValidationError, "cursor"):
                    self.vault.audit(cursor=page["next_cursor"], **kwargs)
        # limit alongside a cursor is fine.
        self.assertEqual(
            1, len(self.vault.audit(cursor=page["next_cursor"], limit=1)["items"])
        )

    def test_unknown_or_corrupt_cursors_are_validation_errors(self):
        for bad in ("garbage", "eyJhIjoxfQ", "!!!!", " ", 123):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.vault.audit(cursor=bad)

    def test_bad_time_and_identifier_arguments_are_validation_errors(self):
        self.create()
        for kwargs in (
            {"recorded_from": "yesterday"},
            {"recorded_to": "2024-13-01T00:00:00Z"},
            {"entity_type": "bad type"},
            {"entity_type": "account", "entity_id": "bad id"},
            {"attribute": "bad attr"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    self.vault.audit(**kwargs)

    # -- write semantics leave no audit traces on failure --------------------

    def test_failed_and_rolled_back_writes_produce_no_items(self):
        self.create()
        with self.assertRaises(ConflictError):
            self.vault.run_batch(
                {
                    "operations": [
                        {"operation": "create", "type": "account", "id": "ghost",
                         "attributes": {"q": 1}},
                        {"operation": "create", "type": "account", "id": "ghost",
                         "attributes": {"q": 2}},
                    ]
                },
                self.key(),
            )
        self.assertEqual([], self.vault.audit(entity_type="account", entity_id="ghost")["items"])
        # The rolled-back batch consumed no event sequences: the next committed
        # append takes the very next id after the creation's two.
        self.correct([{"attribute": "tier", "value": "x"}])
        self.assertEqual(
            "evt_00000000000000000004",
            self.vault.audit(action="version_appended", limit=500)["items"][-1]["event_id"],
        )

    def test_idempotent_replay_produces_no_new_items(self):
        self.create()
        self.clock.advance(days=30)
        first = self.vault.audit(limit=500)["items"]
        self.vault.apply_correction(
            "account",
            "acct-1",
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "replayed",
        )
        committed = self.vault.audit(limit=500)["items"]
        self.clock.advance(days=99)
        self.vault.apply_correction(
            "account",
            "acct-1",
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "replayed",
        )
        self.assertEqual(committed, self.vault.audit(limit=500)["items"])
        self.assertNotEqual(first, committed)

    def test_the_query_is_read_only(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        history_before = self.vault.history("account", "acct-1")
        audit_before = self.vault.audit(limit=500)
        for _ in range(5):
            self.vault.audit(limit=1)
            self.vault.audit(action="window_truncated", attribute="tier")
        self.assertEqual(history_before, self.vault.history("account", "acct-1"))
        self.assertEqual(audit_before, self.vault.audit(limit=500))


class LegacyAuditMigrationTests(unittest.TestCase):
    """Databases written before the audit entry point keep a queryable trail."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "old.db")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE entities (
              type TEXT NOT NULL, id TEXT NOT NULL, created_at INTEGER NOT NULL,
              PRIMARY KEY (type, id)
            );
            CREATE TABLE versions (
              type TEXT NOT NULL, id TEXT NOT NULL, attribute TEXT NOT NULL,
              version INTEGER NOT NULL, operation TEXT NOT NULL, value TEXT,
              valid_from INTEGER NOT NULL, valid_end INTEGER,
              declared_end INTEGER, recorded_at INTEGER NOT NULL
            );
            CREATE TABLE truncations (
              type TEXT NOT NULL, id TEXT NOT NULL, attribute TEXT NOT NULL,
              version INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
              valid_end INTEGER NOT NULL
            );
            CREATE TABLE idempotency (
              key TEXT PRIMARY KEY, operation TEXT NOT NULL, response TEXT NOT NULL
            );
            INSERT INTO entities VALUES ('account', 'old', 1714521600000);
            INSERT INTO versions VALUES
              ('account','old','tier',1,'assert','"gold"',
               1704067200000, 1709251200000, NULL, 1714521600000),
              ('account','old','status',1,'assert','"on"',
               1704067200000, NULL, NULL, 1714521600000);
            INSERT INTO truncations VALUES
              ('account','old','tier',1,1717113600000,1709251200000);
            """
        )
        connection.commit()
        connection.close()
        self.clock = Clock()

    def tearDown(self):
        self.directory.cleanup()

    def test_legacy_rows_are_backfilled_once_and_stay_stable_across_restarts(self):
        vault = TimeVault(self.db_path, self.clock)
        items = vault.audit(limit=500)["items"]
        self.assertEqual(["evt_%020d" % i for i in (1, 2, 3)], [i["event_id"] for i in items])
        # Versions come before the trim that was recorded later; inside the
        # creation millisecond version number then attribute name fix the order.
        self.assertEqual("status", items[0]["attribute"])
        self.assertEqual("tier", items[1]["attribute"])
        self.assertEqual("window_truncated", items[2]["action"])
        self.assertEqual("2024-03-01T00:00:00.000Z", items[2]["valid_end"])
        self.assertEqual("gold", items[1]["value"])

        # Reopening the same database changes nothing.
        reopened = TimeVault(self.db_path, self.clock)
        self.assertEqual(items, reopened.audit(limit=500)["items"])

        # New writes continue the sequence without colliding with backfilled ids.
        self.clock.advance(days=1)
        reopened.apply_correction(
            "account",
            "old",
            {"facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )
        appended = reopened.audit(action="version_appended", limit=500)["items"]
        self.assertEqual("evt_00000000000000000004", appended[-1]["event_id"])
        self.assertEqual("platinum", appended[-1]["value"])

    def test_a_partially_backfilled_database_is_finished_without_collisions(self):
        # Simulate an interrupted migration: the column exists, one row already
        # carries a sequence and the remaining rows are still NULL.
        connection = sqlite3.connect(self.db_path)
        connection.execute("ALTER TABLE versions ADD COLUMN event_seq INTEGER")
        connection.execute("ALTER TABLE truncations ADD COLUMN event_seq INTEGER")
        connection.execute(
            "UPDATE versions SET event_seq = 100 "
            "WHERE attribute = 'tier' AND version = 1"
        )
        connection.commit()
        connection.close()

        vault = TimeVault(self.db_path, self.clock)
        items = vault.audit(limit=500)["items"]
        sequences = sorted(int(item["event_id"].removeprefix("evt_")) for item in items)
        self.assertEqual([100, 101, 102], sequences)
        self.clock.advance(days=1)
        # A backdated correction landing inside status's open window trims it
        # and appends a version, drawing the next two sequences without
        # collision (103 for the trim, 104 for the appended version).
        vault.apply_correction(
            "account",
            "old",
            {"facts": [{"attribute": "status", "value": "x",
                        "valid_from": "2024-02-01T00:00:00Z"}]},
            "k",
        )
        sequences = [
            int(item["event_id"].removeprefix("evt_"))
            for item in vault.audit(limit=500)["items"]
        ]
        self.assertEqual(5, len(sequences))
        self.assertEqual(104, max(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))


class AuditHttpTests(unittest.TestCase):
    """GET /audit over the real socket server."""

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

    def call(self, path: str):
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def seed(self):
        body = json.dumps(
            {"id": "acct-1", "attributes": {"status": "active", "tier": "gold"},
             "valid_from": "2024-01-01T00:00:00Z"}
        ).encode()
        request = urllib.request.Request(
            self.base + "/entities/account", data=body, method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Idempotency-Key", "create-1")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(201, response.status)

    def test_empty_store_reports_empty_items_and_null_cursor(self):
        status, document = self.call("/audit")
        self.assertEqual(200, status)
        self.assertEqual({"items": [], "next_cursor": None}, document)

    def test_trail_filters_and_pagination_over_http(self):
        self.seed()
        status, page_one = self.call("/audit?limit=1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(page_one["items"]))
        self.assertIsNotNone(page_one["next_cursor"])
        self.assertEqual("version_appended", page_one["items"][0]["action"])

        cursor = urllib.parse.quote(page_one["next_cursor"], safe="")
        status, page_two = self.call(f"/audit?limit=1&cursor={cursor}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(page_two["items"]))
        self.assertEqual("evt_00000000000000000002", page_two["items"][0]["event_id"])
        self.assertIsNone(page_two["next_cursor"])

        status, filtered = self.call(
            "/audit?type=account&id=acct-1&attribute=tier"
            "&action=version_appended&recorded_from=2024-01-01T00:00:00Z"
            "&recorded_to=2030-01-01T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(filtered["items"]))
        self.assertEqual("gold", filtered["items"][0]["value"])

        # A legal range with nothing in it is an empty 200, not an error.
        status, empty = self.call(
            "/audit?recorded_from=2030-01-01T00:00:00Z&recorded_to=2030-02-01T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual({"items": [], "next_cursor": None}, empty)

    def test_validation_failures_are_400_validation_error(self):
        self.seed()
        for path in (
            "/audit?id=acct-1",
            "/audit?action=nope",
            "/audit?limit=0",
            "/audit?limit=501",
            "/audit?limit=abc",
            "/audit?recorded_from=2024-06-01T00:00:00Z&recorded_to=2024-05-01T00:00:00Z",
            "/audit?recorded_from=yesterday",
            "/audit?cursor=garbage",
            "/audit?unknown=1",
        ):
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(400, status)
                self.assertEqual("validation_error", body["error"]["code"])

    def test_cursor_cannot_carry_other_filters(self):
        self.seed()
        _, page = self.call("/audit?limit=1")
        cursor = urllib.parse.quote(page["next_cursor"], safe="")
        status, body = self.call(f"/audit?cursor={cursor}&type=account")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_only_get_is_routed(self):
        request = urllib.request.Request(self.base + "/audit", data=b"{}", method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
