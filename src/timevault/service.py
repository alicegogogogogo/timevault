from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ConflictError, NotFoundError, TimeVaultError, ValidationError
from .model import (
    BatchCorrect,
    BatchCreate,
    Correction,
    Entity,
    Fact,
    INFINITY,
    Schema,
    attribute_name,
    identifier,
    instant,
    iso,
    parse_batch,
    value_same,
    value_type,
)
from .store import ACTION_VERSION_APPENDED, ACTION_WINDOW_TRUNCATED, Store

# Difference tags returned by :meth:`TimeVault.compare_views`.
DIFF_ADDED = "added"
DIFF_REMOVED = "removed"
DIFF_CHANGED = "changed"

DEFAULT_AUDIT_LIMIT = 100
MAX_AUDIT_LIMIT = 500
_AUDIT_ACTIONS = (ACTION_VERSION_APPENDED, ACTION_WINDOW_TRUNCATED)
# Pinned filter set carried inside a cursor; nothing else may appear there.
_CURSOR_FILTER_KEYS = ("action", "type", "id", "attribute", "recorded_from", "recorded_to")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise ValidationError("cursor is unknown or malformed") from error


# A cursor is self-describing, but it is also signed so a hand-edited or
# truncated token fails the same way an unknown one does instead of silently
# walking someone else's result set.
_CURSOR_VERSION = 1
_CURSOR_SALT = b"timevault-audit-cursor-v1"


@dataclass(frozen=True)
class AuditFilters:
    """The validated filter set of one audit query."""

    action: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    attribute: str | None = None
    recorded_from: int | None = None
    recorded_to: int | None = None

    def as_cursor(self) -> dict[str, Any]:
        saved: dict[str, Any] = {}
        if self.action is not None:
            saved["action"] = self.action
        if self.entity_type is not None:
            saved["type"] = self.entity_type
        if self.entity_id is not None:
            saved["id"] = self.entity_id
        if self.attribute is not None:
            saved["attribute"] = self.attribute
        if self.recorded_from is not None:
            saved["recorded_from"] = self.recorded_from
        if self.recorded_to is not None:
            saved["recorded_to"] = self.recorded_to
        return saved

    @classmethod
    def from_cursor(cls, saved: Any) -> "AuditFilters":
        if not isinstance(saved, dict):
            raise ValidationError("cursor is unknown or malformed")
        unknown = sorted(set(saved) - set(_CURSOR_FILTER_KEYS))
        if unknown:
            raise ValidationError("cursor is unknown or malformed")
        strings = {"action": None, "type": None, "id": None, "attribute": None}
        for key in strings:
            if key in saved and not isinstance(saved[key], str):
                raise ValidationError("cursor is unknown or malformed")
        times: dict[str, int | None] = {}
        for key in ("recorded_from", "recorded_to"):
            if key in saved:
                if not isinstance(saved[key], int) or isinstance(saved[key], bool):
                    raise ValidationError("cursor is unknown or malformed")
                times[key] = saved[key]
        action = saved.get("action")
        if action is not None and action not in _AUDIT_ACTIONS:
            raise ValidationError("cursor is unknown or malformed")
        return cls(
            action=action,
            entity_type=saved.get("type"),
            entity_id=saved.get("id"),
            attribute=saved.get("attribute"),
            recorded_from=times.get("recorded_from"),
            recorded_to=times.get("recorded_to"),
        )


def _encode_cursor(filters: AuditFilters, upper_seq: int, after: tuple[int, str] | None) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "f": filters.as_cursor(),
            "u": upper_seq,
            "a": None if after is None else [after[0], after[1]],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hashlib.sha256(_CURSOR_SALT + b"." + payload).digest()
    return _b64url(payload) + "." + _b64url(signature)


def _decode_cursor(token: str) -> tuple[AuditFilters, int, tuple[int, str] | None]:
    bad = ValidationError("cursor is unknown or malformed")
    if not isinstance(token, str) or "." not in token:
        raise bad
    payload_text, signature_text = token.rsplit(".", 1)
    try:
        payload = _b64url_decode(payload_text)
        signature = _b64url_decode(signature_text)
    except ValidationError:
        raise bad from None
    if hashlib.sha256(_CURSOR_SALT + b"." + payload).digest() != signature:
        raise bad
    try:
        state = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(state, dict) or state.get("v") != _CURSOR_VERSION:
        raise bad
    upper_seq = state.get("u")
    if not isinstance(upper_seq, int) or isinstance(upper_seq, bool) or upper_seq < 0:
        raise bad
    after_raw = state.get("a")
    after: tuple[int, str] | None
    if after_raw is None:
        after = None
    elif (
        isinstance(after_raw, list)
        and len(after_raw) == 2
        and isinstance(after_raw[0], int)
        and not isinstance(after_raw[0], bool)
        and isinstance(after_raw[1], str)
    ):
        after = (after_raw[0], after_raw[1])
    else:
        raise bad
    return AuditFilters.from_cursor(state.get("f")), upper_seq, after


@dataclass(frozen=True)
class Truncation:
    """One window-closing event, stamped on both axes.

    ``recorded_at`` is the transaction instant the system learned the new bound,
    ``valid_end`` the business instant the window was closed at.
    """

    version: int
    recorded_at: int
    valid_end: int


@dataclass(frozen=True)
class Version:
    """One stored attribute version, as read back from the database.

    ``valid_from`` is the business instant the value starts at.  ``declared_end``
    is the bound the assertion itself declared and never moves; ``valid_end`` is
    where the window ends once every correction so far has been applied, so it is
    ``None`` while the window is open ended.  ``recorded_at`` is the transaction
    instant the row was written.
    """

    attribute: str
    version: int
    operation: str
    value: Any
    valid_from: int
    valid_end: int | None
    declared_end: int | None
    recorded_at: int
    truncations: tuple[Truncation, ...] = ()

    @classmethod
    def from_row(cls, row: Any) -> "Version":
        return cls(
            attribute=str(row["attribute"]),
            version=int(row["version"]),
            operation=str(row["operation"]),
            value=None if row["value"] is None else Store.decode(row["value"]),
            valid_from=int(row["valid_from"]),
            valid_end=None if row["valid_end"] is None else int(row["valid_end"]),
            declared_end=None if row["declared_end"] is None else int(row["declared_end"]),
            recorded_at=int(row["recorded_at"]),
        )

    def with_truncations(self, events: tuple[Truncation, ...]) -> "Version":
        return Version(
            self.attribute,
            self.version,
            self.operation,
            self.value,
            self.valid_from,
            self.valid_end,
            self.declared_end,
            self.recorded_at,
            events,
        )

    def known_truncations(self, known_at: int) -> tuple[Truncation, ...]:
        return tuple(event for event in self.truncations if event.recorded_at <= known_at)

    def superseded_at(self) -> int | None:
        """The transaction instant this version stopped being current, if it has.

        That is the instant of a trim, never a business instant, so together with
        ``recorded_at`` it bounds a half-open transaction interval and can never
        precede it.  When several corrections closed the same window the last one
        is the authoritative closure, because it is the one the ledger's final
        ``valid_end`` comes from; the earlier trims stay in the row's truncation
        events, which is how a reader before a later trim still sees the end that
        trim had not yet moved.
        """
        return None if not self.truncations else self.truncations[-1].recorded_at

    def end_known_at(self, known_at: int) -> int | None:
        """Where this version's window ended as of ``known_at``.

        The window end is never later than the bound the assertion declared, so
        the answer is the earliest of that declared bound and every trim the
        reader already knows about.  A trim recorded after ``known_at`` is not
        knowledge yet, so the window it closed is still open ended — that is what
        keeps the upper axis exact.
        """
        end = self.declared_end
        for event in self.known_truncations(known_at):
            end = event.valid_end if end is None else min(end, event.valid_end)
        if end is None:
            return None
        # ``valid_end`` already folds in every trim, including ones this reader
        # has not learned yet, so it is only ever a safe ceiling for the end the
        # reader knows — never a later bound leaking backwards.
        return self.valid_end if self.valid_end is None else min(end, self.valid_end)

    def as_dict(self, valid_end: Any = ...) -> dict[str, Any]:
        """Render the version, optionally with a recomputed window end.

        Pass ``None`` explicitly to report an open ended window; omitting the
        argument reports the end as it stands in the ledger for the reader.
        """
        end = self.valid_end if valid_end is ... else valid_end
        declared = self.declared_end
        if declared is None or end is None or end >= declared:
            declared_field: str | None = None
        else:
            declared_field = iso(declared)
        return {
            "attribute": self.attribute,
            "version": self.version,
            "operation": self.operation,
            "value": self.value,
            "valid_from": iso(self.valid_from),
            "valid_from_ms": self.valid_from,
            "valid_end": None if end is None else iso(end),
            "valid_end_ms": end,
            "declared_end": declared_field,
            "recorded_at": iso(self.recorded_at),
            "superseded_at": None
            if self.superseded_at() is None
            else iso(self.superseded_at()),
        }


Token = tuple[Version, int, int | None]
Projection = dict[str, tuple[Any, int, str, int, int | None, int]]


@dataclass(frozen=True)
class SchemaVersion:
    """One committed schema version, as read back from the database.

    ``effective_from`` is the business instant the contract starts holding;
    ``recorded_at`` is the transaction instant it was submitted.  A version is
    visible to a reader only from its own ``recorded_at`` onwards, so two
    versions sharing one ``effective_from`` do not clash: the one recorded
    later wins, but only for readers past its submission.
    """

    entity_type: str
    version: int
    effective_from: int
    recorded_at: int
    attributes: dict[str, str]

    @classmethod
    def from_row(cls, entity_type: str, row: Any) -> "SchemaVersion":
        return cls(
            entity_type,
            int(row["version"]),
            int(row["effective_from"]),
            int(row["recorded_at"]),
            Store.decode(row["attributes"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.entity_type,
            "version": self.version,
            "effective_from": iso(self.effective_from),
            "recorded_at": iso(self.recorded_at),
            "attributes": dict(self.attributes),
        }


def known_windows(versions: list[Version], known_at: int) -> list[Token]:
    """Reconstruct the valid windows of one attribute as known at ``known_at``.

    Every version is known from its ``recorded_at`` onwards, and every truncation
    it carries from the ``recorded_at`` of that truncation onwards.  Working from
    the newest version the reader can see back to the oldest, a version holds
    valid time from its own ``valid_from`` until the earliest of:

    * ``valid_from`` of the next version the reader can see, because the value
      written later is the one in force from then on;
    * the end it carries for that reader, which is its declared ``valid_end`` or,
      while the trim that pulled the end back is still unknown, nothing at all;
    * the instant a correction already known at ``known_at`` starts writing
      there, which is where the chain of visible corrections takes over.

    A version whose only displacers are recorded after ``known_at`` keeps an open
    ended window, because at that instant nobody had written the bound — that
    rule is what keeps a transaction-time view exact.  Tokens come back in
    transaction order, oldest first.  Retraction rows always carry an empty
    window: they exist to mark the instant an attribute ceased to exist.
    """
    ordered = sorted(versions, key=lambda item: (item.valid_from, item.version))
    tokens: list[Token] = []
    takeover: int | None = None
    for index in range(len(ordered) - 1, -1, -1):
        version = ordered[index]
        if version.recorded_at > known_at:
            # Not recorded yet for this reader, and it could not have displaced
            # anything either: it is invisible to the whole pass.
            continue
        following = next(
            (
                ordered[other]
                for other in range(index + 1, len(ordered))
                if ordered[other].recorded_at <= known_at
            ),
            None,
        )
        if version.operation == "retract":
            # A retraction never holds valid time of its own; it only closes the
            # window in front of it.
            end: int | None = version.valid_from
        else:
            end = version.end_known_at(known_at)
            if following is not None:
                # A version the reader can see takes over where it begins.
                end = following.valid_from if end is None else min(end, following.valid_from)
            if takeover is not None:
                # A correction already known at ``known_at`` starts writing at
                # that instant, which is the end of everything before it.
                end = takeover if end is None else min(end, takeover)
        tokens.append((version, version.valid_from, end))
        takeover = version.valid_from
    tokens.sort(key=lambda token: (token[0].recorded_at, token[0].version))
    return tokens


def complete_windows(versions: list[Version], known_at: int) -> list[Token]:
    """The windows as they stand in the ledger, for a history listing.

    Every version the reader can see keeps the end the ledger records for it,
    with any trim the reader cannot know about yet left out, so this is the
    full-knowledge view of the ledger for that transaction instant rather than a
    projection: a window a correction pulled back shows the pulled back end once
    that correction is knowable, and a value displaced by a backdated
    restatement keeps the part of its own window that no later version took over.
    """
    ordered = sorted(versions, key=lambda item: (item.valid_from, item.version))
    tokens: list[Token] = []
    for index, version in enumerate(ordered):
        if version.recorded_at > known_at:
            # Recorded after the reader's instant, so this reader never saw it.
            continue
        end = version.end_known_at(known_at)
        successor = next(
            (
                ordered[other]
                for other in range(index + 1, len(ordered))
                if ordered[other].recorded_at <= known_at
            ),
            None,
        )
        if successor is not None:
            end = successor.valid_from if end is None else min(end, successor.valid_from)
        tokens.append((version, version.valid_from, end))
    tokens.sort(key=lambda token: (token[0].valid_from, token[0].version))
    return tokens


def project(tokens: list[Token], at: int) -> Token | None:
    """Select the value that was in force at business instant ``at``.

    Tokens arrive in transaction order, newest first.  A backdated correction can
    leave two windows that touch: the value it displaced keeps the window it had,
    and the restated value covers the earlier part of it.  The value written last
    is the one the ledger believes from the instant it took effect, so the
    newest match wins.
    """
    for token in reversed(tokens):
        start, end = token[1], token[2]
        if start <= at and (end is None or at < end):
            return token
    return None


def _projection_content_same(first: Projection, second: Projection) -> bool:
    """Whether two projections carry the same public data content.

    What counts is what a read reports as data: every attribute's value and
    the valid window it holds.  The version number that supplies the value
    and the transaction instant the row was recorded at are storage
    bookkeeping, so they may differ freely without making the records differ.
    """
    if set(first) != set(second):
        return False
    for name in first:
        left, right = first[name], second[name]
        if not value_same(left[0], right[0]):
            return False
        if (left[2], left[3], left[4]) != (right[2], right[3], right[4]):
            return False
    return True


def _value_document(entry: tuple[Any, int, str, int, int | None, int] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    value, version, operation, start, end, recorded_at = entry
    return {
        "value": value,
        "version": version,
        "operation": operation,
        "valid_from": iso(start),
        "valid_end": None if end is None else iso(end),
        "recorded_at": iso(recorded_at),
    }


def _batch_operation_name(raw: Any) -> str:
    """The idempotency-operation label for a batch request.

    It is derived from the request body rather than the key, so reusing one
    idempotency key on a *different* batch is detected the same way reusing it
    on a single-entity write already is.
    """
    payload = Store.encode(raw)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"batch:{digest}"


@dataclass(frozen=True)
class Viewpoint:
    """One complete read point: a valid instant and a transaction instant.

    Both coordinates are required, unlike the HTTP read parameters, because a
    historical comparison has no sensible "current instant" default to fall
    back on: the caller names the belief state of each side explicitly.  Each
    coordinate accepts the same forms as ``as_of``/``known_at`` on a plain
    read (an RFC 3339 string, integer epoch milliseconds, or float epoch
    seconds) or an aware :class:`datetime.datetime`.
    """

    as_of: Any
    known_at: Any


def _viewpoint_coordinates(viewpoint: Any, side: str) -> tuple[int, int]:
    """Validate one viewpoint and normalise both coordinates to milliseconds.

    A missing coordinate, or a value the shared instant parser rejects, is a
    :class:`ValueError`: the comparison entry point speaks the caller's
    Python-level error contract rather than the HTTP ``ValidationError``
    envelope.  ``None`` is rejected explicitly before the instant parser sees
    it so that "the caller did not name a coordinate" reports as such.
    """
    if not isinstance(viewpoint, Viewpoint):
        raise ValueError(f"{side} viewpoint must be a Viewpoint")
    if viewpoint.as_of is None:
        raise ValueError(f"{side} viewpoint requires an as_of (valid time) instant")
    if viewpoint.known_at is None:
        raise ValueError(f"{side} viewpoint requires a known_at (transaction time) instant")
    try:
        as_of = instant(viewpoint.as_of, f"{side} as_of")
        known_at = instant(viewpoint.known_at, f"{side} known_at")
    except ValidationError as error:
        raise ValueError(str(error)) from error
    return as_of, known_at


def _record_identifiers(record_ids: Any) -> list[tuple[str, str]] | None:
    """Validate the optional logical-record filter.

    The filter is an iterable collection of ``(type, id)`` pairs; ``None``
    means every logical record in the store.  Something that is not iterable
    at all, or that looks like one identifier rather than a collection of
    them (a string or bytes), is a :class:`TypeError`, as is any element that
    does not satisfy the existing type/id identifier constraints.  Duplicate
    identifiers are dropped here, so each logical record is compared exactly
    once.
    """
    if record_ids is None:
        return None
    if isinstance(record_ids, (str, bytes)) or not isinstance(record_ids, Iterable):
        raise TypeError("record_ids must be an iterable collection of (type, id) pairs")
    unique: set[tuple[str, str]] = set()
    for element in record_ids:
        if (
            not isinstance(element, tuple)
            or len(element) != 2
            or not all(isinstance(part, str) for part in element)
        ):
            raise TypeError("each record id must be a (type, id) pair of strings")
        entity_type, entity_id = element
        try:
            identifier(entity_type, "entity type")
            identifier(entity_id, "entity id")
        except ValidationError as error:
            raise TypeError(str(error)) from error
        unique.add((entity_type, entity_id))
    return sorted(unique)


def _projection_record_same(first: Projection, second: Projection) -> bool:
    """Whether two visible records are the same public record.

    Every observable feature of a read counts: the set of attributes in
    effect, each attribute's business value, and the full public version
    metadata — version number, operation, the window as the reader sees it,
    and the transaction instant the version was recorded at.  A correction,
    an interval split, or a retraction therefore registers as a change
    whenever it moves what either side ultimately observes, even when the
    business value happens to be the same; intermediate versions that neither
    viewpoint sees never enter the comparison.
    """
    if set(first) != set(second):
        return False
    for name in first:
        left, right = first[name], second[name]
        # Values go through the store's own equality so a boolean and a number
        # stay distinct (Python itself would equate ``true`` and ``1``); the
        # remaining tuple fields are exactly the public version metadata.
        if not value_same(left[0], right[0]):
            return False
        if left[1:] != right[1:]:
            return False
    return True


class TimeVault:
    """Bitemporal entity store: valid time on one axis, transaction time on the other."""

    def __init__(self, database: str, clock: Callable[[], Any] | None = None):
        self.store = Store(database, clock)

    # -- writes -------------------------------------------------------------

    def create_entity(self, entity_type: str, raw: Any, key: str | None) -> dict[str, Any]:
        moment = self.store.now()
        entity = Entity.parse(entity_type, raw)
        if "valid_from" in raw:
            valid_from = instant(raw["valid_from"], "valid_from")
            if valid_from > moment:
                raise ValidationError("valid_from must not be in the future")
        else:
            valid_from = moment

        def action(recorded_at: int) -> dict[str, Any]:
            return self._create_core(
                entity.type, entity.id, entity.attributes, valid_from, recorded_at
            )

        return self._idempotent(key, f"create:{entity.type}/{entity.id}", action, moment)

    def apply_correction(
        self, entity_type: str, entity_id: str, raw: Any, key: str | None
    ) -> dict[str, Any]:
        now = self.store.now()
        correction = Correction.parse(raw, now)

        def action(recorded_at: int) -> dict[str, Any]:
            return self._correct_core(entity_type, entity_id, correction, recorded_at)

        return self._idempotent(key, f"correct:{entity_type}/{entity_id}", action, now)

    def run_batch(self, raw: Any, key: str | None) -> dict[str, Any]:
        """Atomically apply an ordered list of creates and corrections.

        Every item shares one transaction instant, so the whole batch is one
        point on the transaction axis: later items see earlier items' effects,
        but no outside reader ever sees a partially applied batch.  Any invalid
        or conflicting item aborts the transaction and nothing is stored.
        """
        moment = self.store.now()
        operations = parse_batch(raw, moment)
        # Entities this very batch will create, so a correction whose creation
        # sits later in the array can be told apart from one that names an
        # entity nobody will ever create: the first is an ordering conflict,
        # the second is simply not found.
        created_here = {
            (item.entity_type, item.entity_id)
            for item in operations
            if isinstance(item, BatchCreate)
        }

        def action(recorded_at: int) -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            seen_creates: set[tuple[str, str]] = set()
            for index, item in enumerate(operations, start=1):
                target = (item.entity_type, item.entity_id)
                try:
                    if isinstance(item, BatchCreate):
                        start = item.valid_from if item.valid_from is not None else recorded_at
                        document = self._create_core(
                            item.entity_type,
                            item.entity_id,
                            item.attributes,
                            start,
                            recorded_at,
                        )
                        seen_creates.add(target)
                        document["operation"] = "create"
                    elif isinstance(item, BatchCorrect):
                        try:
                            document = self._correct_core(
                                item.entity_type,
                                item.entity_id,
                                item.correction,
                                recorded_at,
                            )
                        except NotFoundError:
                            if target in created_here and target not in seen_creates:
                                raise ConflictError(
                                    f"entity {item.entity_type}/{item.entity_id} is "
                                    "corrected before it is created"
                                )
                            raise
                        document["operation"] = "correct"
                    else:  # pragma: no cover - parse_batch only yields the two kinds
                        raise ValidationError("unknown batch operation")
                except TimeVaultError as error:
                    message = str(error)
                    if not message.startswith("operation "):
                        raise type(error)(f"operation {index}: {message}") from error
                    raise
                results.append(document)
            return {"recorded_at": iso(recorded_at), "results": results}

        return self._idempotent(key, _batch_operation_name(raw), action, moment)

    # -- schemas --------------------------------------------------------------

    def put_schema(self, entity_type: str, raw: Any, key: str | None) -> dict[str, Any]:
        """Append one schema version for an entity type.

        The new version takes effect at its ``effective_from`` and holds until
        the next registered schema start; it never overwrites an earlier
        version.  Before anything is stored, every entity of the type is
        checked over exactly that interval: one non-empty projection with an
        undeclared attribute or a mistyped value rejects the whole submission
        with a conflict and no version is appended.
        """
        moment = self.store.now()
        schema = Schema.parse(entity_type, raw, moment)

        def action(recorded_at: int) -> dict[str, Any]:
            return self._schema_core(schema, recorded_at)

        return self._idempotent(key, f"schema:{schema.entity_type}", action, moment)

    def get_schema(
        self, entity_type: str, as_of: Any = None, known_at: Any = None
    ) -> dict[str, Any]:
        """The schema version in effect and known at the given coordinates.

        Both coordinates default to the current instant.  A version is known
        only from its own ``recorded_at`` onwards, so a version submitted
        later at the same ``effective_from`` supersedes the earlier one only
        for readers past its own submission.  Coordinates no version is
        visible at are a ``not_found``.
        """
        moment = self.store.now()
        as_of = moment if as_of is None else instant(as_of, "as_of")
        known_at = moment if known_at is None else instant(known_at, "known_at")
        version = self._schema_at(entity_type, as_of, known_at)
        if version is None:
            raise NotFoundError(
                f"no schema for {entity_type} is in effect and known at that instant"
            )
        return version.as_dict()

    def _schema_core(self, schema: Schema, recorded_at: int) -> dict[str, Any]:
        """Validate and append one schema version at a fixed instant."""
        existing = self._schema_versions(schema.entity_type)
        later = [v.effective_from for v in existing if v.effective_from > schema.effective_from]
        end = min(later) if later else INFINITY
        declared = dict(schema.attributes)
        for _, entity_id, _ in self.store.entity_keys(schema.entity_type):
            for _, _, projection in self._entity_segments(
                schema.entity_type, entity_id, schema.effective_from, end, recorded_at
            ):
                self._check_projection(
                    declared,
                    projection,
                    ConflictError,
                    f"entity {schema.entity_type}/{entity_id}: ",
                )
        version = self.store.next_schema_version(schema.entity_type)
        self.store.insert_schema(
            schema.entity_type, version, schema.effective_from, recorded_at, declared
        )
        return SchemaVersion(
            schema.entity_type, version, schema.effective_from, recorded_at, declared
        ).as_dict()

    def _schema_versions(self, entity_type: str) -> list[SchemaVersion]:
        return [
            SchemaVersion.from_row(entity_type, row)
            for row in self.store.schemas_for_type(entity_type)
        ]

    def _schema_at(
        self, entity_type: str, as_of: int, known_at: int
    ) -> SchemaVersion | None:
        """The version in effect at ``as_of`` among those known at ``known_at``."""
        chosen: SchemaVersion | None = None
        for version in self._schema_versions(entity_type):
            if version.effective_from > as_of or version.recorded_at > known_at:
                continue
            if chosen is None or (version.effective_from, version.version) > (
                chosen.effective_from,
                chosen.version,
            ):
                chosen = version
        return chosen

    def _enforce_schemas(self, entity_type: str, entity_id: str, recorded_at: int) -> None:
        """Check an entity's resulting history against every known schema interval.

        Runs inside the write's transaction, after the write's own rows are
        placed: every segment of the entity's history that a schema known at
        this transaction instant covers must satisfy it, so a fact window
        spanning several registered schema intervals is checked against each
        of them.  A type with no registered schema keeps the free attribute
        semantics.  A violation raises before the transaction commits, so a
        rejected write leaves no trace.
        """
        schemas = [
            version
            for version in self._schema_versions(entity_type)
            if version.recorded_at <= recorded_at
        ]
        if not schemas:
            return
        start = min(version.effective_from for version in schemas)
        segments = self._entity_segments(entity_type, entity_id, start, INFINITY, recorded_at)
        for seg_start, seg_end, projection in segments:
            # A projection segment can straddle several schema intervals; cut
            # it at every schema start inside it so each part is checked
            # against the contract that actually holds there.
            cuts = [seg_start]
            cuts.extend(
                version.effective_from
                for version in schemas
                if seg_start < version.effective_from < seg_end
            )
            cuts.append(seg_end)
            for index in range(len(cuts) - 1):
                schema = self._schema_covering(schemas, cuts[index])
                if schema is None:
                    continue
                self._check_projection(
                    schema.attributes,
                    projection,
                    ValidationError,
                    f"entity {entity_type}/{entity_id}: ",
                )

    @staticmethod
    def _schema_covering(
        schemas: list[SchemaVersion], at: int
    ) -> SchemaVersion | None:
        """The schema in effect at one valid instant, or ``None``."""
        chosen: SchemaVersion | None = None
        for version in schemas:
            if version.effective_from > at:
                continue
            if chosen is None or (version.effective_from, version.version) > (
                chosen.effective_from,
                chosen.version,
            ):
                chosen = version
        return chosen

    @staticmethod
    def _check_projection(
        declared: dict[str, str],
        projection: Projection,
        error: type[TimeVaultError],
        context: str,
    ) -> None:
        """Require every projected attribute to be declared and exactly typed.

        A projection only ever holds assertions, so a retraction — which
        carries a null value — is never type-checked here.  An asserted null,
        on the other hand, has the type ``null`` and must be declared as such.
        """
        for name, entry in sorted(projection.items()):
            if name not in declared:
                raise error(f"{context}attribute {name} is not declared in the schema")
            expected = declared[name]
            actual = value_type(entry[0])
            if actual != expected:
                raise error(
                    f"{context}attribute {name} must be of type {expected}, not {actual}"
                )

    # -- reads --------------------------------------------------------------

    def entity_as_of(
        self,
        entity_type: str,
        entity_id: str,
        as_of: Any = None,
        known_at: Any = None,
    ) -> dict[str, Any]:
        """Project the entity onto a business instant and a knowledge instant.

        Both axes default to the current instant, so a plain GET returns the
        entity as it is believed to be right now.  Each argument accepts an RFC
        3339 string or an integer count of epoch milliseconds.
        """
        moment = self.store.now()
        return self._read(
            entity_type,
            entity_id,
            moment if as_of is None else instant(as_of, "as_of"),
            moment if known_at is None else instant(known_at, "known_at"),
        )

    def history(
        self, entity_type: str, entity_id: str, known_at: Any = None
    ) -> dict[str, Any]:
        row = self.store.entity_row(entity_type, entity_id)
        if row is None:
            raise NotFoundError(f"entity {entity_type}/{entity_id} does not exist")
        if known_at is None:
            known_at = self.store.now()
        else:
            known_at = instant(known_at, "known_at")
        grouped = self._grouped_versions(entity_type, entity_id)
        attributes = []
        for name in sorted(grouped):
            tokens = complete_windows(grouped[name], known_at)
            attributes.append(
                {
                    "attribute": name,
                    "versions": [
                        version.as_dict(valid_end) for version, _, valid_end in tokens
                    ],
                }
            )
        return {
            "type": entity_type,
            "id": entity_id,
            "created_at": iso(int(row["created_at"])),
            "known_at": iso(known_at),
            "attributes": attributes,
        }

    def diff(
        self,
        entity_type: str,
        entity_id: str,
        start: Any,
        end: Any,
        known_at: Any = None,
        names: list[str] | None = None,
    ) -> dict[str, Any]:
        start = instant(start, "from")
        end = instant(end, "to")
        known_at = self.store.now() if known_at is None else instant(known_at, "known_at")
        if end <= start:
            raise ValidationError("to must be strictly later than from")
        if names is not None and len(set(names)) != len(names):
            raise ValidationError("attribute may not be repeated")
        left = self._projection(entity_type, entity_id, start, known_at)
        right = self._projection(entity_type, entity_id, end, known_at)
        selected = sorted(set(left) | set(right))
        if names is not None:
            wanted = set(names)
            unknown = sorted(wanted - set(self._all_attributes(entity_type, entity_id)))
            if unknown:
                raise ValidationError(f"unknown attribute(s): {', '.join(unknown)}")
            selected = [name for name in selected if name in wanted]

        changes = []
        for name in selected:
            before = left.get(name)
            after = right.get(name)
            if before is None and after is None:
                continue
            if before is not None and after is not None and value_same(before[0], after[0]):
                # The value carried across the boundary; only the version that
                # supplies it changed, which is not a business-time difference.
                continue
            if before is None:
                kind = "added"
            elif after is None:
                kind = "removed"
            else:
                kind = "changed"
            changes.append(
                {
                    "attribute": name,
                    "change": kind,
                    "before": _value_document(before),
                    "after": _value_document(after),
                }
            )
        return {
            "type": entity_type,
            "id": entity_id,
            "from": iso(start),
            "to": iso(end),
            "known_at": iso(known_at),
            "changes": changes,
        }

    def timeline(
        self,
        entity_type: str,
        entity_id: str,
        start: Any,
        end: Any,
        known_at: Any = None,
    ) -> dict[str, Any]:
        """Walk every state one entity held across a valid-time interval.

        The interval ``[from, to)`` is cut into the maximal half-open segments
        during which the same set of versions supplies the projection: every
        instant inside one segment reads the same attributes from the same
        versions.  A boundary lands wherever any visible attribute starts,
        ends, is corrected, or is withdrawn — a correction that restates the
        same scalar value still splits the interval, because the version
        supplying the value changes — while several attributes changing at
        one instant share a single boundary.  Segment bounds are clipped to
        the query interval, segments never overlap, and a stretch where no
        attribute is in effect at all is a gap: it produces no entry.

        Everything is interpreted under one ``known_at`` (the current instant
        unless given): a correction recorded after it, and any window trim
        that correction carried, does not exist for this walk, so repeating
        the query over the same committed data always yields the same
        document.  The query is read-only: it appends no versions, records no
        truncations, and moves no transaction time.
        """
        start = instant(start, "from")
        end = instant(end, "to")
        known_at = self.store.now() if known_at is None else instant(known_at, "known_at")
        if end <= start:
            raise ValidationError("to must be strictly later than from")
        # One lock acquisition spans the whole walk, so every window is read
        # against the same committed ledger even if a write lands concurrently.
        with self.store.lock:
            row = self.store.entity_row(entity_type, entity_id)
            if row is None:
                raise NotFoundError(f"entity {entity_type}/{entity_id} does not exist")
            if int(row["created_at"]) > known_at:
                raise NotFoundError(
                    f"entity {entity_type}/{entity_id} was not recorded yet at that transaction time"
                )
            grouped = self._grouped_versions(entity_type, entity_id)
            windows = {
                name: known_windows(versions, known_at) for name, versions in grouped.items()
            }

        # Every window edge strictly inside the interval is a cut candidate;
        # between two neighbouring cuts no window starts or ends, so the
        # projection sampled at a segment's start holds for the whole segment.
        spans = self._window_segments(windows, start, end)

        return {
            "type": entity_type,
            "id": entity_id,
            "from": iso(start),
            "to": iso(end),
            "known_at": iso(known_at),
            "segments": [
                {
                    "valid_from": iso(seg_start),
                    "valid_end": iso(seg_end),
                    "attributes": self._projection_attributes(projection),
                }
                for seg_start, seg_end, projection in spans
            ],
        }

    def snapshot_diff(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        first_as_of: Any = None,
        first_known_at: Any = None,
        second_as_of: Any = None,
        second_known_at: Any = None,
    ) -> dict[str, Any]:
        """Compare the records a scope shows at two bitemporal facets.

        Each facet is one ``(as_of, known_at)`` pair, evaluated under exactly
        the visibility rules of :meth:`entity_as_of`: a correction recorded
        after a facet's ``known_at`` — and any trim it carried — does not
        exist for that facet.  Every coordinate defaults to the current
        instant and accepts the same forms as the single-entity read.

        The scope is the entity or collection the caller limits the
        comparison to: a ``type`` and ``id`` name one entity, a ``type``
        alone names every entity of that type, and no scope at all spans the
        whole store.  Entities are partitioned into ``added`` (visible only
        at the second facet), ``removed`` (visible only at the first), and
        ``changed`` (visible at both but with different public data content);
        an entity whose record is the same at both facets is not part of the
        result.  Content is what a read reports as data — attribute values
        and their valid windows — so a difference only in the version number
        that supplies a value or in the transaction instant it was recorded
        at is not a change.  Each category is ordered by the stable
        ``(type, id)`` identifier, so repeating the query over the same data
        always yields the same document, and an empty store, an empty scope,
        or two identical facets simply yields three empty categories.  The
        query is read-only: it appends no versions and moves no transaction
        time.
        """
        moment = self.store.now()
        if entity_type is not None:
            identifier(entity_type, "entity type")
        if entity_id is not None:
            if entity_type is None:
                raise ValidationError("scoping by id requires a type as well")
            identifier(entity_id, "entity id")
        first = (
            moment if first_as_of is None else instant(first_as_of, "first_as_of"),
            moment if first_known_at is None else instant(first_known_at, "first_known_at"),
        )
        second = (
            moment if second_as_of is None else instant(second_as_of, "second_as_of"),
            moment if second_known_at is None else instant(second_known_at, "second_known_at"),
        )
        # Hold the lock across both snapshots so a write cannot commit between
        # them: the two facets are always evaluated against one ledger, never
        # against two halves of a commit in flight.
        with self.store.lock:
            before = self._snapshot(entity_type, entity_id, *first)
            after = self._snapshot(entity_type, entity_id, *second)
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        for type_name, entity in sorted(set(before) | set(after)):
            key = (type_name, entity)
            earlier = before.get(key)
            later = after.get(key)
            if earlier is None:
                added.append({"type": type_name, "id": entity, "record": later[1]})
            elif later is None:
                removed.append({"type": type_name, "id": entity, "record": earlier[1]})
            elif not _projection_content_same(earlier[0], later[0]):
                changed.append(
                    {"type": type_name, "id": entity, "before": earlier[1], "after": later[1]}
                )
        return {
            "first": {"as_of": iso(first[0]), "known_at": iso(first[1])},
            "second": {"as_of": iso(second[0]), "known_at": iso(second[1])},
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def compare_views(
        self,
        left: Viewpoint,
        right: Viewpoint,
        record_ids: Iterable[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Compare the visible records at two complete bitemporal viewpoints.

        Each viewpoint is one :class:`Viewpoint` carrying both an ``as_of``
        (valid time) and a ``known_at`` (transaction time); both coordinates
        are required and must satisfy the same instant constraints as a
        plain read's parameters.  ``record_ids`` optionally narrows the
        comparison to an iterable collection of ``(type, id)`` logical
        record identifiers; when it is omitted every logical record in the
        store is compared.

        Each side is evaluated under exactly the visibility rules of a plain
        read: only versions in effect at that side's valid time, already
        knowable at its transaction time, and not withdrawn then are visible,
        so a later correction never leaks into the earlier view.  The result
        is one flat, stable list ordered by ``(type, id)``: ``added`` entries
        are visible only on the right, ``removed`` only on the left, and
        ``changed`` entries are visible on both but differ in business value
        or in any public version metadata.  Entries whose content and
        observable version metadata are identical on both sides are omitted;
        the missing side of an addition or removal is reported as ``null``.
        Repeated identifiers are compared once, and identifiers that name no
        record produce nothing.

        Both sides are read inside one acquisition of the store lock, so they
        always observe the same committed state — a write that commits
        concurrently cannot land between the two reads.  The comparison is
        strictly read-only and never mutates the caller's identifier
        collection.
        """
        left_coords = _viewpoint_coordinates(left, "left")
        right_coords = _viewpoint_coordinates(right, "right")
        wanted = _record_identifiers(record_ids)

        with self.store.lock:
            # One lock acquisition spans both viewpoints: every other writer
            # takes this same lock across its whole transaction, so the two
            # sides can never straddle a commit boundary.
            if wanted is None:
                keys = [(type_name, entity) for type_name, entity, _ in self.store.entity_keys()]
            else:
                keys = wanted
            before: dict[tuple[str, str], tuple[Projection, dict[str, Any]]] = {}
            after: dict[tuple[str, str], tuple[Projection, dict[str, Any]]] = {}
            for type_name, entity in keys:
                left_record = self._visible_record(type_name, entity, *left_coords)
                if left_record is not None:
                    before[(type_name, entity)] = left_record
                right_record = self._visible_record(type_name, entity, *right_coords)
                if right_record is not None:
                    after[(type_name, entity)] = right_record

        differences: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            type_name, entity = key
            earlier = before.get(key)
            later = after.get(key)
            if earlier is None:
                kind = DIFF_ADDED
                left_document: dict[str, Any] | None = None
                right_document = later[1]  # type: ignore[union-attr]
            elif later is None:
                kind = DIFF_REMOVED
                left_document = earlier[1]
                right_document = None
            elif not _projection_record_same(earlier[0], later[0]):
                kind = DIFF_CHANGED
                left_document = earlier[1]
                right_document = later[1]
            else:
                # The same observable record on both sides: no difference.
                continue
            differences.append(
                {
                    "type": type_name,
                    "id": entity,
                    "difference": kind,
                    "left": left_document,
                    "right": right_document,
                }
            )
        return {
            "left": {"as_of": iso(left_coords[0]), "known_at": iso(left_coords[1])},
            "right": {"as_of": iso(right_coords[0]), "known_at": iso(right_coords[1])},
            "differences": differences,
        }

    def audit(
        self,
        *,
        entity_type: Any = None,
        entity_id: Any = None,
        attribute: Any = None,
        action: Any = None,
        recorded_from: Any = None,
        recorded_to: Any = None,
        limit: Any = None,
        cursor: Any = None,
    ) -> dict[str, Any]:
        """Trace committed ledger changes back to their sources.

        Every committed version append and every window truncation is one
        immutable audit item.  The query is read-only: it appends nothing and
        moves no transaction time.  Items are ordered by ``recorded_at``
        ascending, and the event id breaks ties inside one millisecond with a
        value that is stable and never reused, so the order is identical after
        a restart.

        The first response pins the result set: its cursor carries the
        filters and the sequence upper bound assigned at that moment, so
        later concurrent writes never enter later pages and pagination
        neither repeats nor skips an item.  A request carrying a cursor may
        not repeat any filter apart from ``limit``.
        """
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ValidationError("cursor is unknown or malformed")
            pinned_filters, upper_seq, after = _decode_cursor(cursor)
            # With a cursor every filter but limit is fixed by the first page.
            repeated = {
                "type": entity_type,
                "id": entity_id,
                "attribute": attribute,
                "action": action,
                "recorded_from": recorded_from,
                "recorded_to": recorded_to,
            }
            given = sorted(name for name, value in repeated.items() if value is not None)
            if given:
                raise ValidationError(
                    f"filter parameter(s) {', '.join(given)} cannot be combined with cursor"
                )
            filters = pinned_filters
        else:
            filters = self._parse_audit_filters(
                entity_type, entity_id, attribute, action, recorded_from, recorded_to
            )
            after = None
            # Pin the result set to what has committed so far.  A commit that
            # lands after this reading but before the page is read gets a
            # higher sequence and is excluded by the page's ``seq <= upper``
            # bound, so the two reads need not share one lock acquisition.
            upper_seq = self.store.max_audit_seq()

        page_limit = self._parse_audit_limit(limit)
        if after is not None and not self.store.audit_key_exists(after[0], after[1], upper_seq):
            # The token's position does not exist in this ledger (it was
            # minted against another database), so treat it as unknown.
            raise ValidationError("cursor is unknown or malformed")
        rows = self.store.audit_page(
            after_recorded_at=None if after is None else after[0],
            after_event_id=None if after is None else after[1],
            upper_seq=upper_seq,
            limit=page_limit,
            action=filters.action,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            attribute=filters.attribute,
            recorded_from=filters.recorded_from,
            recorded_to=filters.recorded_to,
        )
        has_more = len(rows) > page_limit
        page = rows[:page_limit]
        items = [self._audit_item(row) for row in page]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                filters, upper_seq, (int(last["recorded_at"]), str(last["event_id"]))
            )
        return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _parse_audit_limit(raw: Any) -> int:
        if raw is None:
            return DEFAULT_AUDIT_LIMIT
        if isinstance(raw, bool) or isinstance(raw, float):
            raise ValidationError("limit must be an integer between 1 and 500")
        if isinstance(raw, int):
            value = raw
        else:
            try:
                text = str(raw).strip()
                value = int(text)
            except (TypeError, ValueError):
                raise ValidationError("limit must be an integer between 1 and 500") from None
            else:
                if str(value) != text:
                    raise ValidationError("limit must be an integer between 1 and 500")
        if value < 1 or value > MAX_AUDIT_LIMIT:
            raise ValidationError("limit must be between 1 and 500")
        return value

    def _parse_audit_filters(
        self,
        entity_type: Any,
        entity_id: Any,
        attribute: Any,
        action: Any,
        recorded_from: Any,
        recorded_to: Any,
    ) -> AuditFilters:
        if entity_id is not None and entity_type is None:
            raise ValidationError("scoping by id requires a type as well")
        if entity_type is not None:
            entity_type = identifier(entity_type, "entity type")
        if entity_id is not None:
            entity_id = identifier(entity_id, "entity id")
        if attribute is not None:
            attribute = attribute_name(attribute)
        if action is not None:
            if action not in _AUDIT_ACTIONS:
                raise ValidationError(
                    "action must be version_appended or window_truncated"
                )
        start = None if recorded_from is None else instant(recorded_from, "recorded_from")
        end = None if recorded_to is None else instant(recorded_to, "recorded_to")
        if start is not None and end is not None and start >= end:
            raise ValidationError("recorded_from must be strictly earlier than recorded_to")
        return AuditFilters(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            attribute=attribute,
            recorded_from=start,
            recorded_to=end,
        )

    @staticmethod
    def _audit_item(row: Any) -> dict[str, Any]:
        """Render one committed ledger event as its public audit document.

        The fields describing the value at commit time are read off the audit
        row itself, never off the version's current state, so a correction that
        later trims the window cannot rewrite an earlier audit item: the
        append reports the declared end it committed with, and the truncation
        reports the bound learned at its own transaction instant.
        """
        recorded_at = int(row["recorded_at"])
        item: dict[str, Any] = {
            "event_id": str(row["event_id"]),
            "recorded_at": iso(recorded_at),
            "type": str(row["type"]),
            "id": str(row["id"]),
            "attribute": str(row["attribute"]),
            "version": int(row["version"]),
            "action": str(row["action"]),
        }
        if str(row["action"]) == ACTION_VERSION_APPENDED:
            item["operation"] = str(row["operation"])
            item["value"] = None if row["value"] is None else Store.decode(row["value"])
            item["valid_from"] = iso(int(row["valid_from"]))
            declared = row["declared_end"]
            item["declared_end"] = None if declared is None else iso(int(declared))
        else:
            item["valid_end"] = iso(int(row["valid_end"]))
        return item

    # -- internals ----------------------------------------------------------

    def _create_core(
        self,
        entity_type: str,
        entity_id: str,
        attributes: tuple[Any, ...],
        valid_from: int,
        recorded_at: int,
    ) -> dict[str, Any]:
        """Insert one entity and its version-1 assertions at a fixed instant."""
        if self.store.entity_row(entity_type, entity_id) is not None:
            raise ConflictError(f"entity {entity_type}/{entity_id} already exists")
        self.store.insert_entity(entity_type, entity_id, recorded_at)
        for attribute in attributes:
            self.store.insert_version(
                entity_type,
                entity_id,
                attribute.name,
                1,
                "assert",
                attribute.value,
                valid_from,
                None,
                recorded_at,
            )
        self._enforce_schemas(entity_type, entity_id, recorded_at)
        document = self._read(entity_type, entity_id, recorded_at, recorded_at)
        document["created_at"] = iso(recorded_at)
        return document

    def _correct_core(
        self,
        entity_type: str,
        entity_id: str,
        correction: Correction,
        recorded_at: int,
    ) -> dict[str, Any]:
        """Apply one already-parsed correction at a fixed transaction instant.

        Raises :class:`NotFoundError` when the entity does not exist yet and
        :class:`ConflictError` when the correction's facts would land before the
        entity was recorded.
        """
        row = self.store.entity_row(entity_type, entity_id)
        if row is None:
            raise NotFoundError(f"entity {entity_type}/{entity_id} does not exist")
        if int(row["created_at"]) > recorded_at:
            raise ConflictError(
                f"cannot state a fact about {entity_type}/{entity_id} before it existed"
            )
        for fact in correction.facts:
            self._apply_fact(entity_type, entity_id, fact, recorded_at)
        self._enforce_schemas(entity_type, entity_id, recorded_at)
        # ``as_of`` equal to the instant just recorded means "every window
        # that starts when this fact takes effect", which describes the
        # attribute as it stands after the correction rather than the state
        # that the correction replaced.  A write is never a 404, so an
        # entity with nothing in effect right now answers with no attributes.
        return self._read(entity_type, entity_id, recorded_at, recorded_at, required=False)

    def _idempotent(
        self,
        key: str | None,
        operation: str,
        action: Callable[[int], dict[str, Any]],
        moment: int,
    ) -> dict[str, Any]:
        """Run ``action`` once per ``key`` and remember the first response.

        The whole check-and-write happens in one immediate transaction so two
        concurrent deliveries of the same key cannot both run the action.
        """
        if not key:
            raise ValidationError("Idempotency-Key header is required")
        with self.store.transaction() as connection:
            remembered = self.store.remembered_operation(key)
            if remembered is not None:
                if remembered != operation:
                    raise ConflictError(
                        "idempotency key was already used for another operation"
                    )
                row = connection.execute(
                    "SELECT response FROM idempotency WHERE key = ?", (key,)
                ).fetchone()
                return Store.decode(row["response"])
            response = action(moment)
            self.store.remember(key, operation, response)
            return response

    def _apply_fact(self, entity_type: str, entity_id: str, fact: Fact, recorded_at: int) -> None:
        """Place one fact on the ledger.

        The fact lands at ``fact.valid_from``.  It takes effect in the ledger at
        that same instant, or at ``recorded_at`` when the fact is backdated: a
        backdated correction is placed in the past but only displaces the tail
        it supersedes, it does not restate the time between its own effective
        instant and the correction's effective instant.
        """
        effective = max(fact.valid_from or recorded_at, recorded_at)
        # A retraction needs no truncation pass: its own row sits after every
        # version recorded before it and bounds the window in front of it.
        if not fact.deleted:
            self.store.truncate_attribute(
                entity_type, entity_id, fact.attribute, fact.valid_from, effective, recorded_at
            )
        self.store.insert_version(
            entity_type,
            entity_id,
            fact.attribute,
            self.store.next_version(entity_type, entity_id, fact.attribute),
            fact.operation,
            fact.value,
            fact.valid_from,
            # A retraction is stored as an empty window at the instant the
            # attribute ceased to exist.  It is never selectable by as_of; it
            # exists so the window before it has a boundary and so the history
            # records that the attribute was withdrawn rather than corrected.
            fact.valid_from if fact.deleted else fact.valid_end,
            recorded_at,
        )

    def _grouped_versions(self, entity_type: str, entity_id: str) -> dict[str, list[Version]]:
        """Every version of the entity, with the trims it took attached.

        A trim lives in its own append-only row, so one version can carry several
        — one per correction that closed its window.  They are loaded once here
        and attached by version number, which is what lets a projection drop the
        trims the reader cannot know about yet.
        """
        events: dict[str, dict[int, list[Truncation]]] = {}
        for row in self.store.truncations_for_entity(entity_type, entity_id):
            version = int(row["version"])
            by_version = events.setdefault(str(row["attribute"]), {})
            by_version.setdefault(version, []).append(
                Truncation(
                    version=version,
                    recorded_at=int(row["recorded_at"]),
                    valid_end=int(row["valid_end"]),
                )
            )
        grouped: dict[str, list[Version]] = {}
        for row in self.store.versions_for_entity(entity_type, entity_id):
            version = Version.from_row(row)
            attached = events.get(version.attribute, {}).get(version.version, [])
            grouped.setdefault(version.attribute, []).append(
                version.with_truncations(tuple(attached))
            )
        return grouped

    def _all_attributes(self, entity_type: str, entity_id: str) -> set[str]:
        return set(self._grouped_versions(entity_type, entity_id))

    def _entity_segments(
        self, entity_type: str, entity_id: str, start: int, end: int, known_at: int
    ) -> list[list[Any]]:
        """The entity's maximal constant-projection spans over ``[start, end)``."""
        grouped = self._grouped_versions(entity_type, entity_id)
        windows = {
            name: known_windows(versions, known_at) for name, versions in grouped.items()
        }
        return self._window_segments(windows, start, end)

    @staticmethod
    def _window_segments(
        windows: dict[str, list[Token]], start: int, end: int
    ) -> list[list[Any]]:
        """Cut ``[start, end)`` into the maximal spans sharing one projection.

        A boundary lands wherever any visible window starts or ends; between
        two neighbouring boundaries the projection sampled at the span's start
        holds for the whole span.  A stretch with nothing in effect is a gap
        and produces no span, and neighbouring spans with identical
        projections merge into one.
        """
        boundaries: set[int] = set()
        for tokens in windows.values():
            for _, token_start, token_end in tokens:
                if start < token_start < end:
                    boundaries.add(token_start)
                if token_end is not None and start < token_end < end:
                    boundaries.add(token_end)
        points = [start, *sorted(boundaries), end]

        spans: list[list[Any]] = []
        for index in range(len(points) - 1):
            seg_start, seg_end = points[index], points[index + 1]
            projection: Projection = {}
            for name in sorted(windows):
                token = project(windows[name], seg_start)
                if token is None:
                    continue
                version, window_start, window_end = token
                projection[name] = (
                    version.value,
                    version.version,
                    version.operation,
                    window_start,
                    window_end,
                    version.recorded_at,
                )
            if not projection:
                # Nothing in effect here: a gap, not a segment.
                continue
            if spans and spans[-1][1] == seg_start and spans[-1][2] == projection:
                # A window edge nothing observable hinged on (a version no
                # reader can ever select, for instance): the same versions
                # keep supplying the projection, so the segment runs on.
                spans[-1][1] = seg_end
            else:
                spans.append([seg_start, seg_end, projection])
        return spans

    def _read(
        self,
        entity_type: str,
        entity_id: str,
        as_of: int,
        known_at: int,
        required: bool = True,
    ) -> dict[str, Any]:
        projection = self._projection(entity_type, entity_id, as_of, known_at, required)
        return self._projection_document(entity_type, entity_id, as_of, known_at, projection)

    @staticmethod
    def _projection_attributes(projection: Projection) -> dict[str, Any]:
        """Render a projection's attribute map the way every read reports it."""
        return {
            name: {
                "value": entry[0],
                "version": entry[1],
                "operation": entry[2],
                "valid_from": iso(entry[3]),
                "valid_end": None if entry[4] is None else iso(entry[4]),
                "recorded_at": iso(entry[5]),
            }
            for name, entry in sorted(projection.items())
        }

    def _projection_document(
        self,
        entity_type: str,
        entity_id: str,
        as_of: int,
        known_at: int,
        projection: Projection,
    ) -> dict[str, Any]:
        """Render a projection as the public record a read reports."""
        return {
            "type": entity_type,
            "id": entity_id,
            "as_of": iso(as_of),
            "known_at": iso(known_at),
            "attributes": self._projection_attributes(projection),
        }

    def _snapshot(
        self,
        entity_type: str | None,
        entity_id: str | None,
        as_of: int,
        known_at: int,
    ) -> dict[tuple[str, str], tuple[Projection, dict[str, Any]]]:
        """The visible record of every in-scope entity at one facet.

        An entity contributes its record only when the facet can see it:
        recorded no later than the facet's ``known_at`` and holding at least
        one attribute in effect at its ``as_of``.  Anything else — an empty
        store, a scope nothing falls in, an entity the facet predates, an
        entity with nothing in effect — is simply absent, which is what lets
        an empty diff come back as empty categories rather than an error.
        """
        records: dict[tuple[str, str], tuple[Projection, dict[str, Any]]] = {}
        for type_name, entity, created_at in self.store.entity_keys(entity_type, entity_id):
            if created_at > known_at:
                # The facet's transaction instant predates the entity itself.
                continue
            record = self._visible_record(type_name, entity, as_of, known_at)
            if record is not None:
                records[(type_name, entity)] = record
        return records

    def _visible_record(
        self,
        entity_type: str,
        entity_id: str,
        as_of: int,
        known_at: int,
    ) -> tuple[Projection, dict[str, Any]] | None:
        """One entity's record as a facet sees it, or ``None`` when invisible.

        The projection is built by the same ``_projection`` path every other
        read uses, so the visibility rules for unknowable corrections, trims,
        and retractions are reused verbatim rather than reimplemented.
        """
        row = self.store.entity_row(entity_type, entity_id)
        if row is None or int(row["created_at"]) > known_at:
            return None
        projection = self._projection(entity_type, entity_id, as_of, known_at, required=False)
        if not projection:
            return None
        return projection, self._projection_document(
            entity_type, entity_id, as_of, known_at, projection
        )

    def _projection(
        self, entity_type: str, entity_id: str, as_of: int, known_at: int, required: bool = True
    ) -> Projection:
        row = self.store.entity_row(entity_type, entity_id)
        if row is None:
            raise NotFoundError(f"entity {entity_type}/{entity_id} does not exist")
        if int(row["created_at"]) > known_at:
            raise NotFoundError(
                f"entity {entity_type}/{entity_id} was not recorded yet at that transaction time"
            )
        grouped = self._grouped_versions(entity_type, entity_id)
        result: Projection = {}
        for name in sorted(grouped):
            window = project(known_windows(grouped[name], known_at), as_of)
            if window is None:
                continue
            version, start, end = window
            result[name] = (
                version.value,
                version.version,
                version.operation,
                start,
                end,
                version.recorded_at,
            )
        if not result and required:
            raise NotFoundError(
                f"entity {entity_type}/{entity_id} was not in effect at that business instant"
            )
        return result

