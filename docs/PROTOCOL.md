# Protocol: guarantees, failure modes, etiquette

The contract between the bus and anything that uses it. Everything here is
demonstrated by the test suite (`tests/`), not asserted from memory; when
behaviour changes, this file changes in the same commit.

## Setup

Pick a name and keep it for the whole session. The canonical invocation
names you with `--me` and reaches the entry shim by absolute path:

```
python C:/repos/claude-code-bus/ccbus.py --me bob recv
```

Forward slashes, deliberately: Git Bash eats backslashes and turns the path
into garbage that resolves somewhere arbitrary. Claude Code sessions must
use this form — each tool call is a fresh process, so exported variables and
working directories do not survive to the next command.

Names are trimmed and case-insensitive (`Bob` and `bob` are one mailbox) and
limited to lowercase letters, digits, `.`, `_`, `-`. The bus lives at
`~/.claude-code-bus/bus.db`; `CCBUS_DIR` overrides it, which is also how you
get a scratch bus for experiments:

```powershell
$env:CCBUS_DIR = "$env:TEMP\bustest"; python C:/repos/claude-code-bus/ccbus.py --me bob send alice "probe"
```

## Delivery guarantees

- **Total order.** Every message gets a sequence number from the database;
  `#12` means the same message to everyone, and order is identical for all
  readers. There is no clock in the identity, so clock skew cannot reorder,
  hide, or bury a message. Sequence numbers are never reused (except after
  `reset`, which empties the bus entirely).
- **Exactly-once per reader.** Delivery is a per-(message, reader) record
  written in the same transaction that selected the messages. Filtered and
  unfiltered reads compose: read one topic narrowly, then everything, and
  each message arrives exactly once, in order, regardless of pattern.
- **A reader that does not exist yet still gets everything.** A name's first
  `recv` returns the full live history addressed to it — broadcasts sent
  before it was ever named included.
- **Broadcast** (`send * "..."`) reaches every reader except the sender.
- **Concurrent readers under one name cannot split silently.** A second
  `wait` while one is live is refused with an error naming the live pid.
  Concurrent `recv` calls are serialized by the store: each message is
  delivered exactly once in total, and `trace <seq>` shows which process
  received it, when — a message that "vanished" is a lookup, not a mystery.
- **Nothing is skipped, ever.** There is no cursor to advance past unread
  mail. Undelivered messages stay pending until read; `gc` refuses to
  archive them; only `reset --force` can destroy them.

## Topics

`--topic <name>` on `send` tags; on `recv`, `peek` and `wait` it filters
(repeatable). Topic names are trimmed and case-insensitive. A message with
no topic is on the empty topic, shown as `(none)` — a real topic, read by
any unfiltered `recv`.

Filtering cannot lose mail (delivery is per-message), but it can starve you
of what you never look at. Two things make that visible:

- A filtered `recv` reports what it passed over:
  `(also pending on topics you did not read: deploy x2)`.
- `topics` lists every topic with per-reader unread counts.

Filtering on a topic nobody uses is a clean empty read, identical to a quiet
bus. Run `topics` before filtering on a name for the first time.

## Waiting: escalate, do not guess

A sender cannot tell a 3-second reply from a 30-minute one. Escalate:

1. Foreground `wait --timeout 60`. A quick reply lands inside the turn.
2. On exit 2, re-issue as a **background** job with `--timeout 3600`. It
   exits the moment mail lands and the harness notifies the session.
3. If that also times out, run `agents` and report. Do not re-arm forever.

The rule is sender-local: nothing depends on the other side cooperating.
Never poll with a sleep loop, and never re-issue a foreground wait
repeatedly — each attempt burns a turn.

`agents` answers "is anyone home":

```
bob   1 unread   NEVER SEEN                     <- nobody ever ran under this name
bob   1 unread   no reads yet (alive, has sent) <- alive, mid-first-exchange
bob   1 unread   last read 2026-08-06T14:36:41Z <- alive, has mail, working
bob   0 unread   last read ...  [watching]      <- a live wait is armed right now
```

Only `NEVER SEEN` means nobody is coming; say so instead of waiting an hour.
`no reads yet` is normal for a first responder on a fresh exchange.

## The watcher slot

`wait` registers a heartbeat while it runs. One live watcher per name,
enforced: a second `wait` is refused with the live pid and the staleness
window. Liveness is the heartbeat, not the pid, so a crashed waiter's slot
frees itself within seconds and cannot be held hostage by pid reuse. A
`recv` while another process's watcher is live warns on stderr — both will
race for each message, and both outputs may not reach the same session.

## The stop guard

A Claude Code `Stop` hook (`ccbus.py guard`) refuses to end a turn while the
bound session has unread mail, or spoke last with no wait armed. It allows
when a live watcher covers the mailbox, when nothing is pending, or when it
already blocked on exactly this state — acting on the bus always changes the
state, so it nags once per state and can never trap.

Guarding needs identity, which takes a handshake because neither side has
the whole picture (a session cannot learn its own id; the hook gets an id
and a directory but no name):

```
python C:/repos/claude-code-bus/ccbus.py --me bob claim <project-root>
```

`claim` records an *offer* for that directory; the offer binds to the first
session that ends a turn there, and the guard announces the bind (name,
session id, directory) so a wrong first-comer is visible. Offers are per
directory — claims for different projects coexist — and expire after an
hour. Paths are normalized on both sides, so case and trailing separators
cannot make an offer silently never-bind. `claim` refuses a directory that
does not exist (the shell-mangled-backslash trap). The binding is keyed to
the session id alone and survives context compaction. `release` frees a
name: every binding and offer for it, so the right session can claim again.

The guard fails open — a broken guard must never wedge a session — but never
silently: every crash is appended to `<bus>/guard-crash.log` and announced
once per distinct error signature (exception type + location, deliberately
not the message text, which varies per crash and would defeat suppression).

## Failure modes

- **A store that cannot be read is never written.** A corrupt or foreign
  `bus.db` fails every command loudly with exit 3 and instructions; nothing
  recreates or overwrites it, and the file is left byte-identical. `doctor`
  runs an integrity check and shows watchers, offers, and bindings.
- **`gc` archives, `reset` deletes.** Bare `gc` and bare `reset` only
  report; `--force` acts. Archiving is a flag on the row — `log --archived`
  still shows everything, and nothing is eligible until every intended
  reader has a delivery record (every known reader, for a broadcast).
  Everything kept back is counted and named.
- **No authentication.** Any process running as this user can read, send,
  and impersonate. Fine for one user's sessions on one machine;
  disqualifying for anything more. Do not put secrets on the bus.
- **One machine.** The database must be on a local disk shared by all
  sessions (WAL does not work over network filesystems).

## Etiquette

- Address by name for a two-party conversation; broadcast is for
  announcements.
- One question per message; end a message that expects a reply with an
  explicit ask (`NEEDS: ...`), one that does not with `FYI` or `DONE`.
- Acknowledge a request you cannot answer immediately, in one line, without
  inventing an ETA.
- Run `log` before reviewing anything the other side changed; replies cross
  in flight.
- Never experiment on the live bus — point `CCBUS_DIR` at a scratch
  directory.
- `reset` is the user's tool, not yours.
