"""Acceptance tests for the historical view diff (``TimeVault.view_diff``)."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from timevault.errors import ValidationError
from timevault.server import make_handler
from timevault.service import TimeVault

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)

LEFT = {"left_as_of": "2024-04-01T00:00:00Z", "left_known_at": "2024-05-15T00:00:00Z"}
RIGHT = {"right_as_of": "2024-04-01T00:00:00Z", "right_known_at": "2024-06-30T00:00:00Z"}


class Clock:
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class ViewDiffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    # -- helpers ------------------------------------------------------------

    def create(self, entity_id: str, entity_type: str = "account", **attributes):
        self.keys += 1
        if not attributes:
            attributes = {"tier": "gold"}
        return self.vault.create_entity(
            entity_type,
            {"id": entity_id, "attributes": attributes, "valid_from": "2024-01-01T00:00:00Z"},
            f"create-{self.keys}",
        )

    def correct(self, facts, entity_id: str = "acct-1", entity_type: str = "account",
                as_of: str = "2024-03-01T00:00:00Z"):
        self.keys += 1
        return self.vault.apply_correction(
            entity_type, entity_id, {"as_of": as_of, "facts": facts}, f"fix-{self.keys}"
        )

    def view(self, **kwargs):
        merged = {**LEFT, **RIGHT, **kwargs}
        return self.vault.view_diff(**merged)

    # -- classification -----------------------------------------------------

    def test_classification_ordering_and_null_sides(self):
        self.create("acct-2", tier="silver")
        self.create("acct-1", tier="gold", status="active")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}], "acct-1")
        self.correct([{"attribute": "tier", "deleted": True}], "acct-2")
        self.create("acct-3", tier="bronze")

        document = self.view()
        entries = {entry["id"]: entry for entry in document["changes"]}
        self.assertEqual(["acct-1", "acct-2", "acct-3"], [e["id"] for e in document["changes"]])

        changed = entries["acct-1"]
        self.assertEqual("changed", changed["change"])
        self.assertEqual("gold", changed["left"]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", changed["right"]["attributes"]["tier"]["value"])

        removed = entries["acct-2"]
        self.assertEqual("removed", removed["change"])
        self.assertEqual("silver", removed["left"]["attributes"]["tier"]["value"])
        self.assertIsNone(removed["right"])

        added = entries["acct-3"]
        self.assertEqual("added", added["change"])
        self.assertIsNone(added["left"])
        self.assertEqual("bronze", added["right"]["attributes"]["tier"]["value"])

        for entry in document["changes"]:
            self.assertEqual("account", entry["type"])

        self.assertEqual("2024-04-01T00:00:00.000Z", document["left"]["as_of"])
        self.assertEqual("2024-05-15T00:00:00.000Z", document["left"]["known_at"])
        self.assertEqual("2024-04-01T00:00:00.000Z", document["right"]["as_of"])
        self.assertEqual("2024-06-30T00:00:00.000Z", document["right"]["known_at"])
        self.assertEqual(document, self.view(), "repeated call must be deterministic")

    def test_identical_views_and_empty_store_yield_no_changes(self):
        same = {"left_as_of": "2024-02-01T00:00:00Z", "left_known_at": "2024-05-01T00:00:00Z",
                "right_as_of": "2024-02-01T00:00:00Z", "right_known_at": "2024-05-01T00:00:00Z"}
        self.assertEqual([], self.vault.view_diff(**same)["changes"])
        self.create("acct-1")
        self.assertEqual([], self.vault.view_diff(**same)["changes"])

    def test_later_correction_does_not_leak_into_the_earlier_view(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        document = self.view(ids=["acct-1"])
        (entry,) = document["changes"]
        self.assertEqual("changed", entry["change"])
        # The trim the later correction recorded is invisible at the left
        # view's known_at, so the window still reads open ended there.
        self.assertIsNone(entry["left"]["attributes"]["tier"]["valid_end"])
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", entry["right"]["attributes"]["tier"]["value"])

    def test_version_metadata_difference_counts_as_changed(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=31)
        # Restate the same value at the same valid_from: only the version
        # number and the recorded instant move, which is still a change.
        self.correct(
            [{"attribute": "tier", "value": "gold", "valid_from": "2024-01-01T00:00:00Z"}],
            as_of="2024-03-01T00:00:00Z",
        )
        (entry,) = self.view(ids=["acct-1"])["changes"]
        self.assertEqual("changed", entry["change"])
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("gold", entry["right"]["attributes"]["tier"]["value"])
        self.assertEqual(1, entry["left"]["attributes"]["tier"]["version"])
        self.assertEqual(2, entry["right"]["attributes"]["tier"]["version"])

    def test_window_trim_counts_as_changed(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=31)
        self.correct(
            [{
                "attribute": "tier",
                "value": "gold",
                "valid_from": "2024-01-01T00:00:00Z",
                "valid_end": "2024-03-01T00:00:00Z",
            }]
        )
        (entry,) = self.view(
            left_as_of="2024-02-01T00:00:00Z", right_as_of="2024-02-01T00:00:00Z"
        )["changes"]
        self.assertEqual("changed", entry["change"])
        self.assertIsNone(entry["left"]["attributes"]["tier"]["valid_end"])
        self.assertEqual(
            "2024-03-01T00:00:00.000Z", entry["right"]["attributes"]["tier"]["valid_end"]
        )

    def test_retraction_removes_the_record_from_later_views(self):
        self.create("acct-1", tier="gold", status="active")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "deleted": True}])
        (entry,) = self.view(ids=["acct-1"])["changes"]
        self.assertEqual("changed", entry["change"])
        self.assertEqual({"status", "tier"}, set(entry["left"]["attributes"]))
        self.assertEqual({"status"}, set(entry["right"]["attributes"]))
        # Retracting everything makes the whole record disappear on the right.
        self.correct([{"attribute": "status", "deleted": True}], as_of="2024-03-02T00:00:00Z")
        (entry,) = self.view(ids=["acct-1"], right_known_at="2024-07-01T00:00:00Z")["changes"]
        self.assertEqual("removed", entry["change"])
        self.assertIsNone(entry["right"])

    def test_entity_created_after_left_known_at_is_added(self):
        self.create("acct-1")
        self.clock.advance(days=40)
        self.create("acct-2", tier="silver")
        entries = {entry["id"]: entry["change"] for entry in self.view()["changes"]}
        self.assertEqual({"acct-2": "added"}, entries)

    # -- identifier filter ----------------------------------------------------

    def test_filter_selects_dedupes_and_ignores_unknown_ids(self):
        self.create("acct-1")
        self.create("acct-2")
        self.create("acct-3")
        self.clock.advance(days=31)
        for entity_id in ("acct-1", "acct-2", "acct-3"):
            self.correct([{"attribute": "tier", "value": "platinum"}], entity_id)
        document = self.view(ids=["acct-3", "acct-1", "acct-3", "ghost"])
        self.assertEqual(["acct-1", "acct-3"], [e["id"] for e in document["changes"]])
        self.assertEqual({"changed"}, {e["change"] for e in document["changes"]})

    def test_filter_accepts_any_iterable_collection(self):
        self.create("acct-1")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        for ids in (["acct-1"], ("acct-1",), {"acct-1"}, frozenset({"acct-1"}),
                    iter(["acct-1"])):
            with self.subTest(ids=repr(ids)):
                (entry,) = self.view(ids=ids)["changes"]
                self.assertEqual("acct-1", entry["id"])
        self.assertEqual([], self.view(ids=[])["changes"])

    def test_filter_is_never_mutated(self):
        self.create("acct-1")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        ids = ["acct-1", "acct-1", "ghost"]
        self.view(ids=ids)
        self.assertEqual(["acct-1", "acct-1", "ghost"], ids)

    def test_bad_filter_is_a_type_error_before_any_read(self):
        self.create("acct-1")
        for ids in (42, 3.5, "acct-1", b"acct-1", [42], [None], ["bad id"], [""], [{"a": 1}]):
            with self.subTest(ids=repr(ids)):
                with self.assertRaises(TypeError):
                    self.view(ids=ids)
        # A filter mixing valid and invalid elements raises rather than
        # returning a partial result.
        with self.assertRaises(TypeError):
            self.view(ids=["acct-1", "bad id"])

    # -- viewpoint validation -------------------------------------------------

    def test_missing_coordinates_are_value_errors(self):
        full = dict(LEFT, **RIGHT)
        for missing in full:
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    self.vault.view_diff(**{k: v for k, v in full.items() if k != missing})

    def test_invalid_coordinates_are_value_errors(self):
        for kwargs in (
            {"left_as_of": "not-an-instant"},
            {"left_known_at": True},
            {"right_as_of": "2024-13-01T00:00:00Z"},
            {"right_known_at": float("nan")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.view(**kwargs)
        # The raised error is the same validation error every query raises.
        with self.assertRaises(ValidationError):
            self.view(left_as_of="not-an-instant")

    # -- read-only and isolation guarantees -----------------------------------

    def test_the_query_is_read_only(self):
        self.create("acct-1")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        before = json.dumps(self.vault.history("account", "acct-1"), sort_keys=True)
        moment = self.clock.moment
        self.view()
        self.assertEqual(before, json.dumps(self.vault.history("account", "acct-1"), sort_keys=True))
        self.assertEqual(moment, self.clock.moment, "the diff must not move transaction time")

    def test_both_views_share_one_committed_state(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=31)
        self.correct([{"attribute": "tier", "value": "platinum"}])
        # A write that lands after the call started cannot leak into either
        # side: the whole comparison is evaluated against the committed
        # state at call time.  The writer thread blocks on the store lock
        # until the diff has finished reading both views.
        original = self.vault._view_records
        writer = None

        def spy(wanted, as_of, known_at):
            nonlocal writer
            records = original(wanted, as_of, known_at)
            if writer is None:
                writer = threading.Thread(
                    target=lambda: self.correct(
                        [{"attribute": "tier", "value": "diamond"}], as_of="2024-03-15T00:00:00Z"
                    ),
                    daemon=True,
                )
                writer.start()
                time.sleep(0.1)  # let the writer reach the store lock
            return records

        self.vault._view_records = spy
        try:
            document = self.view(ids=["acct-1"])
        finally:
            self.vault._view_records = original
        writer.join(timeout=10)
        (entry,) = document["changes"]
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", entry["right"]["attributes"]["tier"]["value"])
        # The write committed only after the diff released the lock.
        current = self.vault.entity_as_of("account", "acct-1")
        self.assertEqual("diamond", current["attributes"]["tier"]["value"])

    def test_concurrent_writer_cannot_split_the_two_views(self):
        self.create("acct-1", tier="gold")
        self.clock.advance(days=31)
        stop = threading.Event()
        failures = []

        def writer():
            counter = 0
            while not stop.is_set():
                counter += 1
                try:
                    self.correct([{"attribute": "tier", "value": f"v{counter}"}])
                except Exception as error:  # pragma: no cover - defensive
                    failures.append(error)
                    return

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(25):
                document = self.view(ids=["acct-1"])
                for entry in document["changes"]:
                    left_tier = entry["left"]["attributes"]["tier"]["value"]
                    right_tier = entry["right"]["attributes"]["tier"]["value"]
                    # The left view is pinned at a known_at before any
                    # correction, so it must always read the initial value.
                    if left_tier != "gold":
                        failures.append(AssertionError(f"left view saw {left_tier!r}"))
                    if not right_tier.startswith("v") and right_tier != "gold":
                        failures.append(AssertionError(f"right view saw {right_tier!r}"))
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual([], failures)


class ViewDiffHttpTests(unittest.TestCase):
    """The view diff over the real socket server."""

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
            return error.code, json.loads(error.read())

    def test_view_diff_over_http(self):
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
            {"as_of": "2024-03-01T00:00:00Z", "facts": [{"attribute": "tier", "value": "platinum"}]},
            "fix-1",
        )
        status, document = self.call(
            "GET",
            "/view-diff?left_as_of=2024-04-01T00:00:00Z&left_known_at=2024-05-15T00:00:00Z"
            "&right_as_of=2024-04-01T00:00:00Z&right_known_at=2024-06-30T00:00:00Z&id=acct-1",
        )
        self.assertEqual(200, status)
        (entry,) = document["changes"]
        self.assertEqual("changed", entry["change"])
        self.assertEqual("gold", entry["left"]["attributes"]["tier"]["value"])
        self.assertEqual("platinum", entry["right"]["attributes"]["tier"]["value"])

        status, body = self.call("GET", "/view-diff?left_as_of=2024-04-01T00:00:00Z")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call("GET", "/view-diff?nope=1")
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "unknown query parameter")

        status, _ = self.call("POST", "/view-diff")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
