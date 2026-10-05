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


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.vault = TimeVault(str(Path(self.directory.name) / "vault.db"), self.clock)
        self.keys = 0

    def tearDown(self):
        self.directory.cleanup()

    # -- helpers ------------------------------------------------------------

    def schema(self, attributes, effective_from="2024-01-01T00:00:00Z", entity_type="account", key=None):
        if key is None:
            self.keys += 1
            key = f"schema-{self.keys}"
        return self.vault.put_schema(
            entity_type, {"effective_from": effective_from, "attributes": attributes}, key
        )

    def account(self, entity_id="acct-1", key="create-1", valid_from="2024-01-01T00:00:00Z", **attributes):
        return self.vault.create_entity(
            "account",
            {"id": entity_id, "attributes": attributes or {"tier": "gold"}, "valid_from": valid_from},
            key,
        )

    def correct(self, facts, as_of=None, entity_id="acct-1", key=None):
        if key is None:
            self.keys += 1
            key = f"fix-{self.keys}"
        body = {"facts": facts}
        if as_of is not None:
            body["as_of"] = as_of
        return self.vault.apply_correction("account", entity_id, body, key)

    # -- submission ---------------------------------------------------------

    def test_put_schema_returns_the_committed_version(self):
        document = self.schema({"tier": "string"})
        self.assertEqual(
            {
                "type": "account",
                "version": 1,
                "effective_from": "2024-01-01T00:00:00.000Z",
                "recorded_at": "2024-05-01T00:00:00.000Z",
                "attributes": {"tier": "string"},
            },
            document,
        )

    def test_schema_versions_are_continuous_and_never_overwritten(self):
        first = self.schema({"tier": "string"})
        self.clock.advance(days=1)
        second = self.schema({"tier": "string", "level": "number"}, effective_from="2024-03-01T00:00:00Z")
        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"])
        self.assertEqual(first, self.vault.get_schema("account", "2024-02-01T00:00:00Z"))
        self.assertEqual(second, self.vault.get_schema("account", "2024-03-01T00:00:00Z"))

    def test_empty_attributes_are_allowed(self):
        document = self.schema({})
        self.assertEqual({}, document["attributes"])

    def test_schema_submission_is_idempotent(self):
        first = self.schema({"tier": "string"}, key="schema-key")
        self.clock.advance(days=1)
        replayed = self.schema({"tier": "string"}, key="schema-key")
        self.assertEqual(first, replayed)
        self.assertEqual(1, self.vault.get_schema("account")["version"])

    def test_schema_key_cannot_be_reused_for_another_operation(self):
        self.schema({"tier": "string"}, key="shared")
        with self.assertRaises(ConflictError):
            self.vault.put_schema(
                "other", {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}}, "shared"
            )

    def test_schema_requires_an_idempotency_key(self):
        with self.assertRaisesRegex(ValidationError, "Idempotency-Key"):
            self.vault.put_schema(
                "account", {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}}, None
            )

    def test_schema_body_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValidationError, "unknown field"):
            self.vault.put_schema(
                "account",
                {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}, "name": "x"},
                "k1",
            )

    def test_schema_body_requires_effective_from_and_attributes(self):
        with self.assertRaisesRegex(ValidationError, "effective_from is required"):
            self.vault.put_schema("account", {"attributes": {}}, "k1")
        with self.assertRaisesRegex(ValidationError, "attributes is required"):
            self.vault.put_schema("account", {"effective_from": "2024-01-01T00:00:00Z"}, "k2")

    def test_schema_rejects_an_illegal_attribute_name(self):
        with self.assertRaises(ValidationError):
            self.schema({"not a name": "string"})

    def test_schema_rejects_an_illegal_attribute_type(self):
        with self.assertRaisesRegex(ValidationError, "type must be one of"):
            self.schema({"tier": "text"})
        with self.assertRaisesRegex(ValidationError, "type must be one of"):
            self.schema({"tier": "integer"})

    def test_schema_rejects_a_future_effective_from(self):
        with self.assertRaisesRegex(ValidationError, "must not be in the future"):
            self.schema({"tier": "string"}, effective_from="2024-06-01T00:00:00Z")

    # -- bitemporal lookup ----------------------------------------------------

    def test_get_schema_defaults_to_the_current_instant(self):
        self.schema({"tier": "string"})
        document = self.vault.get_schema("account")
        self.assertEqual("2024-01-01T00:00:00.000Z", document["effective_from"])
        self.assertEqual("2024-05-01T00:00:00.000Z", document["recorded_at"])

    def test_get_schema_selects_by_both_coordinates(self):
        self.schema({"tier": "string"})
        self.clock.advance(days=10)
        self.schema({"tier": "string", "level": "number"}, effective_from="2024-03-01T00:00:00Z")
        first = self.vault.get_schema("account", "2024-04-01T00:00:00Z", "2024-05-05T00:00:00Z")
        second = self.vault.get_schema("account", "2024-04-01T00:00:00Z", "2024-05-11T00:00:00Z")
        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"])

    def test_later_version_at_the_same_start_is_visible_only_from_its_recording(self):
        self.schema({"tier": "string"}, key="s1")
        self.clock.advance(days=10)
        self.schema({"tier": "string", "level": "number"}, key="s2")
        before = self.vault.get_schema("account", "2024-01-01T00:00:00Z", "2024-05-05T00:00:00Z")
        after = self.vault.get_schema("account", "2024-01-01T00:00:00Z", "2024-05-11T00:00:00Z")
        self.assertEqual(1, before["version"])
        self.assertEqual(2, after["version"])

    def test_get_schema_without_a_visible_version_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account")
        self.schema({"tier": "string"})
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account", "2023-01-01T00:00:00Z")
        with self.assertRaises(NotFoundError):
            self.vault.get_schema("account", known_at="2024-04-01T00:00:00Z")

    # -- commit-time history check --------------------------------------------

    def test_schema_conflicts_with_an_undeclared_attribute_in_its_interval(self):
        self.account(status="active")
        with self.assertRaisesRegex(ConflictError, "status is not declared"):
            self.schema({"tier": "string"})
        self.assertEqual(0, len(self.vault.store.schemas_for_type("account")))

    def test_schema_conflicts_with_a_mistyped_value_in_its_interval(self):
        self.account(tier=7)
        with self.assertRaisesRegex(ConflictError, "must be of type string"):
            self.schema({"tier": "string"})

    def test_a_failed_schema_appends_no_version(self):
        self.account(status="active")
        with self.assertRaises(ConflictError):
            self.schema({"tier": "string"}, key="bad")
        self.correct([{"attribute": "status", "deleted": True}], as_of="2024-01-01T00:00:00Z")
        document = self.schema({"tier": "string"}, key="good")
        self.assertEqual(1, document["version"])

    def test_schema_check_ignores_history_outside_its_interval(self):
        self.account(tier=7)
        # The mistyped value is withdrawn before the schema starts holding.
        self.correct([{"attribute": "tier", "deleted": True}], as_of="2024-02-01T00:00:00Z")
        document = self.schema({"tier": "string"}, effective_from="2024-03-01T00:00:00Z")
        self.assertEqual(1, document["version"])

    def test_schema_check_stops_at_the_next_schema_start(self):
        self.account(tier="gold")
        self.schema({"tier": "string"}, effective_from="2024-01-01T00:00:00Z", key="s1")
        # Migrate tier to a number from April on: retract it (never typed),
        # register the April contract, then assert the number under it.
        self.correct([{"attribute": "tier", "deleted": True}], as_of="2024-04-01T00:00:00Z")
        self.schema({"tier": "number"}, effective_from="2024-04-01T00:00:00Z", key="s2")
        self.correct([{"attribute": "tier", "value": 5}], as_of="2024-04-01T00:00:00Z")
        # A schema backdated between the two only checks its own slice
        # [2024-02-01, 2024-04-01): tier is a string there, so this passes even
        # though tier holds a number from April onwards.
        document = self.schema({"tier": "string"}, effective_from="2024-02-01T00:00:00Z", key="s3")
        self.assertEqual(3, document["version"])
        # The same slice checked against ``number`` fails, because tier holds
        # "gold" throughout it.
        with self.assertRaises(ConflictError):
            self.schema({"tier": "number"}, effective_from="2024-02-01T00:00:00Z", key="s4")

    def test_schema_check_sees_windows_started_before_effective_from(self):
        self.account(tier="gold")
        # level is asserted from February, while no schema exists yet.
        self.correct([{"attribute": "level", "value": 3}], as_of="2024-02-01T00:00:00Z")
        # A schema starting in March still covers level's open window.
        with self.assertRaisesRegex(ConflictError, "level is not declared"):
            self.schema({"tier": "string"}, effective_from="2024-03-01T00:00:00Z", key="s2")
        document = self.schema(
            {"tier": "string", "level": "number"}, effective_from="2024-03-01T00:00:00Z", key="s3"
        )
        self.assertEqual(1, document["version"])

    def test_schema_commit_rewrites_no_facts(self):
        self.account(tier="gold")
        self.schema({"tier": "string"})
        before = self.vault.history("account", "acct-1")
        self.clock.advance(days=1)
        self.schema({"tier": "string", "level": "number"}, effective_from="2024-03-01T00:00:00Z")
        after = self.vault.history("account", "acct-1", "2024-05-01T00:00:00Z")
        self.assertEqual(before, after)

    # -- write-time enforcement -----------------------------------------------

    def test_create_with_an_undeclared_attribute_is_rejected(self):
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "status is not declared"):
            self.account(status="active")
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "acct-1")

    def test_create_with_a_mistyped_attribute_is_rejected(self):
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "must be of type string"):
            self.account(tier=7)

    def test_number_does_not_include_boolean(self):
        self.schema({"flag": "number"})
        with self.assertRaisesRegex(ValidationError, "must be of type number"):
            self.account(flag=True)
        self.schema({"switch": "boolean"}, entity_type="device", key="other")
        with self.assertRaisesRegex(ValidationError, "must be of type boolean"):
            self.vault.create_entity(
                "device", {"id": "d1", "attributes": {"switch": 1}}, "create-device"
            )

    def test_correction_with_an_undeclared_attribute_is_rejected(self):
        self.account(tier="gold")
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "level is not declared"):
            self.correct([{"attribute": "level", "value": 3}])
        self.assertEqual({"tier": "gold"}, {
            name: entry["value"]
            for name, entry in self.vault.entity_as_of("account", "acct-1")["attributes"].items()
        })

    def test_correction_with_a_mistyped_value_is_rejected(self):
        self.account(tier="gold")
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "must be of type string"):
            self.correct([{"attribute": "tier", "value": 9}])

    def test_retraction_is_not_type_checked(self):
        self.account(tier="gold")
        self.schema({"tier": "string"})
        document = self.correct([{"attribute": "tier", "deleted": True}])
        self.assertEqual({}, document["attributes"])

    def test_asserted_null_must_be_declared_as_null(self):
        self.schema({"note": "null"})
        self.account(note=None, key="create-1")
        with self.assertRaisesRegex(ValidationError, "must be of type null"):
            self.correct([{"attribute": "note", "value": "text"}])
        # Migrate the contract through a retraction, which is never typed.
        self.correct([{"attribute": "note", "deleted": True}], as_of="2024-02-01T00:00:00Z")
        self.schema({"note": "string"}, effective_from="2024-02-01T00:00:00Z", key="s2")
        with self.assertRaisesRegex(ValidationError, "must be of type string"):
            self.correct([{"attribute": "note", "value": None}], as_of="2024-02-01T00:00:00Z")

    def test_a_fact_window_spanning_schema_intervals_must_satisfy_each(self):
        self.schema({"tier": "string"}, effective_from="2024-01-01T00:00:00Z", key="s1")
        # Bound the entity's window exactly at April while no April contract
        # exists yet, so the April schema can be registered afterwards.
        self.vault.create_entity(
            "account",
            {"id": "acct-2", "attributes": {"tier": "gold"}, "valid_from": "2024-02-01T00:00:00Z"},
            "create-2",
        )
        self.correct(
            [{"attribute": "tier", "value": "gold",
              "valid_from": "2024-02-01T00:00:00Z", "valid_end": "2024-04-01T00:00:00Z"}],
            entity_id="acct-2",
        )
        self.schema({"tier": "number"}, effective_from="2024-04-01T00:00:00Z", key="s2")
        # A create whose open ended window crosses the April boundary is checked
        # against both contracts and fails the second.
        with self.assertRaisesRegex(ValidationError, "must be of type number"):
            self.account(tier="gold", valid_from="2024-02-01T00:00:00Z")
        # Reopening the bounded window across the boundary fails the same way.
        with self.assertRaisesRegex(ValidationError, "must be of type number"):
            self.correct(
                [{"attribute": "tier", "value": "gold", "valid_from": "2024-02-01T00:00:00Z"}],
                entity_id="acct-2",
            )

    def test_types_without_a_schema_keep_free_semantics(self):
        self.schema({"tier": "string"})
        document = self.vault.create_entity(
            "device", {"id": "d1", "attributes": {"anything": 1, "flag": True}}, "dev-1"
        )
        self.assertEqual("device", document["type"])

    def test_failed_write_leaves_no_trace(self):
        self.account(tier="gold")
        self.schema({"tier": "string"})
        before = self.vault.history("account", "acct-1")
        with self.assertRaises(ValidationError):
            self.correct([{"attribute": "tier", "value": 9}])
        self.assertEqual(before, self.vault.history("account", "acct-1"))

    # -- batch ----------------------------------------------------------------

    def test_batch_reports_the_failing_operation_and_rolls_back(self):
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "operation 2: .*status is not declared"):
            self.vault.run_batch(
                {
                    "operations": [
                        {"operation": "create", "type": "account", "id": "acct-1",
                         "attributes": {"tier": "gold"}},
                        {"operation": "correct", "type": "account", "id": "acct-1",
                         "facts": [{"attribute": "status", "value": "active"}]},
                    ]
                },
                "batch-1",
            )
        with self.assertRaises(NotFoundError):
            self.vault.history("account", "acct-1")

    def test_batch_create_is_checked_against_the_schema(self):
        self.schema({"tier": "string"})
        with self.assertRaisesRegex(ValidationError, "operation 1: .*must be of type string"):
            self.vault.run_batch(
                {"operations": [
                    {"operation": "create", "type": "account", "id": "acct-1",
                     "attributes": {"tier": 7}},
                ]},
                "batch-1",
            )


class HttpSchemaTests(unittest.TestCase):
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

    def test_put_and_get_schema_over_http(self):
        status, body = self.call(
            "PUT", "/schemas/account",
            {"effective_from": "2024-01-01T00:00:00Z", "attributes": {"tier": "string"}},
            key="s1",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, body["version"])
        status, body = self.call("GET", "/schemas/account")
        self.assertEqual(200, status)
        self.assertEqual({"tier": "string"}, body["attributes"])

    def test_get_schema_is_not_found_without_a_visible_version(self):
        status, body = self.call("GET", "/schemas/account")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

    def test_duplicate_query_parameter_is_a_validation_error(self):
        self.call("PUT", "/schemas/account",
                  {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}}, key="s1")
        status, body = self.call("GET", "/schemas/account?as_of=2024-01-01T00:00:00Z&as_of=2024-02-01T00:00:00Z")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_unknown_query_parameter_is_a_validation_error(self):
        status, body = self.call("GET", "/schemas/account?when=2024-01-01T00:00:00Z")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_put_schema_requires_an_idempotency_key(self):
        status, body = self.call(
            "PUT", "/schemas/account",
            {"effective_from": "2024-01-01T00:00:00Z", "attributes": {}},
        )
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_schema_routes_reject_other_verbs_and_depths(self):
        self.assertEqual(404, self.call("POST", "/schemas/account", {}, key="k")[0])
        self.assertEqual(404, self.call("GET", "/schemas")[0])
        self.assertEqual(404, self.call("GET", "/schemas/account/extra")[0])


if __name__ == "__main__":
    unittest.main()
