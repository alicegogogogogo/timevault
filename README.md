# TimeVault

TimeVault is a small bitemporal entity store. Every attribute value it holds is
stamped on two independent time axes:

- **valid time** — when the fact was true in the business world
  (`valid_from` inclusive, `valid_end` exclusive);
- **transaction time** — when the system came to know the fact (`recorded_at`).

A correction never overwrites history. It appends a new version, truncates the
valid window of the value it supersedes, and leaves that earlier value readable
in the history forever. Reads take one instant on each axis (`as_of` and
`known_at`), which is what makes "what did we believe on Tuesday about last
March?" answerable.

## Requirements

- Python 3.11 or newer
- no third-party runtime dependencies

## Run the service

```bash
PYTHONPATH=src python -m timevault.server --host 127.0.0.1 --port 8080 --database timevault.db
```

The process prints `TimeVault listening on http://127.0.0.1:8080` after it has
bound the port.

## The two axes

| axis | written by | read by | interval |
| --- | --- | --- | --- |
| valid time | `valid_from` / `valid_end` of a fact | `as_of` | `[from, end)` |
| transaction time | the injected clock, one instant per write | `known_at` | `[recorded_at, superseded_at)` here, `[recorded_at, now)` if still current |

Both coordinates of the transaction interval are transaction instants: `recorded_at`
is when the write was recorded and `superseded_at` is when a later write stopped
the version being current. Neither is ever a business instant, so the interval can
never run backwards. `superseded_at` is `null` while the version is still current.

Both axes accept an RFC 3339 instant such as `2024-05-31T00:00:00Z`, an integer
count of milliseconds since the Unix epoch, or a float count of seconds since
the epoch. Internally every coordinate is an integer millisecond count, so
ordering and equality are exact. A `GET` with no query projects at the current
instant on both axes.

`known_at` is a cutoff: a version recorded after it does not exist for that read,
so a window that a later correction truncated still looks open ended at that
earlier transaction time.

## Data model

An entity is a set of named attributes under a `type` and an `id`. Values must
be a string, a number, a boolean, or `null`; objects and arrays are rejected.
The ledger keeps one row per **attribute version**:

| field | axis | meaning |
| --- | --- | --- |
| `version` | — | per-attribute counter, from 1, never reused |
| `operation` | — | `assert` (holds `value`) or `retract` (holds nothing) |
| `value` | — | the asserted value, `null` for a retraction |
| `valid_from` | valid | inclusive start of the valid window |
| `declared_end` | valid | the exclusive end the assertion declared, `null` if it declared none; never moves |
| `valid_end` | valid | exclusive end of the window once every correction so far has applied its trim, `null` while open ended |
| `recorded_at` | transaction | when the write request was recorded |
| `superseded_at` | transaction | when a later write stopped this version being current, `null` while current |

`declared_end` and `valid_end` differ exactly when a correction pulled the window
back: the declared bound is what the row was written with, the effective bound is
where the last trim of that window left it. The two together are what let a
reader before the trim see the open ended window the row actually had. A single
window can be trimmed more than once, by a correction in force and then by a
backdated restatement that lands earlier still; each trim appends its own
`truncations` row, so a reader between two trims sees the end the first one left
and never the earlier end the second one pulled it back to.

Rows are append-only. A correction appends its own row, and the only columns it
ever updates on an older row are `valid_end` and `declared_end` — that update is
the trim. Every trim is also appended to the `truncations` table, one row per
event, stamped with the trim's **transaction instant**:
`(type, id, attribute, version, recorded_at, valid_end)`. A trim is knowledge, so
the truncation carries the instant it was learned; a per-version stamp instead of
one row per event could not describe a window trimmed twice, because the second
trim would overwrite the first one's instant.

### How a write lands

`PUT /entities/{type}/{id}` takes `as_of` (the correction's effective instant,
default *now*) and a non-empty `facts` array. A fact is
`{attribute, value, valid_from?, valid_end?, deleted?}` and is placed at its own
`valid_from`, which defaults to the correction's `as_of`. For the attribute a
fact touches:

1. every version whose `valid_from` is at or after the landing point is
   superseded — it keeps its value and its own `valid_end` bound, but no `as_of`
   selects it, because a newer version took over where it began; the trim is
   recorded with the transaction instant of this write;
2. the version straddling the landing point keeps its value and only has its
   `valid_end` pulled back to the landing point, so the new value replaces it
   from then on;
3. the fact is appended: an `assert` holding the given value and an optional
   declared `valid_end`, or a `retract` with an empty window at the landing
   point when `deleted` is `true`.

A fact at or after the correction's effective instant takes effect immediately,
so the value it displaced stops there. A fact that declares its own earlier
`valid_from` is a **backdated restatement**: it is placed at that instant and
takes over the valid time from there, which cuts the window of the value it
displaced and usually leaves that value holding only the part of its own window
that nothing newer claimed. A restatement never rewrites the interval between its
own instant and the correction's, and the flat result is always a set of
non-overlapping windows: each version holds valid time from its `valid_from`
until the next version (in valid time) begins, its own declared `valid_end`, or
the `valid_end` its trims left it, whichever comes first.

A retraction is a row with an empty valid window at the instant the attribute
ceased to exist. It is never selectable by `as_of`; it exists so the window
before it has a boundary and so the history records that the attribute was
withdrawn rather than corrected. The value it withdrew keeps its own
`valid_end`, so that window still looks open ended for a `known_at` earlier than
the retraction was recorded.

### How a read projects

For one attribute and a reader's `known_at`:

1. discard every version recorded after `known_at`, and every trim recorded
   after it — the reader could not see either;
2. a remaining version holds valid time from its `valid_from` until the earliest
   of: the `valid_from` of the next version the reader can see, its declared
   `valid_end`, and the instant a trim the reader can see pulled its window back;
3. a version whose only truncation was written after `known_at` keeps an open
   ended window for that reader, because at that instant nobody had written the
   bound. No stored `valid_end` leaks backwards past the write that put it there;
4. a retraction row always holds an empty window, and it closes the window in
   front of it only from the instant it was recorded onwards;
5. `as_of` selects the newest version whose half-open window contains it. If no
   attribute is in effect, that attribute is absent from the response, and an
   entity with nothing in effect at that instant is `not_found`.

Step 3 is what makes the upper axis exact.

#### Worked example for step 3

Three instants, one entity, one correction:

| event | transaction instant | what it writes |
| --- | --- | --- |
| W1 | `2026-10-02T06:30:16.984Z` | `tier = gold`, valid from `2024-01-01`, open ended |
| W2 | `2026-10-02T06:30:16.987Z` | `tier = platinum`, effective `2024-03-01`; trims gold to end at `2024-03-01` |

Reading `GET /entities/account/acct-1?as_of=2024-04-01T00:00:00Z` at three
`known_at` values, the answer follows by hand from rule 2 and rule 3:

| `known_at` | versions visible | trims visible | gold's window | window at `2024-04-01` contains it? | answer |
| --- | --- | --- | --- | --- | --- |
| `06:30:16.985Z` | gold | none | `[2024-01-01, ∞)` | yes | `gold` |
| `06:30:16.986Z` | gold | none | `[2024-01-01, ∞)` | yes | `gold` |
| `06:30:16.987Z` | gold, platinum | gold ends `2024-03-01` | `[2024-01-01, 2024-03-01)` | no | `platinum` |

The first two rows are rule 3: W2's trim is recorded at `.987Z`, so at `.985Z`
and `.986Z` the trim does not exist yet and gold still runs to `[2024-01-01, ∞)`.
In the last row the reader is at W2's own instant, so it sees both the trim that
ends gold at `2024-03-01` and the platinum version that takes over there;
`2024-04-01` falls in platinum's window, which contains it.

## HTTP API

All bodies are JSON. Unknown body fields and unknown query parameters are
rejected with `validation_error`. Every state-changing POST and PUT requires an
`Idempotency-Key` header; replaying a key returns the first response and writes
nothing new.

### Health

```http
GET /health
```

```json
{"status":"ok"}
```

### Create an entity

```http
POST /entities/account
Idempotency-Key: create-1

{
  "id": "acct-1",
  "attributes": {"status": "active", "tier": "gold"},
  "valid_from": "2024-01-01T00:00:00Z"
}
```

Returns HTTP 201. `valid_from` defaults to the current instant and must not be in
the future. Every attribute starts at version 1 with an open ended window. The
response is the projection at the current instant plus `created_at` — the same
shape as a read, and a projection carries only the fields a read reports:

```json
{"type": "account", "id": "acct-1",
 "as_of": "2024-05-01T00:00:00.000Z", "known_at": "2024-05-01T00:00:00.000Z",
 "created_at": "2024-05-01T00:00:00.000Z",
 "attributes": {
   "status": {"value": "active", "version": 1, "operation": "assert",
              "valid_from": "2024-01-01T00:00:00.000Z", "valid_end": null,
              "declared_end": null,
              "recorded_at": "2024-05-01T00:00:00.000Z"},
   "tier": {"value": "gold", "version": 1, "operation": "assert",
            "valid_from": "2024-01-01T00:00:00.000Z", "valid_end": null,
            "declared_end": null,
            "recorded_at": "2024-05-01T00:00:00.000Z"}}}
```

Creating the same `type`/`id` again is `conflict` (409).

### Correct an entity

```http
PUT /entities/account/acct-1
Idempotency-Key: fix-1

{
  "as_of": "2024-03-01T00:00:00Z",
  "facts": [
    {"attribute": "tier", "value": "platinum"},
    {"attribute": "region", "value": "emea"},
    {"attribute": "status", "deleted": true}
  ]
}
```

Returns HTTP 200 with the projection at the current instant, so the response
always shows the entity as it is now believed to be; a write is never a 404, even
if the correction leaves nothing in effect right now. Afterwards `tier` holds
`platinum` and `region` exists from 2024-03-01, `status` holds nothing from
2024-03-01, and the earlier `tier` and `status` values remain readable through
`as_of` before 2024-03-01 and through `/history`.

A fact that would create an empty window (for example a `valid_end` at or before
the window start) is a `validation_error`, because a version's window is never
made empty by an ordinary correction.

### Batch import

```http
POST /batch
Idempotency-Key: import-1

{
  "operations": [
    {"operation": "create", "type": "account", "id": "acct-1",
     "attributes": {"status": "active", "tier": "gold"},
     "valid_from": "2024-01-01T00:00:00Z"},
    {"operation": "correct", "type": "account", "id": "acct-1",
     "as_of": "2024-03-01T00:00:00Z",
     "facts": [{"attribute": "tier", "value": "platinum"}]},
    {"operation": "correct", "type": "account", "id": "acct-1",
     "facts": [{"attribute": "region", "value": "emea"}]}
  ]
}
```

Returns HTTP 200. The body must be an object containing only an `operations`
array of between 1 and 1000 items, and the request requires an
`Idempotency-Key` header. Each item is one of:

- `create` — `type`, `id`, `attributes`, and an optional `valid_from`, with the
  same field semantics as `POST /entities/{type}`;
- `correct` — `type`, `id`, optional `as_of`, and `facts`, with the same field
  semantics as `PUT /entities/{type}/{id}`.

Operations apply in the order given, so a later item sees the result of the
earlier ones: the same request may create an entity and then correct it, and it
may append several history segments to an entity that already existed. An item
that corrects an entity the same batch creates **later** is a `conflict`;
correcting an entity nobody creates is `not_found`; a duplicate create (against
an existing entity or an earlier item) is a `conflict`. A batch item that fails
any check reports the one-based index of the item, for example
`operation 2: as_of must not be in the future`. Structural and field problems
are `validation_error`.

The whole batch is one write: it commits at a single transaction instant under
one transaction, so every version and trim it produces carries the same
`recorded_at`, and any failing item aborts the entire request — nothing is
stored and no version numbers are consumed. Different batches submitted
concurrently commit one after another, so version numbers stay continuous and
valid windows never overlap; a reader never sees a partially applied batch.

```json
{"recorded_at": "2024-05-31T00:00:00.000Z",
 "results": [
   {"operation": "create", "type": "account", "id": "acct-1",
    "as_of": "2024-05-31T00:00:00.000Z", "known_at": "2024-05-31T00:00:00.000Z",
    "created_at": "2024-05-31T00:00:00.000Z",
    "attributes": {"tier": {"value": "gold", "version": 1, "operation": "assert",
                            "valid_from": "2024-01-01T00:00:00.000Z",
                            "valid_end": null,
                            "recorded_at": "2024-05-31T00:00:00.000Z"}}},
   {"operation": "correct", "type": "account", "id": "acct-1",
    "as_of": "2024-05-31T00:00:00.000Z", "known_at": "2024-05-31T00:00:00.000Z",
    "attributes": {"tier": {"value": "platinum", "version": 2, "operation": "assert",
                            "valid_from": "2024-03-01T00:00:00.000Z",
                            "valid_end": null,
                            "recorded_at": "2024-05-31T00:00:00.000Z"}}}]}
```

`results` is the same length and order as `operations`. Each result has the
shape the matching single-entity write would return (a `create` result carries
`created_at`, a `correct` result does not), plus an `operation` field naming its
type. The top-level `recorded_at` is the batch's transaction instant in the
same instant format the rest of the API uses. After the batch returns, the
ordinary `GET`, `/history`, and `/diff` entry points read the resulting state
exactly as if each operation had been written individually at that instant.

Replaying a key returns the first batch's stored 200 response and writes
nothing, so replayed batches add no versions; reusing a key for a different
batch request is a `conflict`.

### Read an entity

```http
GET /entities/account/acct-1?as_of=2024-02-01T00:00:00Z
GET /entities/account/acct-1?as_of=2024-02-01T00:00:00Z&known_at=2024-05-15T00:00:00Z
GET /entities/account/acct-1
```

```json
{"type": "account", "id": "acct-1",
 "as_of": "2024-02-01T00:00:00.000Z", "known_at": "2024-05-31T00:00:00.000Z",
 "attributes": {
   "status": {"value": "active", "version": 1, "operation": "assert",
              "valid_from": "2024-01-01T00:00:00.000Z",
              "valid_end": "2024-03-01T00:00:00.000Z",
              "recorded_at": "2024-05-01T00:00:00.000Z"},
   "tier": {"value": "gold", "version": 1, "operation": "assert",
            "valid_from": "2024-01-01T00:00:00.000Z",
            "valid_end": "2024-03-01T00:00:00.000Z",
            "recorded_at": "2024-05-01T00:00:00.000Z"}}}
```

Both parameters default to the current instant. `valid_end` in a response is the
window as it appears to that reader, so it reflects the trims recorded up to
`known_at` and nothing later. An instant before the entity's first valid
time, one where no attribute is in effect, or a `known_at` earlier than the
entity was recorded, is `not_found`.

### Read the history

```http
GET /entities/account/acct-1/history
GET /entities/account/acct-1/history?known_at=2024-05-15T00:00:00Z
```

Every version of every attribute, ordered by valid time, with the window as it
stands in the ledger for that reader:

```json
{"type": "account", "id": "acct-1",
 "created_at": "2024-05-01T00:00:00.000Z", "known_at": "2024-05-31T00:00:00.000Z",
 "attributes": [{"attribute": "tier", "versions": [
   {"attribute": "tier", "version": 1, "operation": "assert", "value": "gold",
    "valid_from": "2024-01-01T00:00:00.000Z", "valid_from_ms": 1704067200000,
    "valid_end": "2024-03-01T00:00:00.000Z", "valid_end_ms": 1709251200000,
    "declared_end": null,
    "recorded_at": "2024-05-01T00:00:00.000Z",
    "superseded_at": "2024-05-31T00:00:00.000Z"},
   {"attribute": "tier", "version": 2, "operation": "assert", "value": "platinum",
    "valid_from": "2024-03-01T00:00:00.000Z", "valid_from_ms": 1709251200000,
    "valid_end": null, "valid_end_ms": null, "declared_end": null,
    "recorded_at": "2024-05-31T00:00:00.000Z", "superseded_at": null}]}]}
```

`superseded_at` is the transaction instant a later write stopped the version
being current, and it is `null` while the version is still current. It is not the
business instant the window ended at: that is `valid_end`, and for version 1 the
two are different instants, `2024-03-01` against `2024-05-31`. Gold was written
on `2024-05-01` (`recorded_at`) and superseded on `2024-05-31` (`superseded_at`),
so its transaction interval is `[2024-05-01, 2024-05-31)` — it can never be
inverted, whatever the effective instant of the correction was.

`declared_end` is the bound the assertion itself declared, `null` here because
gold declared none; `valid_end` is where the correction trimmed it. `recorded_at`
filters out versions recorded after the reader's instant, and the same cutoff
hides the trims recorded after it, so a correction recorded after `known_at`
disappears from that earlier history view and the window it trimmed reads as open
ended there.

### Diff two business instants

```http
GET /diff?type=account&id=acct-1&from=2024-02-01T00:00:00Z&to=2024-04-01T00:00:00Z
GET /diff?type=account&id=acct-1&from=...&to=...&known_at=2024-05-15T00:00:00Z&attribute=tier
```

Compares the two projections under one knowledge cutoff (the current instant
unless `known_at` is given) and reports one entry per attribute whose value
differs:

```json
{"type": "account", "id": "acct-1",
 "from": "2024-02-01T00:00:00.000Z", "to": "2024-04-01T00:00:00.000Z",
 "known_at": "2024-05-31T00:00:00.000Z",
 "changes": [
   {"attribute": "region", "change": "added", "before": null,
    "after": {"value": "emea", "version": 1, "operation": "assert",
              "valid_from": "2024-03-01T00:00:00.000Z", "valid_end": null,
              "recorded_at": "2024-05-31T00:00:00.000Z"}},
   {"attribute": "status", "change": "removed",
    "before": {"value": "active", "version": 1, "operation": "assert",
               "valid_from": "2024-01-01T00:00:00.000Z",
               "valid_end": "2024-03-01T00:00:00.000Z",
               "recorded_at": "2024-05-01T00:00:00.000Z"},
    "after": null}]}
```

`change` is `added` when the attribute holds nothing at `from` and something at
`to`, `removed` for the reverse, and `changed` when both hold a different value.
An attribute whose value carried across the interval is not a change, even if a
different version supplies it. `attribute` may be repeated to narrow the result;
naming an attribute the entity never had is a `validation_error`. `to` must be
strictly later than `from`, and the entity must be in effect at both instants.

## Errors

```json
{"error":{"code":"validation_error","message":"human readable detail"}}
```

Validation errors return 400, unknown routes and entities that are not in effect
at the requested instant return 404, and a duplicate entity, a reused
idempotency key, or a correction that would predate the entity returns 409.

## Invariants

- The ledger is append-only; a correction only ever moves a `valid_end` earlier
  and never deletes a value.
- Valid windows of one attribute never overlap.
- The same `(as_of, known_at)` pair always yields the same projection.
- `known_at` never exposes a correction recorded after it, and never exposes a
  trim recorded after it either.
- A transaction interval never runs backwards: `superseded_at` is a transaction
  instant at or after `recorded_at`, never a business instant.
- A version never has `valid_end` before `valid_from`, and a window that a
  correction made empty stays in the history carrying
  `valid_end == valid_from`. The empty window of a retraction row is
  intentional, not an overlap.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
