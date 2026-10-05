from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import NotFoundError, TimeVaultError, ValidationError
from .model import attribute_name, instant
from .service import TimeVault


def one(query: dict[str, list[str]], name: str) -> str | None:
    """Read a query parameter that may appear at most once."""
    values = query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ValidationError(f"query parameter {name} may be given at most once")
    return values[0]


def many(query: dict[str, list[str]], name: str) -> list[str] | None:
    """Read a repeatable query parameter."""
    values = query.get(name)
    return list(values) if values else None


def only(query: dict[str, list[str]], allowed: set[str]) -> None:
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValidationError(f"unknown query parameter(s): {', '.join(unknown)}")


def make_handler(service: TimeVault) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise ValidationError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1_000_000:
                    raise ValueError
                return json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError) as error:
                raise ValidationError("request body must be valid JSON") from error

        def _segments(self) -> tuple[list[str], dict[str, list[str]]]:
            split = urlsplit(self.path)
            parts = [unquote(part) for part in split.path.split("/") if part]
            return parts, parse_qs(split.query, keep_blank_values=True)

        def _instant_parameter(self, query: dict[str, list[str]], name: str) -> int | None:
            raw = one(query, name)
            return None if raw is None else instant(raw, name)

        def _dispatch(self) -> tuple[int, Any]:
            parts, query = self._segments()
            key = self.headers.get("Idempotency-Key")
            if parts == ["health"] and self.command == "GET":
                only(query, set())
                return 200, {"status": "ok"}
            if parts == ["batch"]:
                if self.command != "POST":
                    raise NotFoundError("route was not found")
                only(query, set())
                return 200, service.run_batch(self._body(), key)
            if parts == ["diff"]:
                return self._diff(query)
            if parts == ["snapshot-diff"]:
                return self._snapshot_diff(query)
            if parts == ["audit"]:
                return self._audit(query)
            if parts and parts[0] == "schemas":
                return self._schema_routes(parts[1:], query, key)
            if parts and parts[0] == "entities":
                return self._entity_routes(parts[1:], query, key)
            raise NotFoundError("route was not found")

        def _entity_routes(
            self, parts: list[str], query: dict[str, list[str]], key: str | None
        ) -> tuple[int, Any]:
            if len(parts) == 1:
                if self.command != "POST":
                    raise NotFoundError("route was not found")
                only(query, set())
                return 201, service.create_entity(parts[0], self._body(), key)
            if len(parts) == 2:
                entity_type, entity_id = parts
                if self.command == "PUT":
                    only(query, set())
                    return 200, service.apply_correction(entity_type, entity_id, self._body(), key)
                if self.command == "GET":
                    only(query, {"as_of", "known_at"})
                    return 200, service.entity_as_of(
                        entity_type,
                        entity_id,
                        self._instant_parameter(query, "as_of"),
                        self._instant_parameter(query, "known_at"),
                    )
                raise NotFoundError("route was not found")
            if len(parts) == 3 and parts[2] == "history" and self.command == "GET":
                only(query, {"known_at"})
                return 200, service.history(
                    parts[0], parts[1], self._instant_parameter(query, "known_at")
                )
            if len(parts) == 3 and parts[2] == "timeline" and self.command == "GET":
                only(query, {"from", "to", "known_at"})
                raw_from = one(query, "from")
                raw_to = one(query, "to")
                if raw_from is None or raw_to is None:
                    raise ValidationError("timeline requires both from and to")
                return 200, service.timeline(
                    parts[0],
                    parts[1],
                    instant(raw_from, "from"),
                    instant(raw_to, "to"),
                    self._instant_parameter(query, "known_at"),
                )
            raise NotFoundError("route was not found")

        def _schema_routes(
            self, parts: list[str], query: dict[str, list[str]], key: str | None
        ) -> tuple[int, Any]:
            if len(parts) != 1:
                raise NotFoundError("route was not found")
            if self.command == "PUT":
                only(query, set())
                return 200, service.put_schema(parts[0], self._body(), key)
            if self.command == "GET":
                only(query, {"as_of", "known_at"})
                return 200, service.get_schema(
                    parts[0],
                    self._instant_parameter(query, "as_of"),
                    self._instant_parameter(query, "known_at"),
                )
            raise NotFoundError("route was not found")

        def _diff(self, query: dict[str, list[str]]) -> tuple[int, Any]:
            if self.command != "GET":
                raise NotFoundError("route was not found")
            only(query, {"type", "id", "from", "to", "known_at", "attribute"})
            entity_type = one(query, "type")
            entity_id = one(query, "id")
            raw_from = one(query, "from")
            raw_to = one(query, "to")
            if entity_type is None or entity_id is None:
                raise ValidationError("diff requires both type and id")
            if raw_from is None or raw_to is None:
                raise ValidationError("diff requires both from and to")
            names = many(query, "attribute")
            if names is not None:
                names = [attribute_name(name) for name in names]
            return 200, service.diff(
                entity_type,
                entity_id,
                instant(raw_from, "from"),
                instant(raw_to, "to"),
                self._instant_parameter(query, "known_at"),
                names,
            )

        def _snapshot_diff(self, query: dict[str, list[str]]) -> tuple[int, Any]:
            if self.command != "GET":
                raise NotFoundError("route was not found")
            only(
                query,
                {"type", "id", "first_as_of", "first_known_at", "second_as_of", "second_known_at"},
            )
            return 200, service.snapshot_diff(
                one(query, "type"),
                one(query, "id"),
                self._instant_parameter(query, "first_as_of"),
                self._instant_parameter(query, "first_known_at"),
                self._instant_parameter(query, "second_as_of"),
                self._instant_parameter(query, "second_known_at"),
            )

        def _audit(self, query: dict[str, list[str]]) -> tuple[int, Any]:
            if self.command != "GET":
                raise NotFoundError("route was not found")
            only(
                query,
                {
                    "type",
                    "id",
                    "attribute",
                    "action",
                    "recorded_from",
                    "recorded_to",
                    "limit",
                    "cursor",
                },
            )
            return 200, service.audit(
                entity_type=one(query, "type"),
                entity_id=one(query, "id"),
                attribute=one(query, "attribute"),
                action=one(query, "action"),
                recorded_from=one(query, "recorded_from"),
                recorded_to=one(query, "recorded_to"),
                limit=one(query, "limit"),
                cursor=one(query, "cursor"),
            )

        def _handle(self) -> None:
            try:
                status, response = self._dispatch()
                self._json(status, response)
            except TimeVaultError as error:
                self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
            except Exception:
                self._json(500, {"error": {"code": "internal_error", "message": "internal server error"}})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TimeVault HTTP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--database", default="timevault.db")
    arguments = parser.parse_args()
    service = TimeVault(arguments.database)
    server = ThreadingHTTPServer((arguments.host, arguments.port), make_handler(service))
    print(f"TimeVault listening on http://{arguments.host}:{arguments.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
