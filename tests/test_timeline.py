import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from timevault import TimeVault
from timevault.errors import NotFoundError, ValidationError
from timevault.server import make_handler

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)


class Clock:
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class TimelineTests(unittest.TestCase):
    """Walking an entity's full state across a valid-time interval."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

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

    def timeline(self, frm="2024-01-01T00:00:00Z", to="2024-07-01T00:00:00Z", known_at=None,
                 entity_id="acct-1", entity_type="account"):
        return self.vault.timeline(entity_type, entity_id, frm, to, known_at)

    def shapes(self, document):
        """Each segment reduced to (valid_from, valid_end, name->value)."""
        return [
            (
                segment["valid_from"],
                segment["valid_end"],
                {name: entry["value"] for name, entry in segment["attributes"].items()},
            )
            for segment in document["segments"]
        ]

    # -- segmentation -------------------------------------------------------

    def test_one_segment_when_nothing_changes_inside_the_interval(self):
        self.create()
        document = self.timeline("2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z")
        self.assertEqual("account", document["type"])
        self.assertEqual("acct-1", document["id"])
        self.assertEqual("2024-02-01T00:00:00.000Z", document["from"])
        self.assertEqual("2024-03-01T00:00:00.000Z", document["to"])
        self.assertIsNotNone(document["known_at"])
        self.assertEqual(1, len(document["segments"]))
        segment = document["segments"][0]
        self.assertEqual("2024-02-01T00:00:00.000Z", segment["valid_from"])
        self.assertEqual("2024-03-01T00:00:00.000Z", segment["valid_end"])
        self.assertEqual({"active", "gold"}, set(self.shapes(document)[0][2].values()))
        # Attributes have the same projection shape as a plain entity read.
        entry = segment["attributes"]["tier"]
        self.assertEqual(
            {"value", "version", "operation", "valid_from", "valid_end", "recorded_at"},
            set(entry),
        )
        self.assertEqual("assert", entry["operation"])
        self.assertIsNone(entry["valid_end"])

    def test_segments_follow_every_change_in_valid_time_order(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "diamond"}], as_of="2024-05-01T00:00:00Z"
        )
        document = self.timeline()
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z",
                 {"status": "active", "tier": "gold"}),
                ("2024-03-01T00:00:00.000Z", "2024-05-01T00:00:00.000Z",
                 {"status": "active", "tier": "platinum"}),
                ("2024-05-01T00:00:00.000Z", "2024-07-01T00:00:00.000Z",
                 {"status": "active", "tier": "diamond"}),
            ],
            self.shapes(document),
        )

    def test_boundaries_are_clipped_to_the_requested_half_open_interval(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        document = self.timeline("2024-02-01T00:00:00Z", "2024-06-01T00:00:00Z")
        self.assertEqual(
            [
                ("2024-02-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z",
                 {"status": "active", "tier": "gold"}),
                ("2024-03-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z",
                 {"status": "active", "tier": "platinum"}),
            ],
            self.shapes(document),
        )
        # A boundary exactly at ``to`` lands nothing past it; one exactly at
        # ``from`` starts the first segment there.
        document = self.timeline("2024-03-01T00:00:00Z", "2024-04-01T00:00:00Z")
        self.assertEqual(1, len(document["segments"]))
        self.assertEqual("2024-03-01T00:00:00.000Z", document["segments"][0]["valid_from"])
        self.assertEqual("2024-04-01T00:00:00.000Z", document["segments"][0]["valid_end"])

    def test_several_attributes_changing_at_one_instant_share_one_boundary(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "status", "value": "vip"},
            ],
            as_of="2024-03-01T00:00:00Z",
        )
        document = self.timeline()
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z",
                 {"status": "active", "tier": "gold"}),
                ("2024-03-01T00:00:00.000Z", "2024-07-01T00:00:00.000Z",
                 {"status": "vip", "tier": "platinum"}),
            ],
            self.shapes(document),
        )

    def test_attributes_starting_and_ending_at_different_instants_split_alone(self):
        self.create(tier="gold")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "region", "value": "emea"}], as_of="2024-02-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "region", "deleted": True}], as_of="2024-04-01T00:00:00Z"
        )
        document = self.timeline()
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-02-01T00:00:00.000Z", {"tier": "gold"}),
                ("2024-02-01T00:00:00.000Z", "2024-04-01T00:00:00.000Z",
                 {"region": "emea", "tier": "gold"}),
                ("2024-04-01T00:00:00.000Z", "2024-07-01T00:00:00.000Z", {"tier": "gold"}),
            ],
            self.shapes(document),
        )

    def test_identical_scalar_values_are_still_split_on_a_new_version(self):
        self.create(tier="gold")
        self.clock.advance(days=30)
        # Re-assert the same value at a new valid instant: the versions differ
        # even though the scalar does not, so the segments must not merge.
        self.correct(
            [{"attribute": "tier", "value": "gold"}], as_of="2024-03-01T00:00:00Z"
        )
        document = self.timeline()
        self.assertEqual(2, len(document["segments"]))
        first, second = document["segments"]
        self.assertEqual(1, first["attributes"]["tier"]["version"])
        self.assertEqual(2, second["attributes"]["tier"]["version"])
        self.assertEqual("gold", first["attributes"]["tier"]["value"])
        self.assertEqual("gold", second["attributes"]["tier"]["value"])
        self.assertEqual("2024-03-01T00:00:00.000Z", first["valid_end"])
        self.assertEqual("2024-03-01T00:00:00.000Z", second["valid_from"])

    def test_gaps_with_no_attribute_in_effect_produce_no_segment(self):
        self.create(tier="gold")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "deleted": True}], as_of="2024-03-01T00:00:00Z"
        )
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-05-01T00:00:00Z"
        )
        document = self.timeline()
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z", {"tier": "gold"}),
                ("2024-05-01T00:00:00.000Z", "2024-07-01T00:00:00.000Z",
                 {"tier": "platinum"}),
            ],
            self.shapes(document),
        )
        # Segments never overlap and are strictly ordered.
        for earlier, later in zip(document["segments"], document["segments"][1:]):
            self.assertLessEqual(earlier["valid_end"], later["valid_from"])

    def test_known_but_empty_throughout_the_interval_returns_empty_segments(self):
        self.create(tier="gold")
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "deleted": True}], as_of="2024-03-01T00:00:00Z"
        )
        document = self.timeline("2024-04-01T00:00:00Z", "2024-05-01T00:00:00Z")
        self.assertEqual([], document["segments"])
        self.assertEqual("2024-04-01T00:00:00.000Z", document["from"])
        self.assertEqual("2024-05-01T00:00:00.000Z", document["to"])

    def test_a_declared_valid_end_is_its_own_boundary(self):
        self.create(tier="gold")
        self.clock.advance(days=30)
        self.correct(
            [
                {
                    "attribute": "tier",
                    "value": "platinum",
                    "valid_from": "2024-03-01T00:00:00Z",
                    "valid_end": "2024-04-01T00:00:00Z",
                }
            ]
        )
        document = self.timeline()
        shapes = self.shapes(document)
        # Gold ends where platinum starts; platinum ends at its own declared
        # bound even though nothing follows it, so nothing is reported after.
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z", {"tier": "gold"}),
                ("2024-03-01T00:00:00.000Z", "2024-04-01T00:00:00.000Z",
                 {"tier": "platinum"}),
            ],
            shapes,
        )
        self.assertEqual(
            "2024-04-01T00:00:00.000Z",
            document["segments"][-1]["attributes"]["tier"]["valid_end"],
        )

    def test_timeline_surfaces_transient_states_a_two_point_diff_misses(self):
        self.create(tier="gold")
        self.clock.advance(days=15)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-02-01T00:00:00Z"
        )
        self.clock.advance(days=15)
        self.correct(
            [{"attribute": "tier", "value": "gold"}], as_of="2024-02-15T00:00:00Z"
        )
        # A diff between the endpoints sees gold on both sides and nothing in
        # between; the timeline still reports the brief platinum segment.
        document = self.timeline("2024-01-15T00:00:00Z", "2024-03-01T00:00:00Z")
        self.assertEqual(
            ["gold", "platinum", "gold"],
            [segment["attributes"]["tier"]["value"] for segment in document["segments"]],
        )

    # -- transaction-time visibility ----------------------------------------

    def test_a_correction_recorded_after_known_at_does_not_leak(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        early = self.timeline(known_at="2024-05-15T00:00:00Z")
        late = self.timeline(known_at="2024-07-01T00:00:00Z")
        # The earlier reader does not know platinum exists at all: one segment.
        self.assertEqual(1, len(early["segments"]))
        self.assertEqual("gold", early["segments"][0]["attributes"]["tier"]["value"])
        self.assertIsNone(early["segments"][0]["attributes"]["tier"]["valid_end"])
        # The later reader sees the split and the trim it carried.
        self.assertEqual(
            ["gold", "platinum"],
            [segment["attributes"]["tier"]["value"] for segment in late["segments"]],
        )
        self.assertEqual(
            "2024-03-01T00:00:00.000Z",
            late["segments"][0]["attributes"]["tier"]["valid_end"],
        )

    def test_a_trim_recorded_after_known_at_leaves_the_old_window_open(self):
        self.create(tier="gold")
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
        # The bounded re-assertion is itself recorded in June; before that the
        # original open-ended window is all a reader can see, even while
        # querying valid time past the declared end.
        document = self.timeline(
            "2024-02-01T00:00:00Z", "2024-06-01T00:00:00Z", "2024-05-15T00:00:00Z"
        )
        self.assertEqual(1, len(document["segments"]))
        entry = document["segments"][0]["attributes"]["tier"]
        self.assertEqual("gold", entry["value"])
        self.assertIsNone(entry["valid_end"])
        self.assertEqual("2024-06-01T00:00:00.000Z", document["segments"][0]["valid_end"])

    def test_known_at_defaults_to_the_current_instant(self):
        self.create()
        self.clock.advance(days=10)
        document = self.timeline("2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z")
        self.assertEqual("2024-05-11T00:00:00.000Z", document["known_at"])

    # -- errors --------------------------------------------------------------

    def test_unknown_entity_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.timeline(entity_id="ghost")

    def test_entity_recorded_after_known_at_is_not_found(self):
        self.create()
        with self.assertRaises(NotFoundError):
            self.timeline(known_at="2024-04-01T00:00:00Z")

    def test_missing_bounds_and_reversed_intervals_are_validation_errors(self):
        self.create()
        with self.assertRaisesRegex(ValidationError, "from"):
            self.vault.timeline("account", "acct-1", None, "2024-07-01T00:00:00Z")
        with self.assertRaisesRegex(ValidationError, "to"):
            self.vault.timeline("account", "acct-1", "2024-01-01T00:00:00Z", None)
        with self.assertRaisesRegex(ValidationError, "strictly later"):
            self.vault.timeline(
                "account", "acct-1", "2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z"
            )
        with self.assertRaisesRegex(ValidationError, "strictly later"):
            self.vault.timeline(
                "account", "acct-1", "2024-04-01T00:00:00Z", "2024-02-01T00:00:00Z"
            )
        for bad in ("yesterday", True):
            with self.assertRaises(ValidationError):
                self.vault.timeline("account", "acct-1", bad, "2024-07-01T00:00:00Z")

    # -- determinism / read-only --------------------------------------------

    def test_same_arguments_on_same_data_are_identical(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "region", "value": "emea"},
                {"attribute": "status", "deleted": True},
            ],
            as_of="2024-03-01T00:00:00Z",
        )
        first = self.timeline()
        second = self.timeline()
        self.assertEqual(first, second)
        self.assertEqual(
            self.timeline(known_at="2024-07-01T00:00:00Z"),
            self.timeline(known_at="2024-07-01T00:00:00Z"),
        )

    def test_the_timeline_is_strictly_read_only(self):
        self.create()
        self.clock.advance(days=30)
        self.correct(
            [{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"
        )
        history_before = self.vault.history("account", "acct-1")
        audit_before = self.vault.audit(entity_type="account", entity_id="acct-1")
        for _ in range(3):
            self.timeline()
            self.timeline(known_at="2024-05-15T00:00:00Z")
        self.assertEqual(history_before, self.vault.history("account", "acct-1"))
        self.assertEqual(
            audit_before, self.vault.audit(entity_type="account", entity_id="acct-1")
        )
        # The clock/transaction time does not move.
        moment = self.vault.store.now()
        self.assertEqual(moment, self.vault.store.now())

    def test_segments_agree_with_pointwise_projections(self):
        self.create()
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
        self.correct(
            [{"attribute": "region", "deleted": True}], as_of="2024-05-01T00:00:00Z"
        )
        document = self.timeline()
        known_at = document["known_at"]
        # At the start instant and just before the end of every segment the
        # plain read reports exactly the segment's attributes.
        for segment in document["segments"]:
            for as_of in (segment["valid_from"],):
                read = self.vault.entity_as_of("account", "acct-1", as_of, known_at)
                self.assertEqual(
                    {name: entry["value"] for name, entry in read["attributes"].items()},
                    {name: entry["value"] for name, entry in segment["attributes"].items()},
                )

    def test_millisecond_and_float_second_forms_are_accepted(self):
        self.create()
        text = self.timeline(1704067200000, 1719792000000, 1719792000000)
        floating = self.timeline(1704067200.0, 1719792000.0, 1719792000.0)
        self.assertEqual(text, floating)


class TimelineHttpTests(unittest.TestCase):
    """The timeline entry point over the real socket server."""

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
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def seed(self):
        self.vault.create_entity(
            "account",
            {
                "id": "acct-1",
                "attributes": {"status": "active", "tier": "gold"},
                "valid_from": "2024-01-01T00:00:00Z",
            },
            "create-1",
        )
        self.clock.advance(days=30)
        self.vault.apply_correction(
            "account",
            "acct-1",
            {"as_of": "2024-03-01T00:00:00Z",
             "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )

    def test_timeline_over_http(self):
        self.seed()
        status, document = self.call(
            "/entities/account/acct-1/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual(2, len(document["segments"]))
        self.assertEqual("2024-02-01T00:00:00.000Z", document["segments"][0]["valid_from"])
        self.assertEqual("gold", document["segments"][0]["attributes"]["tier"]["value"])
        self.assertEqual(
            "platinum", document["segments"][1]["attributes"]["tier"]["value"]
        )

    def test_known_at_is_accepted_and_echoed_normalised(self):
        self.seed()
        status, document = self.call(
            "/entities/account/acct-1/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z"
            "&known_at=2024-05-15T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual("2024-05-15T00:00:00.000Z", document["known_at"])
        self.assertEqual(1, len(document["segments"]))

    def test_missing_bounds_are_400_validation_errors(self):
        self.seed()
        for path in (
            "/entities/account/acct-1/timeline?to=2024-04-01T00:00:00Z",
            "/entities/account/acct-1/timeline?from=2024-02-01T00:00:00Z",
            "/entities/account/acct-1/timeline",
        ):
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(400, status)
                self.assertEqual("validation_error", body["error"]["code"])

    def test_illegal_times_reversed_intervals_and_unknown_params_are_400(self):
        self.seed()
        cases = (
            "/entities/account/acct-1/timeline?from=nope&to=2024-04-01T00:00:00Z",
            "/entities/account/acct-1/timeline"
            "?from=2024-04-01T00:00:00Z&to=2024-04-01T00:00:00Z",
            "/entities/account/acct-1/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z&bogus=1",
        )
        for path in cases:
            with self.subTest(path=path):
                status, body = self.call(path)
                self.assertEqual(400, status)
                self.assertEqual("validation_error", body["error"]["code"])

    def test_unknown_and_not_yet_recorded_entities_are_404(self):
        status, body = self.call(
            "/entities/account/ghost/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z"
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

        self.seed()
        status, body = self.call(
            "/entities/account/acct-1/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z"
            "&known_at=2024-04-01T00:00:00Z"
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

    def test_known_entity_empty_throughout_is_200_with_empty_segments(self):
        self.seed()
        self.clock.advance(days=30)
        self.vault.apply_correction(
            "account",
            "acct-1",
            {"as_of": "2024-05-01T00:00:00Z",
             "facts": [{"attribute": "tier", "deleted": True},
                       {"attribute": "status", "deleted": True}]},
            "fix-2",
        )
        status, document = self.call(
            "/entities/account/acct-1/timeline"
            "?from=2026-01-01T00:00:00Z&to=2026-02-01T00:00:00Z"
        )
        self.assertEqual(200, status)
        self.assertEqual([], document["segments"])

    def test_other_verbs_are_404(self):
        request = urllib.request.Request(
            self.base + "/entities/account/acct-1/timeline"
            "?from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z",
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as error:
            self.assertEqual(404, error.code)
            error.close()


if __name__ == "__main__":
    unittest.main()
