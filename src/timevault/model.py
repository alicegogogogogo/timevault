from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import ValidationError

# The store keeps every time coordinate as an integer number of milliseconds
# since the Unix epoch, UTC.  That makes ordering exact and comparisons cheap.
MILLISECOND = 1
SECOND = 1_000
MINUTE = 60 * SECOND
MAX_INSTANT = (2**53 - 1) * MILLISECOND
"""Largest representable instant.  Any later instant normalises to this value."""

INFINITY = MAX_INSTANT
"""Exclusive upper bound used when a valid window is open ended."""

_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_RFC3339 = re.compile(
    r"\A(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?([Zz]|[+-]\d{2}:?\d{2})?\Z"
)


def identifier(value: Any, field: str, limit: int = 100) -> str:
    """Validate a type name or entity identifier."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ValidationError(f"{field} must be at most {limit} characters")
    if not _IDENTIFIER.match(value):
        raise ValidationError(f"{field} must match {_IDENTIFIER.pattern}")
    return value


def attribute_name(value: Any) -> str:
    return identifier(value, "attribute name", limit=64)


def _from_datetime(moment: datetime, field: str) -> int:
    if moment.tzinfo is None:
        raise ValidationError(f"{field} must include a UTC offset or a trailing Z")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _from_text(text: str, field: str) -> int:
    match = _RFC3339.match(text)
    if match is None:
        raise ValidationError(
            f"{field} must be an RFC 3339 instant such as 2024-05-01T00:00:00Z"
        )
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    micros = int((fraction or "").ljust(6, "0") or 0)
    try:
        moment = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micros, timezone.utc
        )
    except ValueError as error:
        raise ValidationError(f"{field} is not a valid calendar instant: {error}") from error
    # ``_from_datetime`` already folds the fraction in through ``micros``; adding
    # it again here would push every instant with fractional seconds forward by
    # up to a second and make an exact boundary instant unrepresentable.
    millis = _from_datetime(moment, field)
    if offset is None or offset.upper() == "Z":
        return millis
    sign = 1 if offset[0] == "+" else -1
    digits = offset[1:].replace(":", "")
    shift = (int(digits[:2]) * 60 + int(digits[2:4])) * MINUTE
    return millis - sign * shift


def instant(value: Any, field: str) -> int:
    """Parse one time coordinate.

    Accepted forms are an RFC 3339 instant, an integer count of milliseconds
    since the epoch, or a float count of seconds since the epoch.  Instants
    after ``MAX_INSTANT`` are clamped to ``INFINITY - 1`` so that arithmetic on
    an open ended window can never overflow.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an instant, not a boolean")
    if isinstance(value, datetime):
        millis = _from_datetime(value, field)
    elif isinstance(value, str):
        millis = _from_text(value.strip(), field)
    elif isinstance(value, int):
        millis = value * MILLISECOND
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError(f"{field} must be a finite instant")
        millis = int(value * SECOND)
    else:
        raise ValidationError(
            f"{field} must be an RFC 3339 string, an integer of milliseconds, or a float of seconds"
        )
    return min(millis, INFINITY - MILLISECOND)


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    raise ValidationError(
        "attribute values must be a string, a number, a boolean, or null (objects and arrays are not allowed)"
    )


def value_same(left: Any, right: Any) -> bool:
    """Structural equality that keeps booleans distinct from numbers."""
    left_type = value_type(left)
    if left_type != value_type(right):
        return False
    if left_type == "number":
        return left == right
    return left == right


def _optional_instant(value: Any, field: str) -> int | None:
    return None if value is None else instant(value, field)


@dataclass(frozen=True)
class Attribute:
    """One named attribute of an entity, with the value it holds."""

    name: str
    value: Any

    @classmethod
    def parse(cls, name: Any, value: Any) -> "Attribute":
        value_type(value)
        return cls(attribute_name(name), value)

    @property
    def type(self) -> str:
        return value_type(self.value)


@dataclass(frozen=True)
class Entity:
    """A set of attributes created at one transaction time."""

    type: str
    id: str
    attributes: tuple[Attribute, ...]

    @classmethod
    def parse(cls, entity_type: Any, raw: Any) -> "Entity":
        if not isinstance(raw, dict):
            raise ValidationError("request body must be a JSON object")
        unknown = sorted(set(raw) - {"id", "attributes", "valid_from"})
        if unknown:
            raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
        for required in ("id", "attributes"):
            if required not in raw:
                raise ValidationError(f"{required} is required")
        entity_id = identifier(raw["id"], "entity id")
        raw_attributes = raw["attributes"]
        if not isinstance(raw_attributes, dict) or not raw_attributes:
            raise ValidationError("attributes must be a non-empty object")
        attributes = tuple(
            Attribute.parse(name, value) for name, value in raw_attributes.items()
        )
        return cls(identifier(entity_type, "entity type"), entity_id, attributes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "attributes": {attribute.name: attribute.value for attribute in self.attributes},
        }


@dataclass(frozen=True)
class Fact:
    """One requested change to a single attribute over a valid window.

    ``valid_from`` may be omitted, in which case the fact starts at the valid
    instant of the correction that carries it.  A fact with an explicit
    ``valid_from`` is placed exactly there; when that instant is earlier than
    what the attribute currently says, the new value splits the window it lands
    in and the correction also truncates everything the attribute claimed from
    that instant onwards.
    """

    attribute: str
    value: Any
    valid_from: int | None
    valid_end: int | None
    deleted: bool
    operation: str

    @classmethod
    def parse(cls, raw: Any) -> "Fact":
        if not isinstance(raw, dict):
            raise ValidationError("each fact must be a JSON object")
        unknown = sorted(set(raw) - {"attribute", "value", "valid_from", "valid_end", "deleted"})
        if unknown:
            raise ValidationError(f"unknown field(s) in fact: {', '.join(unknown)}")
        if "attribute" not in raw:
            raise ValidationError("each fact requires an attribute")
        name = attribute_name(raw["attribute"])

        raw_deleted = raw.get("deleted", False)
        if not isinstance(raw_deleted, bool):
            raise ValidationError(f"fact {name}: deleted must be a boolean")
        if raw_deleted:
            if "value" in raw:
                raise ValidationError(f"fact {name}: a retraction must not carry a value")
            operation = "retract"
            value = None
        else:
            if "value" not in raw:
                raise ValidationError(f"fact {name}: an assertion requires a value")
            value = raw["value"]
            value_type(value)
            operation = "assert"

        valid_from = _optional_instant(raw.get("valid_from"), f"fact {name}: valid_from")
        valid_end = _optional_instant(raw.get("valid_end"), f"fact {name}: valid_end")
        if valid_from is not None and valid_end is not None and valid_end <= valid_from:
            raise ValidationError(
                f"fact {name}: valid_end must be later than valid_from (windows are non-empty half-open intervals)"
            )
        return cls(name, value, valid_from, valid_end, raw_deleted, operation)

    def at(self, valid_from: int) -> "Fact":
        """Fill in an omitted ``valid_from`` with the correction's instant.

        A fact that carries its own ``valid_from`` keeps it: that is what makes a
        correction able to restate the past.
        """
        if self.valid_from is not None:
            return self
        return Fact(self.attribute, self.value, valid_from, self.valid_end, self.deleted, self.operation)


@dataclass(frozen=True)
class Correction:
    """A batch of facts applied at one transaction time."""

    as_of: int
    known_at: int
    facts: tuple[Fact, ...]

    @classmethod
    def parse(cls, raw: Any, now: int) -> "Correction":
        if not isinstance(raw, dict):
            raise ValidationError("request body must be a JSON object")
        unknown = sorted(set(raw) - {"as_of", "valid_from", "facts"})
        if unknown:
            raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
        if "as_of" in raw and "valid_from" in raw:
            raise ValidationError("as_of and valid_from are aliases; send only one of them")
        facts_raw = raw.get("facts")
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ValidationError("facts must be a non-empty array")

        parsed = [Fact.parse(fact) for fact in facts_raw]
        names = [fact.attribute for fact in parsed]
        if len(names) != len(set(names)):
            raise ValidationError("facts must not contain the same attribute twice")

        if "as_of" in raw:
            default_from = instant(raw["as_of"], "as_of")
        elif "valid_from" in raw:
            default_from = instant(raw["valid_from"], "valid_from")
        else:
            default_from = now
        if default_from > now:
            raise ValidationError("as_of must not be in the future")
        facts = tuple(fact.at(default_from) for fact in parsed)
        for fact in facts:
            valid_from = fact.valid_from
            if valid_from is None:
                raise ValidationError("every fact needs a valid_from")
            if valid_from > now:
                raise ValidationError(
                    f"fact {fact.attribute}: valid_from must not be in the future"
                )
            if fact.valid_end is not None and fact.valid_end <= valid_from:
                raise ValidationError(
                    f"fact {fact.attribute}: valid_end must be later than the window start"
                )
        return cls(default_from, now, facts)


@dataclass(frozen=True)
class BatchCreate:
    """One ``create`` item of a batch, parsed and ready to commit.

    ``valid_from`` is ``None`` when the item declared none, in which case the
    batch's transaction instant is the start of every initial window.
    """

    entity_type: str
    entity_id: str
    attributes: tuple[Attribute, ...]
    valid_from: int | None


@dataclass(frozen=True)
class BatchCorrect:
    """One ``correct`` item of a batch, parsed against the batch's clock.

    The carried :class:`Correction` has every fact defaulted to the item's
    ``as_of`` (or the request instant) and all bounds already validated.
    """

    entity_type: str
    entity_id: str
    correction: "Correction"


_BATCH_OPERATION_FIELDS = {
    "create": {"operation", "type", "id", "attributes", "valid_from"},
    "correct": {"operation", "type", "id", "as_of", "facts"},
}


def _batch_op_kind(raw: dict[str, Any], index: int) -> str:
    """Identify a batch item by its ``operation`` discriminator."""
    label = f"operation {index}"
    kind = raw.get("operation")
    if not isinstance(kind, str) or kind not in ("create", "correct"):
        raise ValidationError(
            f'{label}: operation must be "create" or "correct"'
        )
    return kind


def _parse_batch_operation(raw: Any, index: int, now: int) -> BatchCreate | BatchCorrect:
    """Parse and validate one batch item, prefixing every error with its index.

    Indices are one-based, because that is the position the request lists the
    operation at.  Semantic checks that need the committed ledger (duplicate
    create, correcting before creation) happen later, inside the transaction.
    """
    label = f"operation {index}"
    try:
        if not isinstance(raw, dict):
            raise ValidationError("each operation must be a JSON object")
        kind = _batch_op_kind(raw, index)
        unknown = sorted(set(raw) - _BATCH_OPERATION_FIELDS[kind])
        if unknown:
            raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
        entity_type = identifier(raw.get("type"), "type")
        entity_id = identifier(raw.get("id"), "id")

        if kind == "create":
            if "attributes" not in raw:
                raise ValidationError("attributes is required")
            body: dict[str, Any] = {"id": entity_id, "attributes": raw["attributes"]}
            valid_from = None
            if "valid_from" in raw:
                body["valid_from"] = raw["valid_from"]
                valid_from = instant(raw["valid_from"], "valid_from")
                if valid_from > now:
                    raise ValidationError("valid_from must not be in the future")
            entity = Entity.parse(entity_type, body)
            return BatchCreate(entity_type, entity_id, entity.attributes, valid_from)

        if "facts" not in raw:
            raise ValidationError("facts is required")
        correction_body: dict[str, Any] = {"facts": raw["facts"]}
        if "as_of" in raw:
            correction_body["as_of"] = raw["as_of"]
        correction = Correction.parse(correction_body, now)
        return BatchCorrect(entity_type, entity_id, correction)
    except ValidationError as error:
        message = str(error)
        if message.startswith(f"{label}:"):
            raise
        raise ValidationError(f"{label}: {message}") from error


def parse_batch(raw: Any, now: int) -> tuple[BatchCreate | BatchCorrect, ...]:
    """Validate a ``POST /batch`` request body into an ordered list of items."""
    if not isinstance(raw, dict):
        raise ValidationError("request body must be a JSON object")
    unknown = sorted(set(raw) - {"operations"})
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
    operations = raw.get("operations")
    if not isinstance(operations, list):
        raise ValidationError("operations must be an array")
    if not operations:
        raise ValidationError("operations must contain at least one item")
    if len(operations) > 1000:
        raise ValidationError("operations may contain at most 1000 items")
    return tuple(
        _parse_batch_operation(item, index, now)
        for index, item in enumerate(operations, start=1)
    )


def epoch_millis(moment: datetime) -> int:
    """Convert an aware datetime to integer epoch milliseconds."""
    return _from_datetime(moment, "clock")


def iso(millis: int) -> str | None:
    """Render an epoch-millisecond instant the way the HTTP API reports it."""
    if millis >= INFINITY:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    moment = epoch + timedelta(milliseconds=millis)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
