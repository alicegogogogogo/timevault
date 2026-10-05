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
    attribute_name,
    identifier,
    instant,
    iso,
    parse_batch,
    value_same,
)
from .store import ACTION_VERSION_APPENDED, ACTION_WINDOW_TRUNCATED, Store

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


def _projection_identical(first: Projection, second: Projection) -> bool:
    """Whether two projections agree on content and public version metadata.

    Unlike :func:`_projection_content_same`, which treats the version number
    and the recorded instant as storage bookkeeping, this comparison counts
    everything a read reports about the version that supplies each value —
    its number, operation, valid window, and recorded instant — so a
    correction that merely restates a value with an identical one still
    makes the two views differ.
    """
    if set(first) != set(second):
        return False
    for name in first:
        left, right = first[name], second[name]
        if not value_same(left[0], right[0]):
            return False
        if left[1:] != right[1:]:
            return False
    return True


def _view_id_filter(ids: Any) -> set[str] | None:
    """Validate the optional identifier filter of a view diff.

    The filter must be an iterable of entity identifiers.  It is copied into
    a new set, so the caller's own collection is never mutated and duplicate
    identifiers collapse into one comparison.  Anything else — a
    non-iterable, a bare string, or an element that is not a valid
    identifier — is a ``TypeError`` raised before any record is read, so a
    bad filter can never yield a partial result.
    """
    if ids is None:
        return None
    if isinstance(ids, (str, bytes)) or not isinstance(ids, Iterable):
        raise TypeError("ids must be an iterable of entity identifiers")
    wanted: set[str] = set()
    for element in ids:
        if not isinstance(element, str):
            raise TypeError("ids must contain only entity identifier strings")
        try:
            identifier(element, "id")
        except ValidationError as error:
            raise TypeError(
                f"ids must contain only valid entity identifiers: {error}"
            ) from error
        wanted.add(element)
    return wanted


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

    def view_diff(
        self,
        left_as_of: Any = None,
        left_known_at: Any = None,
        right_as_of: Any = None,
        right_known_at: Any = None,
        ids: Any = None,
    ) -> dict[str, Any]:
        """Compare the records two historical views show over one ledger.

        Each view is one ``(as_of, known_at)`` pair — a valid-time instant
        and a transaction-time instant — and both coordinates of both views
        are required: a missing coordinate, or one that violates the instant
        constraints every other query accepts, is a ``ValueError``.  ``ids``
        is an optional iterable of entity identifiers narrowing the
        comparison; when it is omitted every entity in the store takes part.
        A filter that is not an iterable of identifiers, or that holds an
        element which is not a valid identifier, is a ``TypeError`` raised
        before anything is read, so a bad call never yields a partial
        result.

        Each side is evaluated under exactly the visibility rules of
        :meth:`entity_as_of`: a version shows only when it holds at the
        view's ``as_of``, was recorded no later than its ``known_at``, and
        was not retracted by then, and a correction recorded after a view's
        ``known_at`` — including any trim it carried — does not exist for
        that view.

        The result lists one entry per entity whose visible record differs,
        ordered by the entity identifier (the type breaks ties), so the same
        data and arguments always yield the same document.  ``change`` is
        ``added`` when the record is visible only on the right, ``removed``
        when only on the left, and ``changed`` when both sides see it but
        the business values or the public version metadata — version number,
        operation, valid window, recorded instant — differ; an entity whose
        record, content and observable version information alike, is
        identical on both sides is not part of the result, so two identical
        views produce an empty ``changes`` list.  Each entry carries the
        full public record of both sides under ``left`` and ``right``, with
        the side the record is missing from explicitly ``None``.  Duplicate
        identifiers in the filter are compared once, identifiers no entity
        has produce no entries, and a correction, a window split, or a
        retraction shows up exactly when it changes the version a side
        finally sees — never as an internal intermediate version.

        Both views are read under one lock acquisition, so a concurrent
        write cannot make the two sides observe different commit boundaries,
        and the query itself is strictly read-only: it appends no versions,
        moves no transaction time, and never touches the caller's filter
        collection.
        """
        if left_as_of is None:
            raise ValidationError("left_as_of is required")
        if left_known_at is None:
            raise ValidationError("left_known_at is required")
        if right_as_of is None:
            raise ValidationError("right_as_of is required")
        if right_known_at is None:
            raise ValidationError("right_known_at is required")
        left = (instant(left_as_of, "left_as_of"), instant(left_known_at, "left_known_at"))
        right = (instant(right_as_of, "right_as_of"), instant(right_known_at, "right_known_at"))
        wanted = _view_id_filter(ids)
        # Hold the lock across both views so a write cannot commit between
        # them: the two sides are always evaluated against one committed
        # state of the ledger, never against two halves of a commit in
        # flight.
        with self.store.lock:
            before = self._view_records(wanted, *left)
            after = self._view_records(wanted, *right)
        changes: list[dict[str, Any]] = []
        keys = sorted(set(before) | set(after), key=lambda key: (key[1], key[0]))
        for type_name, entity_id in keys:
            earlier = before.get((type_name, entity_id))
            later = after.get((type_name, entity_id))
            if earlier is None:
                kind = "added"
            elif later is None:
                kind = "removed"
            elif _projection_identical(earlier[0], later[0]):
                continue
            else:
                kind = "changed"
            changes.append(
                {
                    "type": type_name,
                    "id": entity_id,
                    "change": kind,
                    "left": None if earlier is None else earlier[1],
                    "right": None if later is None else later[1],
                }
            )
        return {
            "left": {"as_of": iso(left[0]), "known_at": iso(left[1])},
            "right": {"as_of": iso(right[0]), "known_at": iso(right[1])},
            "changes": changes,
        }

    # The same operation under its alternate name: both views are views over
    # the ledger's history, so ``history_diff`` is kept as a public alias.
    history_diff = view_diff

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

    def _projection_document(
        self,
        entity_type: str,
        entity_id: str,
        as_of: int,
        known_at: int,
        projection: Projection,
    ) -> dict[str, Any]:
        """Render a projection as the public record a read reports."""
        attributes = {
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
        return {
            "type": entity_type,
            "id": entity_id,
            "as_of": iso(as_of),
            "known_at": iso(known_at),
            "attributes": attributes,
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
            projection = self._projection(type_name, entity, as_of, known_at, required=False)
            if not projection:
                continue
            records[(type_name, entity)] = (
                projection,
                self._projection_document(type_name, entity, as_of, known_at, projection),
            )
        return records

    def _view_records(
        self, wanted: set[str] | None, as_of: int, known_at: int
    ) -> dict[tuple[str, str], tuple[Projection, dict[str, Any]]]:
        """The visible record of every in-scope entity at one historical view.

        This mirrors :meth:`_snapshot`, with the scope given as a set of
        entity identifiers rather than a type/id prefix: an entity takes
        part when the filter is absent or names its identifier.  Visibility
        itself is the ordinary read rule — the entity must be recorded no
        later than the view's ``known_at`` and hold at least one attribute
        in effect at its ``as_of``; anything else is simply absent, which is
        what lets an empty diff come back as an empty list rather than an
        error.
        """
        records: dict[tuple[str, str], tuple[Projection, dict[str, Any]]] = {}
        for type_name, entity, created_at in self.store.entity_keys():
            if wanted is not None and entity not in wanted:
                continue
            if created_at > known_at:
                # The view's transaction instant predates the entity itself.
                continue
            projection = self._projection(type_name, entity, as_of, known_at, required=False)
            if not projection:
                continue
            records[(type_name, entity)] = (
                projection,
                self._projection_document(type_name, entity, as_of, known_at, projection),
            )
        return records

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

