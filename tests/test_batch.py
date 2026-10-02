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
from timevault.service import MAX_BATCH_OPERATIONS, TimeVault

BASE = datetime(2024, 5, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


def create(entity_id="acct-1", **attributes):
    return {
        "operation": "create",
        "type": "account",
        "id": entity_id,
        "attributes": attributes or {"status": "active", "tier": "gold"},
        "valid_from": "2024-01-01T00:00:00Z",
    }


def correct(facts, entity_id="acct-1", as_of=None):
    item = {"operation": "correct", "type": "account", "id": entity_id, "facts": facts}
    if as_of is not None:
        item["as_of"] = as_of
    return item


class BatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    def batch(self, operations, key="batch-1"):
        return self.vault.batch({"operations": operations}, key)

    def key(self):
        self.keys += 1
        return f"batch-{self.keys}"

    def versions_of(self, entity_id="acct-1", attribute="tier"):
        section = [
            item
            for item in self.vault.history("account", entity_id)["attributes"]
            if item["attribute"] == attribute
        ]
        return section[0]["versions"]

    def values(self, entity_id="acct-1", as_of=None, known_at=None):
        document = self.vault.entity_as_of("account", entity_id, as_of, known_at)
        return {name: entry["value"] for name, entry in document["attributes"].items()}

    # -- happy path ----------------------------------------------------------

    def test_batch_creates_then_corrects_the_same_new_entity(self):
        result = self.batch(
            [
                create(),
                correct(
                    [{"attribute": "tier", "value": "platinum"}],
                    as_of="2024-03-01T00:00:00Z",
                ),
            ]
        )
        self.assertEqual("2024-05-01T00:00:00.000Z", result["recorded_at"])
        self.assertEqual(2, len(result["results"]))
        first, second = result["results"]
        self.assertEqual("create", first["operation"])
        self.assertEqual("correct", second["operation"])
        # The create result keeps the single-entity create response shape.
        self.assertEqual("2024-05-01T00:00:00.000Z", first["created_at"])
        self.assertEqual("gold", first["attributes"]["tier"]["value"])
        self.assertEqual(1, first["attributes"]["tier"]["version"])
        # The correct sees the entity the create just inserted and answers with
        # the post-correction state.
        self.assertEqual("platinum", second["attributes"]["tier"]["value"])
        self.assertEqual(2, second["attributes"]["tier"]["version"])
        self.assertEqual("active", second["attributes"]["status"]["value"])

    def test_every_row_shares_one_transaction_instant(self):
        result = self.batch(
            [
                create("a"),
                create("b", tier="bronze"),
                correct(
                    [{"attribute": "tier", "value": "platinum"}],
                    entity_id="a",
                    as_of="2024-03-01T00:00:00Z",
                ),
            ],
            key=self.key(),
        )
        stamp = result["recorded_at"]
        for entity_id in ("a", "b"):
            for section in self.vault.history("account", entity_id)["attributes"]:
                for version in section["versions"]:
                    self.assertEqual(stamp, version["recorded_at"])
        self.assertIsNone(self.versions_of("a")[1]["superseded_at"])
        self.assertEqual(stamp, self.versions_of("a")[0]["superseded_at"])

    def test_several_corrects_append_several_history_segments(self):
        self.batch([create()], key=self.key())
        result = self.batch(
            [
                correct(
                    [{"attribute": "tier", "value": "platinum"}],
                    as_of="2024-02-01T00:00:00Z",
                ),
                correct(
                    [{"attribute": "tier", "value": "silver"}],
                    as_of="2024-03-01T00:00:00Z",
                ),
                correct(
                    [{"attribute": "tier", "value": "bronze"}],
                    as_of="2024-04-01T00:00:00Z",
                ),
            ],
            key=self.key(),
        )
        self.assertEqual(["platinum", "silver", "bronze"], [
            item["attributes"]["tier"]["value"] for item in result["results"]
        ])
        versions = self.versions_of()
        self.assertEqual([1, 2, 3, 4], [v["version"] for v in versions])
        self.assertEqual(["gold", "platinum", "silver", "bronze"], [v["value"] for v in versions])
        # Windows tile the axis without gaps or overlaps.
        self.assertEqual("2024-02-01T00:00:00.000Z", versions[0]["valid_end"])
        self.assertEqual("2024-03-01T00:00:00.000Z", versions[1]["valid_end"])
        self.assertEqual("2024-04-01T00:00:00.000Z", versions[2]["valid_end"])
        self.assertIsNone(versions[3]["valid_end"])
        self.assertEqual("gold", self.values(as_of="2024-01-15T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values(as_of="2024-02-15T00:00:00Z")["tier"])
        self.assertEqual("silver", self.values(as_of="2024-03-15T00:00:00Z")["tier"])
        self.assertEqual("bronze", self.values(as_of="2024-04-15T00:00:00Z")["tier"])

    def test_correct_result_defaults_as_of_to_recorded_at(self):
        result = self.batch([create(), correct([{"attribute": "tier", "value": "x"}])])
        self.assertEqual(result["recorded_at"], result["results"][1]["as_of"])
        self.assertEqual(result["recorded_at"], result["results"][1]["known_at"])

    def test_get_history_and_diff_after_the_batch_are_unchanged_entry_points(self):
        self.clock.advance(days=30)
        self.batch(
            [
                create(),
                correct(
                    [{"attribute": "tier", "value": "platinum"}],
                    as_of="2024-03-01T00:00:00Z",
                ),
            ],
            key=self.key(),
        )
        self.assertEqual("gold", self.values(as_of="2024-02-01T00:00:00Z")["tier"])
        self.assertEqual("platinum", self.values(as_of="2024-04-01T00:00:00Z")["tier"])
        diff = self.vault.diff(
            "account", "acct-1", "2024-02-01T00:00:00Z", "2024-04-01T00:00:00Z"
        )
        self.assertEqual(["tier"], [change["attribute"] for change in diff["changes"]])
        self.assertEqual(2, len(self.versions_of()))

    def test_create_in_batch_may_omit_valid_from(self):
        result = self.batch(
            [
                {
                    "operation": "create",
                    "type": "account",
                    "id": "now",
                    "attributes": {"tier": "gold"},
                }
            ],
            key=self.key(),
        )
        self.assertEqual(result["recorded_at"], result["results"][0]["attributes"]["tier"]["valid_from"])

    # -- atomicity and error semantics --------------------------------------

    def test_a_failing_operation_rolls_back_the_whole_batch(self):
        with self.assertRaises(NotFoundError):
            self.batch(
                [
                    create("a"),
                    correct([{"attribute": "tier", "value": "x"}], entity_id="missing"),
                    create("b"),
                ]
            )
        self.assertNotIn("a", self.existing_ids())
        self.assertNotIn("b", self.existing_ids())

    def existing_ids(self):
        ids = set()
        # Entity rows only exist if the transaction committed; history is the
        # public way to probe each candidate.
        for entity_id in ("a", "b", "missing"):
            try:
                self.vault.history("account", entity_id)
            except NotFoundError:
                continue
            ids.add(entity_id)
        return ids

    def test_error_messages_carry_the_one_based_operation_index(self):
        cases = [
            ([{"operation": "correct", "type": "account", "id": "x", "facts": []}], r"operation 1"),
            ([create(), "not-an-object"], r"operation 2"),
            (
                [
                    create(),
                    {"operation": "correct", "type": "account", "id": "acct-1",
                     "facts": [{"attribute": "tier", "value": "x", "when": 1}]},
                ],
                r"operation 2",
            ),
            (
                [
                    {"operation": "create", "type": "account", "id": "a",
                     "attributes": {"x": 1}},
                    {"operation": "create", "type": "account", "id": "b",
                     "attributes": {"x": 1}},
                    {"operation": "delete", "type": "account", "id": "c",
                     "attributes": {"x": 1}},
                ],
                r"operation 3",
            ),
        ]
        for operations, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValidationError, pattern):
                    self.batch(operations, key=self.key())

    def test_request_shape_validation(self):
        cases = [
            ([], "request body must be a JSON object"),
            ("nope", "request body must be a JSON object"),
            ({"operations": {"a": 1}}, "operations must be an array"),
            ({"operations": "nope"}, "operations must be an array"),
            ({"operations": []}, "at least one"),
            ({"operations": [create()], "extra": 1}, "unknown field"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValidationError, message):
                    self.vault.batch(body, self.key())

    def test_more_than_a_thousand_operations_is_a_validation_error(self):
        operations = [
            {"operation": "create", "type": "account", "id": f"e{i}", "attributes": {"x": 1}}
            for i in range(MAX_BATCH_OPERATIONS + 1)
        ]
        with self.assertRaisesRegex(ValidationError, "at most 1000"):
            self.batch(operations, key=self.key())

    def test_exactly_a_thousand_operations_commit(self):
        operations = [
            {"operation": "create", "type": "account", "id": f"e{i:04d}", "attributes": {"x": i}}
            for i in range(MAX_BATCH_OPERATIONS)
        ]
        result = self.batch(operations, key=self.key())
        self.assertEqual(MAX_BATCH_OPERATIONS, len(result["results"]))

    def test_correcting_a_never_created_entity_is_not_found(self):
        with self.assertRaisesRegex(NotFoundError, r"operation 1: entity account/ghost"):
            self.batch(
                [correct([{"attribute": "tier", "value": "x"}], entity_id="ghost")],
                key=self.key(),
            )

    def test_duplicate_create_is_a_conflict_with_index(self):
        with self.assertRaisesRegex(ConflictError, r"operation 2.*already exists"):
            self.batch([create("dup"), create("dup")], key=self.key())
        # Duplicating an entity an earlier request created is a conflict too.
        self.batch([create("extant")], key=self.key())
        with self.assertRaisesRegex(ConflictError, r"operation 1.*already exists"):
            self.batch([create("extant")], key=self.key())

    def test_correct_before_create_is_a_conflict_not_a_not_found(self):
        with self.assertRaisesRegex(ConflictError, r"operation 1.*before it is created"):
            self.batch(
                [
                    correct([{"attribute": "tier", "value": "x"}], entity_id="late"),
                    create("late"),
                ],
                key=self.key(),
            )
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "late")

    def test_explicit_as_of_must_not_be_later_than_recorded_at(self):
        self.clock.advance(days=-2)
        with self.assertRaisesRegex(ValidationError, r"operation 2.*as_of"):
            self.batch(
                [
                    create(),
                    correct(
                        [{"attribute": "tier", "value": "x"}],
                        as_of="2024-05-10T00:00:00Z",
                    ),
                ],
                key=self.key(),
            )
        # Nothing was written: the create rolled back with the correction.
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "acct-1")

    def test_fact_valid_end_before_valid_from_is_an_indexed_validation_error(self):
        with self.assertRaisesRegex(ValidationError, r"operation 2.*valid_end"):
            self.batch(
                [
                    create(),
                    correct(
                        [
                            {
                                "attribute": "tier",
                                "value": "x",
                                "valid_from": "2024-03-01T00:00:00Z",
                                "valid_end": "2024-02-01T00:00:00Z",
                            }
                        ]
                    ),
                ],
                key=self.key(),
            )

    def test_field_sets_are_operation_specific(self):
        with self.assertRaisesRegex(ValidationError, r"operation 1.*unknown field"):
            self.batch(
                [{"operation": "create", "type": "account", "id": "a",
                  "attributes": {"x": 1}, "facts": []}],
                key=self.key(),
            )
        with self.assertRaisesRegex(ValidationError, r"operation 1.*unknown field"):
            self.batch(
                [{"operation": "correct", "type": "account", "id": "a",
                  "facts": [{"attribute": "x", "value": 1}], "attributes": {"x": 1}}],
                key=self.key(),
            )

    # -- idempotency ---------------------------------------------------------

    def test_batch_requires_an_idempotency_key(self):
        with self.assertRaisesRegex(ValidationError, "Idempotency-Key"):
            self.vault.batch({"operations": [create()]}, None)

    def test_replaying_a_key_returns_the_first_response_and_adds_no_versions(self):
        payload = [
            create(),
            correct([{"attribute": "tier", "value": "platinum"}], as_of="2024-03-01T00:00:00Z"),
        ]
        first = self.batch(payload, key="same")
        self.clock.advance(days=10)
        repeated = self.vault.batch({"operations": payload}, "same")
        self.assertEqual(first, repeated)
        self.assertEqual(2, len(self.versions_of()))
        self.assertEqual("2024-05-01T00:00:00.000Z", repeated["recorded_at"])

    def test_same_key_for_a_different_batch_is_a_conflict_and_writes_nothing(self):
        self.batch([create("a")], key="shared")
        with self.assertRaisesRegex(ConflictError, "another operation"):
            self.batch([create("b")], key="shared")
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "b")

    def test_a_batch_key_cannot_replay_a_single_write_key(self):
        self.vault.create_entity(
            "account", {"id": "a", "attributes": {"x": 1}}, "single"
        )
        with self.assertRaisesRegex(ConflictError, "another operation"):
            self.batch([create("b")], key="single")

    # -- concurrency ---------------------------------------------------------

    def test_concurrent_batches_serialize_into_continuous_non_overlapping_versions(self):
        self.batch([create()], key=self.key())
        errors = []

        def worker(number):
            try:
                self.batch(
                    [
                        correct(
                            [{"attribute": "tier", "value": f"v{number}"}],
                            as_of="2024-02-01T00:00:00Z",
                        )
                    ],
                    key=f"concurrent-{number}",
                )
            except Exception as error:  # pragma: no cover - failure path
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        versions = self.versions_of()
        self.assertEqual(list(range(1, 22)), [v["version"] for v in versions])
        # Every correction lands at the same business instant, so each displaced
        # window is an empty one (valid_end == valid_start) and the open ended
        # final window never overlaps any of them.
        for earlier, later in zip(versions, versions[1:]):
            end = earlier["valid_end_ms"]
            self.assertIsNotNone(end)
            self.assertLessEqual(end, later["valid_from_ms"])

    def test_readers_never_see_a_partial_batch(self):
        stop = threading.Event()
        seen_partial = []

        def writer():
            number = 0
            while not stop.is_set():
                operations = []
                for offset in range(5):
                    entity_id = f"e{number:04d}-{offset}"
                    operations.append(create(entity_id, alpha=1, beta=2))
                try:
                    self.batch(operations, key=f"bulk-{number}")
                except ConflictError:
                    pass
                number += 1

        def reader():
            while not stop.is_set():
                # Probe the most recently named batch; whatever the outcome, an
                # existing entity must carry every attribute its create listed.
                for entity_id in ("e0000-0", "e0001-0", "e0002-0"):
                    try:
                        attributes = self.values(entity_id)
                    except NotFoundError:
                        continue
                    if set(attributes) != {"alpha", "beta"}:
                        seen_partial.append((entity_id, attributes))
                        return

        write_thread = threading.Thread(target=writer)
        read_threads = [threading.Thread(target=reader) for _ in range(4)]
        write_thread.start()
        for thread in read_threads:
            thread.start()
        threading.Event().wait(0.5)
        stop.set()
        write_thread.join(timeout=10)
        for thread in read_threads:
            thread.join(timeout=10)
        self.assertEqual([], seen_partial)


class BatchHttpTests(unittest.TestCase):
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

    def call(self, method: str, path: str, body=None, key: str | None = "batch-1"):
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

    def payload(self):
        return {
            "operations": [
                {
                    "operation": "create",
                    "type": "account",
                    "id": "acct-1",
                    "attributes": {"tier": "gold"},
                    "valid_from": "2024-01-01T00:00:00Z",
                },
                {
                    "operation": "correct",
                    "type": "account",
                    "id": "acct-1",
                    "as_of": "2024-03-01T00:00:00Z",
                    "facts": [{"attribute": "tier", "value": "platinum"}],
                },
            ]
        }

    def test_batch_over_http(self):
        status, body = self.call("POST", "/batch", self.payload())
        self.assertEqual(200, status)
        self.assertEqual(2, len(body["results"]))
        self.assertEqual("create", body["results"][0]["operation"])
        self.assertEqual("correct", body["results"][1]["operation"])
        self.assertEqual("platinum", body["results"][1]["attributes"]["tier"]["value"])

        status, document = self.call(
            "GET", "/entities/account/acct-1?as_of=2024-02-01T00:00:00Z", key=None
        )
        self.assertEqual(200, status)
        self.assertEqual("gold", document["attributes"]["tier"]["value"])

    def test_batch_without_key_is_validation_error(self):
        status, body = self.call("POST", "/batch", self.payload(), key=None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_batch_rejects_get_and_query_parameters(self):
        status, _ = self.call("GET", "/batch", key=None)
        self.assertEqual(404, status)
        status, body = self.call("POST", "/batch?nope=1", self.payload())
        self.assertEqual(400, status)
        self.assertRegex(body["error"]["message"], "unknown query parameter")

    def test_failed_batch_reports_the_operation_index_in_the_envelope(self):
        payload = {"operations": [self.payload()["operations"][0],
                                  {"operation": "correct", "type": "account", "id": "ghost",
                                   "facts": [{"attribute": "tier", "value": "x"}]}]}
        status, body = self.call("POST", "/batch", payload, key="batch-x")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])
        self.assertRegex(body["error"]["message"], r"operation 2")

    def test_http_replay_returns_the_same_response(self):
        status, first = self.call("POST", "/batch", self.payload(), key="replay")
        self.assertEqual(200, status)
        self.clock.advance(days=5)
        status, second = self.call("POST", "/batch", self.payload(), key="replay")
        self.assertEqual(200, status)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
