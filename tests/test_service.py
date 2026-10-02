import json
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
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class TimeVaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    # -- helpers ------------------------------------------------------------

    def account(self, entity_id: str = "acct-1", key: str = "create-1", **attributes):
        payload = {
            "id": entity_id,
            "attributes": attributes or {"status": "active", "tier": "gold"},
            "valid_from": "2024-01-01T00:00:00Z",
        }
        return self.vault.create_entity("account", payload, key)

    def correct(self, facts, as_of=None, entity_id: str = "acct-1", key: str | None = None):
        if key is None:
            self.keys += 1
            key = f"fix-{self.keys}"
        body = {"facts": facts}
        if as_of is not None:
            body["as_of"] = as_of
        return self.vault.apply_correction("account", entity_id, body, key)

    def values(self, as_of=None, known_at=None) -> dict:
        document = self.vault.entity_as_of("account", "acct-1", as_of, known_at)
        return {name: entry["value"] for name, entry in document["attributes"].items()}

    def tier_versions(self) -> list:
        document = self.vault.history("account", "acct-1")
        section = [item for item in document["attributes"] if item["attribute"] == "tier"]
        return section[0]["versions"]

    # -- creation -----------------------------------------------------------

    def test_health_and_creation_defaults_valid_from_to_the_record_time(self):
        self.clock.advance(days=3)
        document = self.vault.create_entity(
            "account", {"id": "acct-1", "attributes": {"tier": "gold"}}, "create-1"
        )
        self.assertEqual("2024-05-04T00:00:00.000Z", document["created_at"])
        self.assertEqual("2024-05-04T00:00:00.000Z", document["as_of"])
        entry = document["attributes"]["tier"]
        self.assertEqual(1, entry["version"])
        self.assertIsNone(entry["valid_end"])
        self.assertEqual("2024-05-04T00:00:00.000Z", entry["valid_from"])

    def test_plain_read_projects_on_both_axes_at_the_current_instant(self):
        self.account()
        document = self.vault.entity_as_of("account", "acct-1")
        self.assertEqual("2024-05-01T00:00:00.000Z", document["as_of"])
        self.assertEqual("2024-05-01T00:00:00.000Z", document["known_at"])
        self.assertEqual({"status": "active", "tier": "gold"}, self.values())

    def test_entity_is_unknown_before_its_valid_from(self):
        self.account()
        self.clock.advance(days=30)
        self.assertEqual({"status": "active", "tier": "gold"}, self.values("2024-04-01T00:00:00Z"))
        with self.assertRaises(NotFoundError):
            self.vault.entity_as_of("account", "acct-1", "2023-12-31T00:00:00Z")

    def test_duplicate_entity_is_a_conflict(self):
        self.account()
        with self.assertRaises(ConflictError):
            self.account(key="create-2")

    def test_missing_entity_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.vault.entity_as_of("account", "nope")
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "nope")

    def test_creation_is_idempotent(self):
        first = self.account()
        repeated = self.account(key="create-1")
        self.assertEqual(first, repeated)
        self.assertEqual(1, len(self.tier_versions()))

    def test_create_requires_an_idempotency_key(self):
        with self.assertRaisesRegex(ValidationError, "Idempotency-Key"):
            self.vault.create_entity("account", {"id": "a", "attributes": {"x": 1}}, None)

    def test_idempotency_key_cannot_be_reused_for_another_operation(self):
        self.account()
        with self.assertRaisesRegex(ConflictError, "another operation"):
            self.correct(
                [{"attribute": "tier", "value": "silver"}],
                as_of="2024-03-01T00:00:00Z",
                key="create-1",
            )

    # -- projection semantics ----------------------------------------------

    def test_as_of_is_half_open_on_the_valid_axis(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.assertEqual(
            {"status": "active", "tier": "platinum"},
            self.values("2024-03-01T00:00:00Z"),
        )
        self.assertEqual({"status": "active", "tier": "gold"}, self.values("2024-02-29T23:59:59Z"))

    def test_known_at_is_half_open_on_the_transaction_axis(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        # One millisecond before the correction is recorded the reader still sees
        # only the old value.  The correction is what closes the old window, so at
        # that instant the window is still open ended and the old value is still
        # in effect at any later business instant too.
        self.assertEqual("gold", self.values("2024-02-15T00:00:00Z", 1717113599999)["tier"])
        self.assertEqual("gold", self.values("2024-04-15T00:00:00Z", 1717113599999)["tier"])
        self.assertEqual("platinum", self.values("2024-04-15T00:00:00Z")["tier"])
        # A window is half-open, so the instant a correction lands belongs to the
        # replacement and the displaced value is not in effect exactly there.
        self.assertEqual("gold", self.values("2024-02-29T23:59:59Z")["tier"])
        self.assertEqual("platinum", self.values("2024-03-01T00:00:00Z")["tier"])

    def test_querying_before_the_entity_was_known_is_not_found(self):
        self.account()
        with self.assertRaisesRegex(NotFoundError, "not recorded yet"):
            self.vault.entity_as_of("account", "acct-1", "2024-04-01T00:00:00Z", "2024-04-01T00:00:00Z")

    def test_a_truncation_recorded_after_known_at_leaves_the_window_open(self):
        """A reader between the two writes cannot know the correction happened.

        The truncation of the gold window is knowledge that only the correction
        carries.  A ``known_at`` that is strictly between the creation and the
        correction must therefore read the old belief with an open ended window,
        not the truncated one and not the replacement.
        """
        self.account()
        # The reader's instant sits strictly between creation and correction.
        self.clock.advance(days=15)
        between = "2024-05-16T00:00:00Z"
        self.clock.advance(days=15)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )

        # One millisecond before the correction is recorded, and an instant in
        # the middle of the gap: both see gold with an open window.
        for known_at in (between, "2024-05-30T23:59:59.999Z"):
            with self.subTest(known_at=known_at):
                document = self.vault.entity_as_of(
                    "account", "acct-1", "2024-04-01T00:00:00Z", known_at
                )
                entry = document["attributes"]["tier"]
                self.assertEqual("gold", entry["value"])
                self.assertEqual(1, entry["version"])
                self.assertIsNone(entry["valid_end"])

        # The correction's own instant closes the window and switches the value.
        document = self.vault.entity_as_of(
            "account", "acct-1", "2024-04-01T00:00:00Z", "2024-05-31T00:00:00Z"
        )
        entry = document["attributes"]["tier"]
        self.assertEqual("platinum", entry["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", entry["valid_from"])

    def test_the_supersede_interval_is_a_transaction_time_interval(self):
        """``superseded_at`` is when the truncation was recorded, not when it bites."""
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        gold = self.tier_versions()[0]
        self.assertEqual("2024-05-01T00:00:00.000Z", gold["recorded_at"])
        # Business side: the valid window really ends where the correction lands.
        self.assertEqual("2024-03-01T00:00:00.000Z", gold["valid_end"])
        # Transaction side: it stopped being current when the correction was
        # recorded, so [recorded_at, superseded_at) is a self-consistent interval.
        self.assertEqual("2024-05-31T00:00:00.000Z", gold["superseded_at"])
        self.assertLess(gold["recorded_at"], gold["superseded_at"])

        # The same holds when a second, backdated correction trims the same
        # window again: the business end follows the correction's landing point,
        # the supersede stamp follows the write that closed the window.
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}],
            key="fix-2",
        )
        gold, silver = self.tier_versions()[0], self.tier_versions()[1]
        self.assertEqual("2024-02-01T00:00:00.000Z", gold["valid_end"])
        self.assertEqual("2024-06-30T00:00:00.000Z", gold["superseded_at"])
        self.assertEqual("2024-06-30T00:00:00.000Z", silver["recorded_at"])

        # A reader between the two trims sees the window as the first trim left
        # it, never the end the second one pulled it back to: at 2024-06-01 the
        # platinum window still runs to 2024-03-01, and from 2024-06-30 the
        # backdated silver takes over from 2024-02-01 instead.
        document = self.vault.entity_as_of(
            "account", "acct-1", "2024-04-15T00:00:00Z", "2024-06-01T00:00:00Z"
        )
        self.assertEqual("platinum", document["attributes"]["tier"]["value"])
        document = self.vault.entity_as_of(
            "account", "acct-1", "2024-03-15T00:00:00Z", "2024-06-01T00:00:00Z"
        )
        self.assertEqual("platinum", document["attributes"]["tier"]["value"])
        document = self.vault.entity_as_of(
            "account", "acct-1", "2024-02-15T00:00:00Z", "2024-06-30T00:00:00Z"
        )
        self.assertEqual("silver", document["attributes"]["tier"]["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", document["attributes"]["tier"]["valid_end"])

    def test_the_ledger_keeps_its_four_invariants_across_a_full_history(self):
        """Append-only, deterministic, lossless, and non-overlapping windows."""
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}],
            key="fix-2",
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "bronze", "valid_from": "2024-01-15T00:00:00Z"}],
            key="fix-3",
        )
        self.clock.advance(days=30)
        self.correct([{"attribute": "status", "deleted": True}], as_of="2024-02-01T00:00:00Z", key="fix-4")

        versions = self.tier_versions()
        # Append-only and lossless: every value ever written is still there.
        self.assertEqual(
            ["bronze", "gold", "platinum", "silver"],
            sorted(version["value"] for version in versions),
        )
        self.assertEqual([1, 2, 3, 4], sorted(version["version"] for version in versions))

        # Transaction intervals are self-consistent, and every version keeps a
        # non-empty window: a stored end never precedes its own start.
        for version in versions:
            if version["superseded_at"] is not None:
                self.assertLessEqual(version["recorded_at"], version["superseded_at"])
            if version["valid_end_ms"] is not None:
                self.assertGreaterEqual(version["valid_end_ms"], version["valid_from_ms"])

        # Valid windows of one attribute never overlap: walking business time
        # forward, every instant belongs to at most one version.
        windows = sorted(
            (version["valid_from_ms"], version["valid_end_ms"]) for version in versions
        )
        for (_, earlier_end), (later_start, _) in zip(windows, windows[1:]):
            if earlier_end is not None:
                self.assertLessEqual(earlier_end, later_start)

        # The same pair of axes always yields the same projection, and the
        # retraction is stored as an intentionally empty window.
        for as_of in ("2024-01-20T00:00:00Z", "2024-02-15T00:00:00Z", "2024-05-15T00:00:00Z"):
            for known_at in (None, "2024-06-15T00:00:00Z", "2024-07-15T00:00:00Z"):
                with self.subTest(as_of=as_of, known_at=known_at):
                    first = self.vault.entity_as_of("account", "acct-1", as_of, known_at)
                    second = self.vault.entity_as_of("account", "acct-1", as_of, known_at)
                    self.assertEqual(first, second)
        status = [
            item
            for item in self.vault.history("account", "acct-1")["attributes"]
            if item["attribute"] == "status"
        ][0]["versions"]
        self.assertEqual("assert", status[0]["operation"])
        self.assertEqual("retract", status[1]["operation"])
        self.assertEqual(status[1]["valid_from_ms"], status[1]["valid_end_ms"])

    def test_epoch_milliseconds_are_accepted_alongside_rfc3339(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        self.assertEqual("gold", self.values("2024-04-01T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values(1717372800000)["tier"])

    # -- correction semantics ----------------------------------------------

    def test_correction_truncates_the_old_window_without_losing_the_value(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        versions = self.tier_versions()
        self.assertEqual(2, len(versions))
        self.assertEqual("gold", versions[0]["value"])
        self.assertEqual("2024-01-01T00:00:00.000Z", versions[0]["valid_from"])
        self.assertEqual("2024-03-01T00:00:00.000Z", versions[0]["valid_end"])
        self.assertEqual("platinum", versions[1]["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", versions[1]["valid_from"])
        self.assertIsNone(versions[1]["superseded_at"])
        self.assertEqual("active", self.values("2024-02-01T00:00:00Z")["status"])

    def test_correction_keeps_the_original_value_readable_in_history(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.assertEqual("gold", self.values("2024-01-15T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values("2024-04-15T00:00:00Z")["tier"])

    def test_backdated_correction_splits_the_window_and_wins_from_then_on(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.assertEqual("platinum", self.values("2024-04-15T00:00:00Z")["tier"])
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}],
            key="fix-2",
        )
        # The backdated value is placed at 2024-02-01 and wins from there, so the
        # window gold held is cut at 2024-02-01 and silver takes it over until
        # the version it displaced begins again at 2024-03-01.
        self.assertEqual("gold", self.values("2024-01-15T00:00:00Z")["tier"])
        self.assertEqual("silver", self.values("2024-02-15T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values("2024-05-15T00:00:00Z")["tier"])
        values = [version["value"] for version in self.tier_versions()]
        self.assertEqual(["gold", "silver", "platinum"], values)

    def test_superseded_versions_keep_their_values_but_no_valid_time(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "silver"}], as_of="2024-02-01T00:00:00Z", key="fix-2"
        )
        versions = self.tier_versions()
        self.assertEqual(3, len(versions))
        self.assertEqual(["gold", "silver", "platinum"], [item["value"] for item in versions])
        middle = versions[2]
        self.assertEqual("platinum", middle["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", middle["valid_from"])
        # Silver lands at 2024-02-01 and wins from there up to the version it
        # displaced, which starts again at 2024-03-01: what it displaced is no
        # longer selectable inside its own old window.
        self.assertEqual("silver", self.values("2024-02-15T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values("2024-03-15T00:00:00Z")["tier"])

    def test_history_with_known_at_hides_later_corrections(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        document = self.vault.history("account", "acct-1", "2024-05-15T00:00:00Z")
        versions = [
            item for item in document["attributes"] if item["attribute"] == "tier"
        ][0]["versions"]
        self.assertEqual(1, len(versions))
        self.assertEqual("gold", versions[0]["value"])
        self.assertEqual("2024-01-01T00:00:00.000Z", versions[0]["valid_from"])

    def test_retraction_closes_the_window_and_leaves_the_value_in_history(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "status", "deleted": True}], as_of="2024-03-01T00:00:00Z"
        )
        self.assertEqual("active", self.values("2024-02-15T00:00:00Z")["status"])
        self.assertNotIn("status", self.values("2024-04-15T00:00:00Z"))
        section = [
            item for item in self.vault.history("account", "acct-1")["attributes"]
            if item["attribute"] == "status"
        ][0]
        self.assertEqual("active", section["versions"][0]["value"])
        self.assertEqual("assert", section["versions"][0]["operation"])
        self.assertEqual("2024-03-01T00:00:00.000Z", section["versions"][0]["valid_end"])
        self.assertEqual("retract", section["versions"][1]["operation"])
        self.assertIsNone(section["versions"][1]["value"])

    def test_bounded_assertion_stops_by_itself(self):
        self.clock.advance(days=-95)
        self.account()
        self.clock.advance(days=5)
        self.correct(
            [
                {
                    "attribute": "tier",
                    "value": "trial",
                    "valid_from": "2024-02-01T00:00:00Z",
                    "valid_end": "2024-03-01T00:00:00Z",
                }
            ]
        )
        self.assertEqual("trial", self.values("2024-02-14T00:00:00Z")["tier"])
        self.assertEqual("gold", self.values("2024-01-10T00:00:00Z")["tier"])
        self.assertNotIn("tier", self.values("2024-03-20T00:00:00Z"))

    def test_correction_version_numbers_increase_per_attribute(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "one"}], as_of="2024-03-01T00:00:00Z")
        self.clock.advance(days=30)
        result = self.correct(
            [{"attribute": "tier", "value": "two"}], as_of="2024-04-01T00:00:00Z", key="fix-2"
        )
        self.assertEqual(3, result["attributes"]["tier"]["version"])
        self.assertEqual(1, result["attributes"]["status"]["version"])

    def test_correction_response_reflects_the_state_after_the_write(self):
        self.account()
        self.clock.advance(days=30)
        result = self.correct([{"attribute": "tier", "value": "platinum"}])
        self.assertEqual("platinum", result["attributes"]["tier"]["value"])
        self.assertEqual("active", result["attributes"]["status"]["value"])

    def test_correction_is_idempotent(self):
        self.account()
        self.clock.advance(days=30)
        first = self.correct(
            [{"attribute": "tier", "value": "platinum"}],
            as_of="2024-03-01T00:00:00Z",
            key="fix-same",
        )
        repeated = self.correct(
            [{"attribute": "tier", "value": "platinum"}],
            as_of="2024-03-01T00:00:00Z",
            key="fix-same",
        )
        self.assertEqual(first, repeated)
        self.assertEqual(2, len(self.tier_versions()))

    def test_correction_on_an_unknown_entity_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.correct([{"attribute": "tier", "value": "x"}], entity_id="missing")

    def test_fact_cannot_predate_the_entity(self):
        """A correction may not claim that the entity existed before it was recorded."""
        self.account()
        self.clock.advance(days=-1)
        with self.assertRaisesRegex(ConflictError, "before it existed"):
            self.correct([{"attribute": "tier", "value": "x"}])

    def test_a_backdated_fact_may_restate_business_time_before_the_record_time(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum", "valid_from": "2024-02-01T00:00:00Z"}]
        )
        self.assertEqual("gold", self.values("2024-01-15T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values("2024-04-15T00:00:00Z")["tier"])

    # -- diff ---------------------------------------------------------------

    def test_diff_reports_added_removed_and_changed(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "region", "value": "emea"},
                {"attribute": "status", "deleted": True},
            ],
            as_of="2024-03-01T00:00:00Z",
        )
        document = self.vault.diff(
            "account", "acct-1", "2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z"
        )
        changes = {change["attribute"]: change for change in document["changes"]}
        self.assertEqual({"region", "status", "tier"}, set(changes))
        self.assertEqual("added", changes["region"]["change"])
        self.assertIsNone(changes["region"]["before"])
        self.assertEqual("emea", changes["region"]["after"]["value"])
        self.assertEqual("removed", changes["status"]["change"])
        self.assertEqual("active", changes["status"]["before"]["value"])
        self.assertIsNone(changes["status"]["after"])
        self.assertEqual("changed", changes["tier"]["change"])
        self.assertEqual("gold", changes["tier"]["before"]["value"])
        self.assertEqual("platinum", changes["tier"]["after"]["value"])

    def test_diff_is_empty_when_nothing_changed(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        document = self.vault.diff(
            "account", "acct-1", "2024-03-05T00:00:00Z", "2024-04-05T00:00:00Z"
        )
        self.assertEqual([], document["changes"])

    def test_diff_uses_the_knowledge_of_the_requested_transaction_time(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        # At 2024-05-15 the correction is not recorded yet, so at that instant the
        # window it truncated still ran to its own end: the tier did not change.
        document = self.vault.diff(
            "account",
            "acct-1",
            "2024-02-01T00:00:00Z",
            "2024-02-20T00:00:00Z",
            known_at="2024-05-15T00:00:00Z",
        )
        self.assertEqual([], document["changes"])
        # With the correction known, the same interval reports the change.
        document = self.vault.diff(
            "account", "acct-1", "2024-02-01T00:00:00Z", "2024-05-15T00:00:00Z"
        )
        self.assertEqual(["tier"], [change["attribute"] for change in document["changes"]])
        self.assertEqual("changed", document["changes"][0]["change"])

    def test_diff_can_be_limited_to_named_attributes(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "region", "value": "emea"},
            ],
            as_of="2024-03-01T00:00:00Z",
        )
        document = self.vault.diff(
            "account",
            "acct-1",
            "2024-02-01T00:00:00Z",
            "2024-04-01T00:00:00Z",
            names=["tier"],
        )
        self.assertEqual(["tier"], [change["attribute"] for change in document["changes"]])
        with self.assertRaisesRegex(ValidationError, "unknown attribute"):
            self.vault.diff(
                "account",
                "acct-1",
                "2024-02-01T00:00:00Z",
                "2024-04-01T00:00:00Z",
                names=["nope"],
            )

    def test_diff_rejects_an_empty_or_reversed_interval(self):
        self.account()
        with self.assertRaisesRegex(ValidationError, "strictly later"):
            self.vault.diff(
                "account", "acct-1", "2024-02-01T00:00:00Z", "2024-02-01T00:00:00Z"
            )

    # -- validation ---------------------------------------------------------

    def test_request_shape_validation(self):
        cases = [
            ({"id": "a"}, "attributes is required"),
            ({"attributes": {"x": 1}}, "id is required"),
            ({"id": "bad id", "attributes": {"x": 1}}, "entity id"),
            ({"id": "a", "attributes": {}}, "non-empty object"),
            ({"id": "a", "attributes": {"x": 1}, "extra": 2}, "unknown field"),
            ({"id": "a", "attributes": {"x": {"nested": 1}}}, "objects and arrays"),
            ({"id": "a", "attributes": {"x": 1}, "valid_from": "2025-01-01T00:00:00Z"}, "future"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValidationError, message):
                    self.vault.create_entity("account", body, "k")

    def test_entity_type_and_id_shape(self):
        for entity_type in ["", "bad type", "x" * 101]:
            with self.subTest(entity_type=entity_type):
                with self.assertRaises(ValidationError):
                    self.vault.create_entity(
                        entity_type, {"id": "a", "attributes": {"x": 1}}, "k"
                    )
        with self.assertRaises(ValidationError):
            self.account(entity_id="has space")

    def test_fact_shape_validation(self):
        self.account()
        cases = [
            ([{"attribute": "tier"}], "requires a value"),
            ([{"attribute": "tier", "value": "x", "deleted": True}], "must not carry a value"),
            ([{"attribute": "tier", "value": "x", "deleted": "yes"}], "must be a boolean"),
            ([{"attribute": "tier", "value": "x", "valid_end": "2024-01-01T00:00:00Z"}], "later than the window start"),
            ([{"attribute": "tier", "value": "x", "when": 1}], "unknown field"),
            ([], "non-empty array"),
            (
                [
                    {"attribute": "tier", "value": "x"},
                    {"attribute": "tier", "value": "y"},
                ],
                "same attribute twice",
            ),
        ]
        for facts, message in cases:
            with self.subTest(facts=facts):
                with self.assertRaisesRegex(ValidationError, message):
                    self.correct(facts)

    def test_time_parsing_is_strict(self):
        self.account()
        for value in ["yesterday", "2024-13-01T00:00:00Z", "2024-01-01", True, {"at": 1}]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.vault.entity_as_of("account", "acct-1", value)
        with self.assertRaisesRegex(ValidationError, "future"):
            self.correct([{"attribute": "tier", "value": "x"}], as_of="2030-01-01T00:00:00Z")

    def test_correction_body_validation(self):
        self.account()
        with self.assertRaisesRegex(ValidationError, "aliases"):
            self.vault.apply_correction(
                "account",
                "acct-1",
                {
                    "as_of": "2024-02-01T00:00:00Z",
                    "valid_from": "2024-02-01T00:00:00Z",
                    "facts": [{"attribute": "tier", "value": "x"}],
                },
                "k1",
            )
        with self.assertRaisesRegex(ValidationError, "unknown field"):
            self.vault.apply_correction(
                "account",
                "acct-1",
                {"facts": [{"attribute": "tier", "value": "x"}], "when": 1},
                "k2",
            )

    def test_values_must_be_scalars(self):
        with self.assertRaisesRegex(ValidationError, "objects and arrays"):
            self.vault.create_entity(
                "account", {"id": "acct-1", "attributes": {"tier": {"nested": 1}}}, "k1"
            )
        with self.assertRaisesRegex(ValidationError, "objects and arrays"):
            self.vault.create_entity(
                "account", {"id": "acct-1", "attributes": {"tier": [1, 2]}}, "k2"
            )


class HttpTests(unittest.TestCase):
    """Exercise the real socket server: routing, verbs, status codes."""

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

    def test_health(self):
        self.assertEqual((200, {"status": "ok"}), self.call("GET", "/health"))

    def test_create_and_project_over_http(self):
        status, created = self.call(
            "POST",
            "/entities/account",
            {"id": "acct-1", "attributes": {"tier": "gold"}, "valid_from": "2024-01-01T00:00:00Z"},
            "create-1",
        )
        self.assertEqual(201, status)
        self.assertEqual("gold", created["attributes"]["tier"]["value"])

        self.clock.advance(days=30)
        status, _ = self.call(
            "PUT",
            "/entities/account/acct-1",
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )
        self.assertEqual(200, status)

        status, document = self.call(
            "GET", "/entities/account/acct-1?as_of=2024-02-01T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual("gold", document["attributes"]["tier"]["value"])

        status, document = self.call(
            "GET", "/entities/account/acct-1?as_of=2024-02-01T00:00:00Z&known_at=2024-05-15T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual("gold", document["attributes"]["tier"]["value"])

        status, history = self.call("GET", "/entities/account/acct-1/history")
        self.assertEqual(200, status)
        self.assertEqual(2, len(history["attributes"][0]["versions"]))

    def test_http_errors_use_the_shared_envelope(self):
        status, body = self.call("GET", "/entities/account/acct-1")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

        status, body = self.call("POST", "/entities/account", {"id": "a", "attributes": {"x": 1}}, None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call("GET", "/nope")
        self.assertEqual(404, status)
        self.assertIn("message", body["error"])

        status, body = self.call("GET", "/entities/account/acct-1?nope=1")
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "unknown query parameter")

    def test_diff_over_http(self):
        self.call(
            "POST",
            "/entities/account",
            {"id": "acct-1", "attributes": {"tier": "gold"}, "valid_from": "2024-01-01T00:00:00Z"},
            "create-1",
        )
        self.clock.advance(days=30)
        self.call(
            "PUT",
            "/entities/account/acct-1",
            {"facts": [{"attribute": "tier", "value": "silver", "valid_from": "2024-02-01T00:00:00Z"}]},
            "fix-1",
        )
        status, document = self.call(
            "GET",
            "/diff?type=account&id=acct-1&from=2024-01-15T00:00:00Z&to=2024-03-15T00:00:00Z&attribute=tier",
        )
        self.assertEqual(200, status)
        self.assertEqual(["tier"], [change["attribute"] for change in document["changes"]])
        self.assertEqual("gold", document["changes"][0]["before"]["value"])
        self.assertEqual("silver", document["changes"][0]["after"]["value"])

        status, body = self.call("GET", "/diff?type=account&id=acct-1&from=2024-01-15T00:00:00Z")
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "from and to")


if __name__ == "__main__":
    unittest.main()
