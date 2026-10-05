import threading
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timevault import TimeVault, Viewpoint
from timevault.service import DIFF_ADDED, DIFF_CHANGED, DIFF_REMOVED

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)


class Clock:
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class ViewComparisonTests(unittest.TestCase):
    """Historical view diffing at two complete (as_of, known_at) viewpoints."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0
        self.left = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        self.right = Viewpoint("2024-04-01T00:00:00Z", "2024-07-15T00:00:00Z")

    def tearDown(self):
        self.directory.cleanup()

    # -- helpers ------------------------------------------------------------

    def create(self, entity_id="acct-1", entity_type="account", key=None, **attributes):
        self.keys += 1
        payload = {
            "id": entity_id,
            "attributes": attributes or {"status": "active", "tier": "gold"},
            "valid_from": "2024-01-01T00:00:00Z",
        }
        return self.vault.create_entity(entity_type, payload, key or f"create-{self.keys}")

    def correct(self, facts, entity_id="acct-1", entity_type="account", as_of=None, key=None):
        self.keys += 1
        body = {"facts": facts}
        if as_of is not None:
            body["as_of"] = as_of
        return self.vault.apply_correction(
            entity_type, entity_id, body, key or f"fix-{self.keys}"
        )

    def compare(self, left=None, right=None, record_ids=None):
        return self.vault.compare_views(left or self.left, right or self.right, record_ids)

    def by_id(self, document):
        return {entry["id"]: entry for entry in document["differences"]}

    # -- categories ---------------------------------------------------------

    def test_added_removed_and_changed_between_two_viewpoints(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "region", "value": "emea"},
                {"attribute": "status", "deleted": True},
            ],
            as_of="2024-03-01T00:00:00Z",
        )
        self.clock.advance(days=30)
        self.create("acct-2", tier="silver")

        document = self.compare()
        entries = self.by_id(document)
        self.assertEqual({"acct-1", "acct-2"}, set(entries))

        added = entries["acct-2"]
        self.assertEqual(DIFF_ADDED, added["difference"])
        self.assertIsNone(added["left"])
        self.assertEqual("acct-2", added["right"]["id"])
        self.assertEqual("silver", added["right"]["attributes"]["tier"]["value"])

        changed = entries["acct-1"]
        self.assertEqual(DIFF_CHANGED, changed["difference"])
        self.assertEqual("gold", changed["left"]["attributes"]["tier"]["value"])
        self.assertEqual("active", changed["left"]["attributes"]["status"]["value"])
        self.assertNotIn("region", changed["left"]["attributes"])
        self.assertEqual("platinum", changed["right"]["attributes"]["tier"]["value"])
        self.assertEqual("emea", changed["right"]["attributes"]["region"]["value"])
        self.assertNotIn("status", changed["right"]["attributes"])

    def test_a_record_visible_only_on_the_left_is_removed(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "deleted": True}, {"attribute": "status", "deleted": True}],
            as_of="2024-03-01T00:00:00Z",
        )
        entries = self.by_id(self.compare())
        self.assertEqual(["acct-1"], list(entries))
        removed = entries["acct-1"]
        self.assertEqual(DIFF_REMOVED, removed["difference"])
        self.assertIsNone(removed["right"])
        self.assertEqual("gold", removed["left"]["attributes"]["tier"]["value"])

    def test_identical_viewpoints_return_no_differences(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        facet = Viewpoint("2024-04-01T00:00:00Z", "2024-06-15T00:00:00Z")
        document = self.vault.compare_views(facet, Viewpoint(1711929600000, 1718409600000))
        self.assertEqual([], document["differences"])
        # Repeated calls with the same data and arguments return the same order.
        self.assertEqual(document, self.vault.compare_views(facet, facet))

    def test_empty_store_and_missing_records_yield_nothing(self):
        document = self.compare()
        self.assertEqual([], document["differences"])
        self.create("acct-1")
        # The entity exists but was not recorded at either transaction time.
        early = Viewpoint("2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z")
        self.assertEqual([], self.vault.compare_views(early, early)["differences"])

    # -- visibility semantics -----------------------------------------------

    def test_a_later_correction_never_leaks_into_the_earlier_view(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        # Same valid instant on both sides; only the transaction time moves
        # across the correction's record instant.
        left = Viewpoint("2024-04-01T00:00:00Z", "2024-05-15T00:00:00Z")
        right = Viewpoint("2024-04-01T00:00:00Z", "2024-06-15T00:00:00Z")
        entries = self.by_id(self.vault.compare_views(left, right))
        entry = entries["acct-1"]
        self.assertEqual(DIFF_CHANGED, entry["difference"])
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertIsNone(entry["left"]["attributes"]["tier"]["valid_end"])
        self.assertEqual(1, entry["left"]["attributes"]["tier"]["version"])
        self.assertEqual("platinum", entry["right"]["attributes"]["tier"]["value"])
        self.assertEqual(2, entry["right"]["attributes"]["tier"]["version"])

    def test_a_backdated_interval_split_is_a_change_without_exposing_middle_versions(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}],
            key="split-1",
        )
        # Left reader knows only gold; right reader sees the split windows.
        # At 2024-02-15 the right observes silver, and the displaced gold is
        # only visible through its surviving earlier window — never as an
        # intermediate internal version.
        left = Viewpoint("2024-02-15T00:00:00Z", "2024-05-15T00:00:00Z")
        right = Viewpoint("2024-02-15T00:00:00Z", "2024-07-15T00:00:00Z")
        entry = self.by_id(self.vault.compare_views(left, right))["acct-1"]
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("silver", entry["right"]["attributes"]["tier"]["value"])
        self.assertEqual(
            "2024-03-01T00:00:00.000Z", entry["right"]["attributes"]["tier"]["valid_end"]
        )

    def test_version_metadata_alone_makes_a_change(self):
        """Unlike snapshot-diff, the public version metadata is observable."""
        self.create("acct-1", tier="gold")
        self.clock.advance(days=30)
        # Re-assert the identical value over the identical window: only the
        # version number and record transaction instant move.
        self.correct(
            [{"attribute": "tier", "value": "gold", "valid_from": "2024-01-01T00:00:00Z"}]
        )
        left = Viewpoint("2024-06-01T00:00:00Z", "2024-05-15T00:00:00Z")
        right = Viewpoint("2024-06-01T00:00:00Z", "2024-06-15T00:00:00Z")
        entry = self.by_id(self.vault.compare_views(left, right))["acct-1"]
        self.assertEqual(DIFF_CHANGED, entry["difference"])
        self.assertEqual(1, entry["left"]["attributes"]["tier"]["version"])
        self.assertEqual(2, entry["right"]["attributes"]["tier"]["version"])
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("gold", entry["right"]["attributes"]["tier"]["value"])

    def test_a_trimmed_window_bound_is_a_change(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=30)
        self.correct(
            [
                {
                    "attribute": "tier",
                    "value": "gold",
                    "valid_from": "2024-01-01T00:00:00Z",
                    "valid_end": "2024-03-01T00:00:00Z",
                }
            ]
        )
        left = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        right = Viewpoint("2024-02-01T00:00:00Z", "2024-06-15T00:00:00Z")
        entry = self.by_id(self.vault.compare_views(left, right))["acct-1"]
        self.assertIsNone(entry["left"]["attributes"]["tier"]["valid_end"])
        self.assertEqual(
            "2024-03-01T00:00:00.000Z", entry["right"]["attributes"]["tier"]["valid_end"]
        )

    def test_boolean_and_number_values_stay_distinct(self):
        """A later correction that replaces 1 with true is a business change."""
        self.create("acct-1", tier=1)
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": True, "valid_from": "2024-01-01T00:00:00Z"}]
        )
        left = Viewpoint("2024-06-01T00:00:00Z", "2024-05-15T00:00:00Z")
        right = Viewpoint("2024-06-01T00:00:00Z", "2024-06-15T00:00:00Z")
        entry = self.by_id(self.vault.compare_views(left, right))["acct-1"]
        self.assertEqual(DIFF_CHANGED, entry["difference"])
        self.assertEqual(1, entry["left"]["attributes"]["tier"]["value"])
        self.assertIs(True, entry["right"]["attributes"]["tier"]["value"])

    def test_valid_time_movement_alone_is_compared(self):
        self.create("acct-1")
        self.clock.advance(days=60)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        left = Viewpoint("2024-02-01T00:00:00Z", "2024-07-15T00:00:00Z")
        right = Viewpoint("2024-04-01T00:00:00Z", "2024-07-15T00:00:00Z")
        entry = self.by_id(self.vault.compare_views(left, right))["acct-1"]
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", entry["right"]["attributes"]["tier"]["value"])

    # -- filtering and ordering ---------------------------------------------

    def test_results_are_stably_sorted_by_logical_record_id(self):
        self.create("acct-b")
        self.create("acct-a")
        self.create("dev-1", entity_type="device", status="on")
        self.clock.advance(days=30)
        # Mix every category into one store: acct-a changes value, acct-b is
        # withdrawn, and dev-1 changes value across the two viewpoints.
        self.correct(
            [{"attribute": "tier", "value": "platinum"}],
            entity_id="acct-a",
            as_of="2024-03-01T00:00:00Z",
            key="fix-a",
        )
        self.correct(
            [{"attribute": "tier", "deleted": True}, {"attribute": "status", "deleted": True}],
            entity_id="acct-b",
            as_of="2024-03-01T00:00:00Z",
            key="fix-b",
        )
        self.correct(
            [{"attribute": "status", "value": "off"}],
            entity_id="dev-1",
            entity_type="device",
            as_of="2024-03-01T00:00:00Z",
            key="fix-d",
        )
        document = self.compare()
        self.assertEqual(
            [("account", "acct-a"), ("account", "acct-b"), ("device", "dev-1")],
            [(entry["type"], entry["id"]) for entry in document["differences"]],
        )
        self.assertEqual(
            [DIFF_CHANGED, DIFF_REMOVED, DIFF_CHANGED],
            [entry["difference"] for entry in document["differences"]],
        )
        self.assertEqual(document, self.compare())

    def test_identifier_filter_dedupes_and_skips_unknown_records(self):
        self.create("acct-1")
        self.create("acct-2")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}],
            entity_id="acct-2",
            as_of="2024-03-01T00:00:00Z",
            key="fix-2",
        )
        ids = [
            ("account", "acct-2"),
            ("account", "acct-2"),
            ("account", "ghost"),
            ("account", "acct-2"),
        ]
        document = self.compare(record_ids=ids)
        self.assertEqual(["acct-2"], [entry["id"] for entry in document["differences"]])
        # The caller's collection is never modified.
        self.assertEqual(
            [("account", "acct-2"), ("account", "acct-2"),
             ("account", "ghost"), ("account", "acct-2")],
            ids,
        )

    def test_identifier_filter_accepts_a_set_and_a_generator(self):
        self.create("acct-1")
        self.create("acct-2")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}],
            entity_id="acct-1",
            as_of="2024-03-01T00:00:00Z",
            key="fix-1",
        )
        wanted = {("account", "acct-1")}
        document = self.compare(record_ids=wanted)
        self.assertEqual(["acct-1"], [entry["id"] for entry in document["differences"]])
        document = self.compare(record_ids=(pair for pair in wanted))
        self.assertEqual(["acct-1"], [entry["id"] for entry in document["differences"]])

    def test_filtered_record_invisible_to_both_sides_produces_nothing(self):
        self.create("acct-1")
        early = Viewpoint("2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z")
        document = self.vault.compare_views(
            early, early, record_ids=[("account", "acct-1"), ("account", "ghost")]
        )
        self.assertEqual([], document["differences"])

    def test_instant_coordinates_accept_all_public_time_forms(self):
        self.create("acct-1")
        # Integer milliseconds and aware datetimes parse to the same facets as
        # their RFC 3339 spellings, so identical viewpoints compare empty.
        facet_text = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        facet_mixed = Viewpoint(
            datetime(2024, 2, 1, tzinfo=timezone.utc),
            1715731200000,
        )
        self.assertEqual([], self.vault.compare_views(facet_text, facet_mixed)["differences"])

    # -- argument validation ------------------------------------------------

    def test_missing_coordinates_raise_value_error(self):
        self.create("acct-1")
        good = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        for left, right, label in (
            (Viewpoint(None, "2024-05-15T00:00:00Z"), good, "as_of"),
            (Viewpoint("2024-02-01T00:00:00Z", None), good, "known_at"),
            (good, Viewpoint(None, "2024-05-15T00:00:00Z"), "as_of"),
            (good, Viewpoint("2024-02-01T00:00:00Z", None), "known_at"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, label):
                    self.vault.compare_views(left, right)

    def test_non_viewpoint_arguments_raise_value_error(self):
        good = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        for bad in (None, ("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z"), object()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.vault.compare_views(bad, good)
                with self.assertRaises(ValueError):
                    self.vault.compare_views(good, bad)

    def test_bad_time_values_raise_value_error(self):
        good = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        for value in ("yesterday", "2024-13-01T00:00:00Z", True, {"at": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.vault.compare_views(Viewpoint(value, "2024-05-15T00:00:00Z"), good)
                with self.assertRaises(ValueError):
                    self.vault.compare_views(good, Viewpoint("2024-02-01T00:00:00Z", value))

    def test_bad_record_filter_raises_type_error(self):
        good = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        for bad in (123, object(), "account", b"account"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    self.vault.compare_views(good, good, record_ids=bad)
        for bad in (
            [("account",)],
            [("account", "acct-1", "extra")],
            [("account", 7)],
            [(5, "acct-1")],
            ["acct-1"],
            [{"type": "account", "id": "acct-1"}],
            [("bad type", "acct-1")],
            [("account", "bad id")],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    self.vault.compare_views(good, good, record_ids=bad)

    def test_invalid_arguments_return_no_partial_results_and_stay_read_only(self):
        self.create("acct-1")
        before = self.vault.history("account", "acct-1")
        good = Viewpoint("2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z")
        with self.assertRaises(ValueError):
            self.vault.compare_views(Viewpoint(None, None), good)
        with self.assertRaises(TypeError):
            self.vault.compare_views(good, good, record_ids=7)
        self.assertEqual(before, self.vault.history("account", "acct-1"))

    # -- read-only / consistency guarantees ---------------------------------

    def test_the_comparison_is_strictly_read_only(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        before = self.vault.history("account", "acct-1")
        self.compare()
        self.compare(record_ids=[("account", "acct-1")])
        self.assertEqual(before, self.vault.history("account", "acct-1"))
        # No new version rows, no clock movement, no audit writes: a plain
        # read after the comparison still answers identically.
        self.assertEqual(
            self.vault.entity_as_of("account", "acct-1", "2024-04-01T00:00:00Z"),
            self.vault.entity_as_of("account", "acct-1", "2024-04-01T00:00:00Z"),
        )

    def test_both_viewpoints_observe_one_committed_state(self):
        self.create("acct-1")
        self.clock.advance(days=30)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original = self.vault._visible_record
        entered_once = []

        def instrumented(entity_type, entity_id, as_of, known_at):
            if not entered_once:
                entered_once.append(True)
                entered.set()
                self.assertTrue(release.wait(5))
            return original(entity_type, entity_id, as_of, known_at)

        self.vault._visible_record = instrumented
        try:
            # The right viewpoint's transaction time is *after* the writer's
            # eventual record instant (2024-05-31), so if its read happened
            # after that commit it would see platinum and report a change.
            left = Viewpoint("2024-04-01T00:00:00Z", "2024-05-15T00:00:00Z")
            right = Viewpoint("2024-04-01T00:00:00Z", "2024-06-15T00:00:00Z")

            def compare():
                finished.document = self.vault.compare_views(left, right)
                finished.set()

            comparer = threading.Thread(target=compare)
            comparer.start()
            self.assertTrue(entered.wait(5))

            write_done = threading.Event()

            def write():
                self.correct(
                    [{"attribute": "tier", "value": "platinum"}],
                    as_of="2024-03-01T00:00:00Z",
                    key="concurrent-fix",
                )
                write_done.set()

            writer = threading.Thread(target=write)
            writer.start()
            # The writer cannot commit while the comparison holds its single
            # read boundary.
            self.assertFalse(write_done.wait(0.3))
            release.set()
            self.assertTrue(finished.wait(5))
            comparer.join(5)
            writer.join(5)
            self.assertTrue(write_done.is_set())
        finally:
            self.vault._visible_record = original

        # Both sides were evaluated against the pre-write committed state, so
        # the right side still read gold even though its known_at is later
        # than the write that committed immediately afterwards.
        self.assertEqual([], finished.document["differences"])


if __name__ == "__main__":
    unittest.main()
