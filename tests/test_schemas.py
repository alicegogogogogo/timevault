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

BASE = datetime(2024, 7, 1, tzinfo=timezone.utc)


class Clock:
    """Deterministic clock: tests move it explicitly."""

    def __init__(self, start: datetime = BASE):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment = self.moment + timedelta(**delta)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    # -- helpers ------------------------------------------------------------

    def put_schema(self, attributes, effective_from="2024-01-01T00:00:00Z",
                   entity_type="account", key=None, **extra):
        self.keys += 1
        body = {"effective_from": effective_from, "attributes": attributes}
        body.update(extra)
        return self.vault.put_schema(entity_type, body, key or f"schema-{self.keys}")

    def create(self, attributes, entity_id="acct-1", valid_from="2024-01-01T00:00:00Z",
               entity_type="account", key=None):
        self.keys += 1
        return self.vault.create_entity(
            entity_type,
            {"id": entity_id, "attributes": attributes, "valid_from": valid_from},
            key or f"create-{self.keys}",
        )

    def correct(self, facts, entity_id="acct-1", entity_type="account", key=None, **extra):
        self.keys += 1
        body = {"facts": facts}
        body.update(extra)
        return self.vault.apply_correction(entity_type, entity_id, body, key or f"fix-{self.keys}")

    # -- submission ---------------------------------------------------------

    def test_put_returns_the_committed_version(self):
        document = self.put_schema({"tier": "string", "score": "number", "vip": "boolean"})
        self.assertEqual("account", document["type"])
        self.assertEqual(1, document["version"])
        self.assertEqual("2024-01-01T00:00:00.000Z", document["effective_from"])
        self.assertEqual("2024-07-01T00:00:00.000Z", document["recorded_at"])
        self.assertEqual(
            {"tier": "string", "score": "number", "vip": "boolean"}, document["attributes"]
        )

    def test_versions_are_consecutive_and_never_overwritten(self):
        first = self.put_schema({"tier": "string"})
        self.clock.advance(days=1)
        second = self.put_schema({"tier": "string", "region": "string"})
        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"])
        earlier = self.vault.get_schema("account", known_at="2024-07-01T12:00:00Z")
        self.assertEqual(1, earlier["version"])
        self.assertEqual({"tier": "string"}, earlier["attributes"])

    def test_empty_attributes_are_allowed(self):
        document = self.put_schema({})
        self.assertEqual({}, document["attributes"])

    def test_effective_from_accepts_every_instant_form(self):
        document = self.put_schema({"tier": "string"}, effective_from=1704067200000)
        self.assertEqual("2024-01-01T00:00:00.000Z", document["effective_from"])
        document = self.put_schema({"tier": "string"}, effective_from=1704067200.0)
        self.assertEqual("2024-01-01T00:00:00.000Z", document["effective_from"])

    def test_future_effective_from_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.put_schema({"tier": "string"}, effective_from="2024-08-01T00:00:00Z")

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.put_schema({"tier": "string"}, description="nope")

    def test_missing_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.vault.put_schema("account", {"attributes": {}}, "k-1")
        with self.assertRaises(ValidationError):
            self.vault.put_schema("account", {"effective_from": "2024-01-01T00:00:00Z"}, "k-2")

    def test_illegal_attribute_name_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.put_schema({"not a name": "string"})

    def test_illegal_type_name_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.put_schema({"tier": "text"})
        with self.assertRaises(ValidationError):
            self.put_schema({"tier": 7})

    def test_idempotency_key_is_required(self):
        with self.assertRaises(ValidationError):
            self.vault.put_schema(
                "account",
                {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}},
                None,
            )

    def test_replay_returns_the_first_response_and_adds_no_version(self):
        body = {"effective_from": "2024-01-01T00:00:00Z", "attributes": {"tier": "string"}}
        first = self.vault.put_schema("account", body, "schema-key")
        self.clock.advance(days=1)
        replayed = self.vault.put_schema("account", body, "schema-key")
        self.assertEqual(first, replayed)
        self.assertEqual(1, self.vault.get_schema("account")["version"])

    def test_key_reused_for_another_operation_is_a_conflict(self):
        self.put_schema({"tier": "string"}, key="shared-key")
        with self.assertRaises(ConflictError):
            self.vault.create_entity(
                "account",
                {"id": "acct-1", "attributes": {"tier": "gold"}},
                "shared-key",
            )

    # -- bitemporal reads ---------------------------------------------------

    def test_get_defaults_to_the_current_instant(self):
        self.put_schema({"tier": "string"})
        document = self.vault.get_schema("account")
        self.assertEqual(1, document["version"])

    def test_get_without_a_visible_schema_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account")
        self.put_schema({"tier": "string"}, effective_from="2024-03-01T00:00:00Z")
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account", as_of="2024-02-01T00:00:00Z")
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account", known_at="2024-06-01T00:00:00Z")

    def test_as_of_selects_the_governing_version(self):
        self.put_schema({"tier": "string"}, effective_from="2024-01-01T00:00:00Z")
        self.clock.advance(days=1)
        self.put_schema({"tier": "number"}, effective_from="2024-03-01T00:00:00Z")
        before = self.vault.get_schema("account", as_of="2024-02-01T00:00:00Z")
        after = self.vault.get_schema("account", as_of="2024-04-01T00:00:00Z")
        self.assertEqual((1, "string"), (before["version"], before["attributes"]["tier"]))
        self.assertEqual((2, "number"), (after["version"], after["attributes"]["tier"]))

    def test_later_version_at_the_same_start_is_visible_only_once_recorded(self):
        first = self.put_schema({"tier": "string"}, effective_from="2024-01-01T00:00:00Z")
        self.clock.advance(days=10)
        second = self.put_schema({"tier": "number"}, effective_from="2024-01-01T00:00:00Z")
        self.assertEqual(2, second["version"])
        # A reader between the two submissions still sees the first version.
        earlier = self.vault.get_schema("account", known_at=first["recorded_at"])
        self.assertEqual(1, earlier["version"])
        self.assertEqual("string", earlier["attributes"]["tier"])
        # From the second submission's instant onwards it takes over.
        later = self.vault.get_schema("account", known_at=second["recorded_at"])
        self.assertEqual(2, later["version"])
        self.assertEqual("number", later["attributes"]["tier"])

    # -- registration checks ------------------------------------------------

    def test_schema_contradicting_visible_history_is_a_conflict(self):
        self.create({"tier": "gold", "score": 7})
        with self.assertRaises(ConflictError):
            # ``score`` holds a number, not a string.
            self.put_schema({"tier": "string", "score": "string"})
        with self.assertRaises(ConflictError):
            # ``score`` is not declared at all.
            self.put_schema({"tier": "string"})
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account")
        # Declaring every attribute with its exact type commits.
        document = self.put_schema({"tier": "string", "score": "number"})
        self.assertEqual(1, document["version"])

    def test_unknown_attribute_in_history_is_a_conflict(self):
        self.create({"tier": "gold", "nickname": "g"})
        with self.assertRaises(ConflictError):
            self.put_schema({"tier": "string"})

    def test_boolean_is_not_a_number(self):
        self.create({"vip": True})
        with self.assertRaises(ConflictError):
            self.put_schema({"vip": "number"})
        document = self.put_schema({"vip": "boolean"})
        self.assertEqual(1, document["version"])

    def test_history_before_effective_from_is_not_checked(self):
        self.create({"tier": "gold"}, valid_from="2024-01-01T00:00:00Z")
        self.correct([{"attribute": "tier", "value": 42}],
                     **{"as_of": "2024-03-01T00:00:00Z"})
        # The schema starts after the string value's window closed.
        document = self.put_schema({"tier": "number"}, effective_from="2024-03-01T00:00:00Z")
        self.assertEqual(1, document["version"])

    def test_history_after_the_next_schema_start_is_not_checked(self):
        self.create({"tier": "gold"}, valid_from="2024-02-01T00:00:00Z")
        self.correct([{"attribute": "tier", "value": 9}],
                     **{"as_of": "2024-06-01T00:00:00Z"})
        self.put_schema({"tier": "number"}, effective_from="2024-06-01T00:00:00Z")
        # The new version governs only up to the existing 2024-06-01 start,
        # where the string value's window already ended.
        document = self.put_schema({"tier": "string"}, effective_from="2024-01-01T00:00:00Z")
        self.assertEqual(2, document["version"])

    def test_retraction_rows_are_not_judged_by_their_null_value(self):
        self.create({"tier": "gold"})
        self.correct([{"attribute": "tier", "deleted": True}],
                     **{"as_of": "2024-03-01T00:00:00Z"})
        # The retraction's empty placeholder holds null; it is not a type error.
        document = self.put_schema({"tier": "string"})
        self.assertEqual(1, document["version"])

    # -- write enforcement --------------------------------------------------

    def test_create_with_an_undeclared_attribute_is_rejected(self):
        self.put_schema({"tier": "string"})
        with self.assertRaises(ValidationError):
            self.create({"tier": "gold", "nickname": "g"})
        with self.assertRaises(NotFoundError):
            self.vault.entity_as_of("account", "acct-1")

    def test_create_with_a_wrong_value_type_is_rejected(self):
        self.put_schema({"tier": "string", "score": "number"})
        with self.assertRaises(ValidationError):
            self.create({"tier": "gold", "score": "high"})
        with self.assertRaises(ValidationError):
            self.create({"tier": "gold", "score": True})
        document = self.create({"tier": "gold", "score": 7})
        self.assertEqual(7, document["attributes"]["score"]["value"])

    def test_correction_with_a_wrong_value_type_is_rejected(self):
        self.put_schema({"tier": "string"})
        self.create({"tier": "gold"})
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "tier", "value": 42}])
        self.assertEqual("gold", self.vault.entity_as_of("account", "acct-1")
                         ["attributes"]["tier"]["value"])

    def test_correction_adding_an_undeclared_attribute_is_rejected(self):
        self.put_schema({"tier": "string"})
        self.create({"tier": "gold"})
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "nickname", "value": "g"}])

    def test_retraction_is_not_judged_by_its_null_value(self):
        self.put_schema({"tier": "string"})
        self.create({"tier": "gold"})
        document = self.correct([{"attribute": "tier", "deleted": True}])
        self.assertEqual({}, document["attributes"])

    def test_null_values_match_only_the_null_type(self):
        self.put_schema({"tier": "null"})
        with self.assertRaises(ValidationError):
            self.create({"tier": "gold"})
        document = self.create({"tier": None})
        self.assertIsNone(document["attributes"]["tier"]["value"])

    def test_a_fact_window_spanning_schema_intervals_must_satisfy_each(self):
        self.put_schema({"tier": "string", "score": "string"},
                        effective_from="2024-01-01T00:00:00Z")
        self.put_schema({"tier": "string", "score": "number"},
                        effective_from="2024-06-01T00:00:00Z")
        self.create({"tier": "gold"}, valid_from="2024-06-01T00:00:00Z")
        # ``score`` has no versions yet: an open ended string window from
        # before the boundary reaches into the number interval.
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "score", "value": "low",
                           "valid_from": "2024-05-01T00:00:00Z"}])
        # A number window reaching back across the boundary fails the string
        # segment the same way.
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "score", "value": 7,
                           "valid_from": "2024-05-01T00:00:00Z"}])
        # Each segment satisfied on its own commits fine.
        self.correct([{"attribute": "score", "value": "high",
                       "valid_from": "2024-05-01T00:00:00Z",
                       "valid_end": "2024-06-01T00:00:00Z"}])
        self.correct([{"attribute": "score", "value": 7}],
                     **{"as_of": "2024-06-01T00:00:00Z"})

    def test_history_before_the_first_schema_stays_free(self):
        self.put_schema({"tier": "string"}, effective_from="2024-06-01T00:00:00Z")
        self.create({"tier": "gold"}, valid_from="2024-06-01T00:00:00Z")
        # A bounded window entirely before the schema's start is not governed.
        self.correct([{"attribute": "anything", "value": 123,
                       "valid_from": "2024-01-01T00:00:00Z",
                       "valid_end": "2024-06-01T00:00:00Z"}])
        document = self.vault.entity_as_of("account", "acct-1", "2024-03-01T00:00:00Z")
        self.assertEqual(123, document["attributes"]["anything"]["value"])
        # The same fact open ended would cross into the governed interval.
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "other", "value": 1,
                           "valid_from": "2024-01-01T00:00:00Z"}])

    def test_unconfigured_types_keep_free_semantics(self):
        self.put_schema({"tier": "string"})
        document = self.create({"whatever": 123, "extra": True},
                               entity_type="legacy", entity_id="x-1")
        self.assertEqual(123, document["attributes"]["whatever"]["value"])

    # -- batches ------------------------------------------------------------

    def test_batch_items_are_checked_and_roll_back_together(self):
        self.put_schema({"tier": "string"})
        with self.assertRaises(ValidationError) as caught:
            self.vault.run_batch(
                {
                    "operations": [
                        {"operation": "create", "type": "account", "id": "acct-1",
                         "attributes": {"tier": "gold"},
                         "valid_from": "2024-01-01T00:00:00Z"},
                        {"operation": "correct", "type": "account", "id": "acct-1",
                         "facts": [{"attribute": "tier", "value": 42}]},
                    ]
                },
                "batch-1",
            )
        self.assertIn("operation 2", str(caught.exception))
        # The whole batch rolled back: the entity does not exist and no
        # version was consumed.
        with self.assertRaises(NotFoundError):
            self.vault.entity_as_of("account", "acct-1")
        document = self.create({"tier": "gold"})
        self.assertEqual(1, document["attributes"]["tier"]["version"])

    def test_batch_replay_adds_no_versions(self):
        self.put_schema({"tier": "string"})
        body = {
            "operations": [
                {"operation": "create", "type": "account", "id": "acct-1",
                 "attributes": {"tier": "gold"}, "valid_from": "2024-01-01T00:00:00Z"}
            ]
        }
        first = self.vault.run_batch(body, "batch-replay")
        replayed = self.vault.run_batch(body, "batch-replay")
        self.assertEqual(first, replayed)
        history = self.vault.history("account", "acct-1")
        self.assertEqual(1, len(history["attributes"][0]["versions"]))

    # -- reads stay untouched -----------------------------------------------

    def test_schema_registration_does_not_rewrite_entity_reads(self):
        self.create({"tier": "gold"})
        self.put_schema({"tier": "string"})
        document = self.vault.entity_as_of("account", "acct-1")
        self.assertEqual("gold", document["attributes"]["tier"]["value"])
        history = self.vault.history("account", "acct-1")
        self.assertEqual(1, len(history["attributes"][0]["versions"]))


class SchemaHttpTests(unittest.TestCase):
    """Exercise the schema routes over the real socket server."""

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

    def test_put_and_get_over_http(self):
        status, document = self.call(
            "PUT",
            "/schemas/account",
            {"effective_from": "2024-01-01T00:00:00Z",
             "attributes": {"tier": "string", "score": "number"}},
            "schema-1",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, document["version"])

        status, document = self.call("GET", "/schemas/account")
        self.assertEqual(200, status)
        self.assertEqual({"tier": "string", "score": "number"}, document["attributes"])

        status, document = self.call(
            "GET", "/schemas/account?as_of=2023-01-01T00:00:00Z"
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", document["error"]["code"])

    def test_http_validation_errors(self):
        status, body = self.call(
            "PUT", "/schemas/account", {"effective_from": "2024-01-01T00:00:00Z",
                                        "attributes": {}, "extra": 1}, "s-1"
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call("PUT", "/schemas/account",
                                 {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}})
        self.assertEqual(400, status)

        status, body = self.call(
            "GET", "/schemas/account?as_of=2024-01-01T00:00:00Z&as_of=2024-02-01T00:00:00Z"
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

        status, body = self.call("GET", "/schemas/account?bogus=1")
        self.assertEqual(400, status)

        status, body = self.call("POST", "/schemas/account",
                                 {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}},
                                 "s-2")
        self.assertEqual(404, status)

    def test_conflict_over_http(self):
        self.call("POST", "/entities/account",
                  {"id": "acct-1", "attributes": {"tier": "gold"},
                   "valid_from": "2024-01-01T00:00:00Z"}, "c-1")
        status, body = self.call(
            "PUT", "/schemas/account",
            {"effective_from": "2024-01-01T00:00:00Z", "attributes": {"tier": "number"}},
            "s-3",
        )
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
