# Design record

Why this implementation is shaped the way it is. The prototype's
retrospective (`~/.claude/skills/ccbus/DESIGN.md`, written 2026-08-04) is
the input document; this file records which of its recommendations were
followed, which were not, and what that bought. Sections referenced as
"§n" are that document's.

## What was kept

The prototype's *interface* survived almost verbatim — send/recv/peek/wait/
log/agents/topics/claim/release/gc/reset, exit 2 on wait timeout, `--me`,
`--topic`, `--json`, the escalation ladder, the stop-guard handshake, the
relay discipline. Those were shaped by real traffic and none of them were
the source of bugs. What changed is everything underneath.

## The three structural decisions

### 1. SQLite instead of file-per-message (§8 recommendation 1)

One WAL-mode database. This was the prototype's own first recommendation,
and it eliminates entire failure classes by construction rather than by
vigilance:

- **No timestamp identities.** `seq` (AUTOINCREMENT) is the total order;
  insertion order under the write lock *is* delivery order. The prototype's
  settling margin, buried-message detector, clock-skew warnings, and the
  mtime heuristic (§4.4) have no analogue here because the hazard they
  patrolled does not exist.
- **No partial writes, no corrupt-file stall.** A row is committed or
  absent. The prototype's stall-don't-skip machinery (§4.3) — correct, but
  load-bearing complexity — is unnecessary; its spirit survives as "a store
  that cannot be read is never written" (exit 3, file left byte-identical).
- **Schema-shaped corruption (Class F) is a column constraint** now, not a
  runtime validator that every parse site must remember to route through.

### 2. Per-(message, reader) delivery instead of high-water cursors

The prototype's most serious open defect (§7) was one-reader-per-name as
documentation: cursors split between concurrent readers and messages
vanished with `0 unread` showing everywhere — Class A in the delivery path.
Two changes close it:

- **Delivery records.** `recv` selects pending rows and writes delivery
  rows in one immediate transaction. Exactly-once per name holds under any
  interleaving, filtered or not; the per-topic mark bookkeeping, floor
  raising, starvation-by-mark and cursor pruning problems all disappear.
  Every delivery is an audit record (reader, time, pid) — `trace` turns
  "a message vanished" into a lookup.
- **An enforced watcher slot.** `wait` registers a heartbeat; a second
  `wait` under a live name is refused with the live pid. Liveness is the
  heartbeat, not the pid, so Windows pid recycling (§5 Class E) is
  irrelevant and a crashed waiter frees its slot in seconds. `recv` beside
  a live watcher warns with the pid.

A fresh name having no delivery rows *is* the "broadcast reaches readers
that do not exist yet" property — it falls out of the model instead of
being implemented.

### 3. The guard in the same language as the bus (§8 recommendation 4)

Every Class E platform trap — BOM, `$ErrorActionPreference` interacting
with native stderr, locale dates, pid recycling — came from the PowerShell
hook, not from the bus. The guard is now `ccbus.py guard`: Python end to
end, registered directly as the Stop hook command. The prototype called
this "the highest-value change on this list relative to effort"; it was.

Guard semantics preserved from the prototype, because they were right:
block on unread or spoke-last-unwatched, allow on live watcher, never block
twice on the same state, fail open with an announcement once per distinct
error signature (type + location, never message text — §5 Class C explains
why). Crash bookkeeping uses plain files in the bus root rather than the
database, because the most likely crash cause is the database.

Handshake fixes over the prototype (§7 open problems):

- Offers are **per directory**, so a second claim no longer orphans the
  first.
- Paths are **normalized on both sides** (`resolve` + `normcase`), so an
  offer can no longer silently never-bind over a trailing separator or
  drive-case difference.
- Binding is **transactional**: two sessions stopping simultaneously in one
  directory produce exactly one bind, and the loser sees no offer rather
  than a false announcement.

Still unsolved, deliberately: first-come binding can still give the name to
the wrong of two sessions in one directory. The bind announcement plus
`release` remain the mitigation; solving it outright needs the harness to
tell a session its own id, which it cannot do.

## Smaller decisions

- **Names are normalized and restricted** (casefold; `[a-z0-9._-]`). The
  prototype normalized topics after a near-miss but left names exact;
  `Alice`/`alice` would have been the same silent split-brain.
- **`gc` archives by flagging rows**, never moving files; `log --archived`
  keeps history readable, and nothing is eligible until every intended
  reader has a delivery record. `reset --force` remains the only
  destructor, and both keep the preview-by-default shape.
- **The wait loop polls `PRAGMA data_version`** and only runs the delivery
  query when another connection has committed — a background wait costs a
  single pragma per tick.
- **The guard never creates the bus.** A Stop hook fires on every session's
  every stop, most of which have nothing to do with the bus; `guard`
  returns allow immediately if `bus.db` does not exist.
- **Zero-install entry shim** (`ccbus.py` at the repo root) so the
  canonical invocation is one absolute path with no packaging step,
  keeping the tool agent-legible and agent-patchable (§8 recommendation 2).

## Failure-class countermeasures (§5)

| Class | Countermeasure |
|---|---|
| A — success reported over lost data | Writes are transactional; a store that cannot be read is never written (tested byte-identical); refusals are loud and non-zero |
| B — docs assert what code does not do | PROTOCOL.md claims map to named tests; docs change in the commit that changes behaviour |
| C — failing open silently | Guard logs every crash and announces once per signature; suppression keyed on type+location, not text |
| D — tests that cannot fail | Tests assert premises (first watcher live before expecting refusal; sender count checked; random-pattern test checks volume) |
| E — platform traps | No PowerShell anywhere; stdin decoded `utf-8-sig` at both boundaries; paths normalized; heartbeats instead of pids |
| F — schema-shaped corruption | Columns and CHECK constraints; foreign/newer databases refused before any write |

## Acceptance criteria (§9) → tests

1. Delivery to a not-yet-existing reader — `test_message_to_future_reader_is_delivered_on_first_read`
2. Filtered/unfiltered composition, exactly-once, ordered — `test_filtered_and_unfiltered_reads_compose`, `test_filtered_read_pattern_property`
3. Unreadable store stalls loudly, advances nothing — `test_garbage_file_refused_loudly`, `test_corrupt_store_exit_3`
4. Wrong-shape data — impossible by schema; foreign DBs refused (`test_foreign_database_refused`)
5. Two readers under one name — refused (`test_second_live_watcher_refused`, `test_wait_refused_while_another_watcher_lives`) or exactly-once in total (`test_concurrent_deliver_no_loss_no_duplication`)
6. Nothing silently dropped below a mark — no marks exist; `test_gc_archives_only_delivered_and_old` shows kept mail still delivers
7. Fail-open components announce once per cause — `test_crash_fails_open_and_announces_once_per_signature`
8. Cannot-read-state refuses to write, non-zero — `test_foreign_database_refused`, `test_newer_schema_refused`
9. `agents` three-way distinction — `test_agents_distinguishes_three_states`
10. Sent-with-no-listener cannot stop silently — `test_spoke_last_blocks_unless_watcher_armed`
11. Binding survives compaction — `test_binding_survives_compaction`
12. Cold session establishes contact from docs alone — cannot be run by the author; executed live at cutover with a second session

## What was deliberately not built

- **MCP push / a daemon** (§8): the polling loop has never been the failure
  point, and a daemon adds a lifecycle that can be. `data_version` makes
  polling nearly free.
- **Authentication, multiple machines, display casing, per-message TTLs**:
  out of scope per the same reasoning as the prototype; the bus is one
  user's sessions on one machine.
- **Migration from the prototype bus**: fresh start by decision; the old
  bus stays readable with the old tool.
