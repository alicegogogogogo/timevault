import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from timevault.errors import NotFoundError, ValidationError
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


class TimelineTests(unittest.TestCase):
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

    def timeline(self, start, end, known_at=None, entity_id: str = "acct-1"):
        return self.vault.timeline("account", entity_id, start, end, known_at)

    # -- segment shape --------------------------------------------------------

    def test_segments_split_at_every_visible_change(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        self.clock.advance(days=30)
        self.correct([{"attribute": "status", "deleted": True}], as_of="2024-04-01T00:00:00Z")

        document = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")
        self.assertEqual("account", document["type"])
        self.assertEqual("acct-1", document["id"])
        self.assertEqual("2024-01-01T00:00:00.000Z", document["from"])
        self.assertEqual("2024-06-01T00:00:00.000Z", document["to"])

        segments = document["segments"]
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z"),
                ("2024-03-01T00:00:00.000Z", "2024-04-01T00:00:00.000Z"),
                ("2024-04-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z"),
            ],
            [(segment["valid_from"], segment["valid_end"]) for segment in segments],
        )
        first, second, third = segments
        self.assertEqual("gold", first["attributes"]["tier"]["value"])
        self.assertEqual("active", first["attributes"]["status"]["value"])
        self.assertEqual("platinum", second["attributes"]["tier"]["value"])
        self.assertEqual("active", second["attributes"]["status"]["value"])
        # The retraction withdrew status, so the last segment holds tier only.
        self.assertEqual(["tier"], sorted(third["attributes"]))

    def test_a_same_value_correction_still_splits_the_interval(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "gold"}], as_of="2024-03-01T00:00:00Z")

        segments = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")["segments"]
        self.assertEqual(2, len(segments))
        self.assertEqual("2024-03-01T00:00:00.000Z", segments[0]["valid_end"])
        self.assertEqual("2024-03-01T00:00:00.000Z", segments[1]["valid_from"])
        # The scalar carried across, but the supplying version changed.
        self.assertEqual("gold", segments[0]["attributes"]["tier"]["value"])
        self.assertEqual("gold", segments[1]["attributes"]["tier"]["value"])
        self.assertEqual(1, segments[0]["attributes"]["tier"]["version"])
        self.assertEqual(2, segments[1]["attributes"]["tier"]["version"])

    def test_simultaneous_changes_share_one_boundary(self):
        self.account()
        self.clock.advance(days=30)
        self.correct(
            [
                {"attribute": "tier", "value": "platinum"},
                {"attribute": "status", "value": "suspended"},
            ],
            as_of="2024-03-01T00:00:00Z",
        )

        segments = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")["segments"]
        self.assertEqual(2, len(segments))
        self.assertEqual("2024-03-01T00:00:00.000Z", segments[0]["valid_end"])
        self.assertEqual("2024-03-01T00:00:00.000Z", segments[1]["valid_from"])
        self.assertEqual("platinum", segments[1]["attributes"]["tier"]["value"])
        self.assertEqual("suspended", segments[1]["attributes"]["status"]["value"])

    def test_segment_bounds_clip_to_the_query_interval(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")

        segments = self.timeline("2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z")["segments"]
        self.assertEqual(
            [
                ("2024-02-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z"),
                ("2024-03-01T00:00:00.000Z", "2024-04-01T00:00:00.000Z"),
            ],
            [(segment["valid_from"], segment["valid_end"]) for segment in segments],
        )
        self.assertEqual("gold", segments[0]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", segments[1]["attributes"]["tier"]["value"])

    def test_a_stretch_with_nothing_in_effect_is_a_gap_not_a_segment(self):
        self.account(status="active")
        self.clock.advance(days=30)
        self.correct(
            [
                {
                    "attribute": "status",
                    "value": "review",
                    "valid_from": "2024-03-01T00:00:00Z",
                    "valid_end": "2024-04-01T00:00:00Z",
                }
            ]
        )

        segments = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")["segments"]
        self.assertEqual(
            [
                ("2024-01-01T00:00:00.000Z", "2024-03-01T00:00:00.000Z"),
                ("2024-03-01T00:00:00.000Z", "2024-04-01T00:00:00.000Z"),
            ],
            [(segment["valid_from"], segment["valid_end"]) for segment in segments],
        )
        self.assertEqual("active", segments[0]["attributes"]["status"]["value"])
        self.assertEqual("review", segments[1]["attributes"]["status"]["value"])

    def test_nothing_in_effect_anywhere_in_the_interval_is_an_empty_list(self):
        self.account()
        document = self.timeline("2023-01-01T00:00:00Z", "2023-06-01T00:00:00Z")
        self.assertEqual([], document["segments"])

    def test_segment_attributes_match_a_point_read_at_the_segment_start(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        self.clock.advance(days=30)
        self.correct([{"attribute": "status", "deleted": True}], as_of="2024-04-01T00:00:00Z")

        document = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")
        for segment in document["segments"]:
            read = self.vault.entity_as_of(
                "account", "acct-1", segment["valid_from"], document["known_at"]
            )
            self.assertEqual(read["attributes"], segment["attributes"])

    def test_segments_are_ordered_and_never_overlap(self):
        self.account()
        for day in (10, 20, 30):
            self.clock.advance(days=5)
            self.correct(
                [{"attribute": "tier", "value": f"tier-{day}"}],
                as_of=f"2024-01-{day:02d}T00:00:00Z",
            )
        segments = self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")["segments"]
        for earlier, later in zip(segments, segments[1:]):
            self.assertLess(earlier["valid_from"], later["valid_from"])
            self.assertLessEqual(earlier["valid_end"], later["valid_from"])

    # -- the transaction axis -------------------------------------------------

    def test_known_at_hides_later_corrections_and_their_trims(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")

        document = self.timeline(
            "2024-01-01T00:00:00Z",
            "2024-06-01T00:00:00Z",
            known_at="2024-05-15T00:00:00Z",
        )
        self.assertEqual("2024-05-15T00:00:00.000Z", document["known_at"])
        segments = document["segments"]
        # The correction is not knowledge yet: one open window, no split, and
        # the trim it carried does not bound the window either.
        self.assertEqual(1, len(segments))
        self.assertEqual("gold", segments[0]["attributes"]["tier"]["value"])
        self.assertIsNone(segments[0]["attributes"]["tier"]["valid_end"])

    def test_the_same_query_over_the_same_data_is_reproducible(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        first = self.timeline(
            "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", known_at="2024-06-01T00:00:00Z"
        )
        second = self.timeline(
            "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", known_at="2024-06-01T00:00:00Z"
        )
        self.assertEqual(first, second)

    # -- validation and absence ------------------------------------------------

    def test_from_must_be_strictly_earlier_than_to(self):
        self.account()
        with self.assertRaises(ValidationError):
            self.timeline("2024-03-01T00:00:00Z", "2024-03-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.timeline("2024-04-01T00:00:00Z", "2024-03-01T00:00:00Z")

    def test_an_unknown_entity_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")

    def test_an_entity_not_recorded_yet_at_known_at_is_not_found(self):
        self.account()
        with self.assertRaises(NotFoundError):
            self.timeline(
                "2024-01-01T00:00:00Z",
                "2024-06-01T00:00:00Z",
                known_at="2024-04-01T00:00:00Z",
            )

    def test_malformed_instants_are_validation_errors(self):
        self.account()
        with self.assertRaises(ValidationError):
            self.timeline("not-a-time", "2024-06-01T00:00:00Z")
        with self.assertRaises(ValidationError):
            self.timeline("2024-01-01T00:00:00Z", "not-a-time")
        with self.assertRaises(ValidationError):
            self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", known_at="nope")

    def test_the_query_is_read_only(self):
        self.account()
        self.clock.advance(days=30)
        self.correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z")
        history_before = self.vault.history("account", "acct-1")
        audit_before = self.vault.audit()
        self.timeline("2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")
        self.assertEqual(history_before, self.vault.history("account", "acct-1"))
        self.assertEqual(audit_before, self.vault.audit())


class TimelineHttpTests(unittest.TestCase):
    """Exercise the timeline route over the real socket server."""

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

    def create_account(self):
        return self.call(
            "POST",
            "/entities/account",
            {
                "id": "acct-1",
                "attributes": {"status": "active", "tier": "gold"},
                "valid_from": "2024-01-01T00:00:00Z",
            },
            "create-1",
        )

    def test_timeline_over_http(self):
        self.create_account()
        self.clock.advance(days=30)
        self.call(
            "PUT",
            "/entities/account/acct-1",
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )
        status, document = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-01-01T00:00:00Z&to=2024-06-01T00:00:00Z",
        )
        self.assertEqual(200, status)
        self.assertEqual("account", document["type"])
        self.assertEqual("acct-1", document["id"])
        self.assertEqual("2024-01-01T00:00:00.000Z", document["from"])
        self.assertEqual("2024-06-01T00:00:00.000Z", document["to"])
        self.assertIn("known_at", document)
        segments = document["segments"]
        self.assertEqual(2, len(segments))
        self.assertEqual("gold", segments[0]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", segments[1]["attributes"]["tier"]["value"])

        # A short-lived state a two-endpoint diff would miss still shows up.
        self.clock.advance(days=30)
        self.call(
            "PUT",
            "/entities/account/acct-1",
            {
                "facts": [
                    {
                        "attribute": "tier",
                        "value": "silver",
                        "valid_from": "2024-03-10T00:00:00Z",
                        "valid_end": "2024-03-20T00:00:00Z",
                    }
                ]
            },
            "fix-2",
        )
        status, document = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-03-01T00:00:00Z&to=2024-04-01T00:00:00Z",
        )
        self.assertEqual(200, status)
        segments = document["segments"]
        self.assertEqual(
            [
                ("2024-03-01T00:00:00.000Z", "2024-03-10T00:00:00.000Z"),
                ("2024-03-10T00:00:00.000Z", "2024-03-20T00:00:00.000Z"),
                ("2024-03-20T00:00:00.000Z", "2024-04-01T00:00:00.000Z"),
            ],
            [(segment["valid_from"], segment["valid_end"]) for segment in segments],
        )
        self.assertEqual("platinum", segments[0]["attributes"]["tier"]["value"])
        self.assertEqual("silver", segments[1]["attributes"]["tier"]["value"])
        # The bounded window closed the open one at its start, so after its
        # end the tier holds nothing at all: the tail segment is a gap for it.
        self.assertNotIn("tier", segments[2]["attributes"])
        self.assertEqual("active", segments[2]["attributes"]["status"]["value"])

    def test_timeline_known_at_over_http(self):
        self.create_account()
        self.clock.advance(days=30)
        self.call(
            "PUT",
            "/entities/account/acct-1",
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )
        status, document = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-01-01T00:00:00Z"
            "&to=2024-06-01T00:00:00Z&known_at=2024-05-15T00:00:00Z",
        )
        self.assertEqual(200, status)
        self.assertEqual("2024-05-15T00:00:00.000Z", document["known_at"])
        self.assertEqual(1, len(document["segments"]))
        self.assertEqual("gold", document["segments"][0]["attributes"]["tier"]["value"])

    def test_timeline_request_validation_over_http(self):
        self.create_account()
        status, body = self.call("GET", "/entities/account/acct-1/timeline?to=2024-06-01T00:00:00Z")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call(
            "GET", "/entities/account/acct-1/timeline?from=2024-01-01T00:00:00Z"
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-06-01T00:00:00Z&to=2024-01-01T00:00:00Z",
        )
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "strictly later")

        status, body = self.call(
            "GET", "/entities/account/acct-1/timeline?from=nope&to=2024-06-01T00:00:00Z"
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-01-01T00:00:00Z&to=2024-06-01T00:00:00Z&nope=1",
        )
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "unknown query parameter")

    def test_timeline_not_found_over_http(self):
        status, body = self.call(
            "GET",
            "/entities/account/ghost/timeline?from=2024-01-01T00:00:00Z&to=2024-06-01T00:00:00Z",
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

        self.create_account()
        status, body = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2024-01-01T00:00:00Z"
            "&to=2024-06-01T00:00:00Z&known_at=2024-04-01T00:00:00Z",
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

    def test_timeline_empty_segments_over_http(self):
        self.create_account()
        status, document = self.call(
            "GET",
            "/entities/account/acct-1/timeline?from=2023-01-01T00:00:00Z&to=2023-06-01T00:00:00Z",
        )
        self.assertEqual(200, status)
        self.assertEqual([], document["segments"])


if __name__ == "__main__":
    unittest.main()
