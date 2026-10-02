from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import ConflictError, NotFoundError, ValidationError
from .model import Correction, Entity, Fact, INFINITY, instant, iso, value_same
from .store import Store


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
            if self.store.entity_row(entity.type, entity.id) is not None:
                raise ConflictError(f"entity {entity.type}/{entity.id} already exists")
            self.store.insert_entity(entity.type, entity.id, recorded_at)
            for attribute in entity.attributes:
                self.store.insert_version(
                    entity.type,
                    entity.id,
                    attribute.name,
                    1,
                    "assert",
                    attribute.value,
                    valid_from,
                    None,
                    recorded_at,
                )
            document = self._read(entity.type, entity.id, recorded_at, recorded_at)
            document["created_at"] = iso(recorded_at)
            return document

        return self._idempotent(key, f"create:{entity.type}/{entity.id}", action, moment)

    def apply_correction(
        self, entity_type: str, entity_id: str, raw: Any, key: str | None
    ) -> dict[str, Any]:
        now = self.store.now()
        correction = Correction.parse(raw, now)

        def action(recorded_at: int) -> dict[str, Any]:
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

        return self._idempotent(key, f"correct:{entity_type}/{entity_id}", action, now)

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

    # -- internals ----------------------------------------------------------

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

