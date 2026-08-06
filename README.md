# claude-code-bus

Async message bus for Claude Code sessions sharing one machine. Sessions in
unrelated repositories exchange messages, hand off work, and announce claims
on machine-wide resources (Docker, ports, shared databases) — without a
server, a daemon, or any dependency beyond Python 3.10+.

This is the permanent implementation of the `ccbus` prototype; its design
rationale is recorded in [docs/DESIGN.md](docs/DESIGN.md) and the user/agent
contract in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Why it exists

A Claude Code session only runs during a turn; between turns it does not
exist. Two sessions therefore cannot talk over sockets or pipes — the channel
must hold a message for minutes or hours and deliver it whenever the
recipient next runs. The bus is that channel: a single SQLite database that
both sides can reach, with exactly-once delivery per reader and a stop-hook
guard that keeps sessions from walking away mid-conversation.

## Quickstart

```
python C:/repos/claude-code-bus/ccbus.py --me alice send bob "hello"
python C:/repos/claude-code-bus/ccbus.py --me bob recv
python C:/repos/claude-code-bus/ccbus.py --me bob wait --timeout 300
python C:/repos/claude-code-bus/ccbus.py agents
```

No install needed; the entry shim resolves everything. The bus lives at
`~/.claude-code-bus/bus.db` (override with `CCBUS_DIR`). Forward slashes on
purpose: Git Bash eats backslashes.

## Command surface

| Command | Effect |
|---|---|
| `send <to> <text>` | send; `*` broadcasts, `-` reads body from stdin |
| `recv` | print new messages, mark them delivered |
| `peek` | same, without marking |
| `wait` | block until mail lands; exit 2 on timeout |
| `log` | full transcript, delivery marks untouched |
| `agents` | who is on the bus, unread counts, liveness |
| `topics` | topics, traffic, who is behind on each |
| `trace <seq>` | delivery audit for one message: who got it, when, which pid |
| `claim <dir>` / `release` | stop-guard identity handshake |
| `status` | one-shot JSON state (used by the guard) |
| `gc` | archive old delivered messages (soft, reversible) |
| `reset --force` | wipe everything (the only destructor) |
| `doctor` | integrity check and state overview |
| `guard` | Stop-hook entry point |

Exit codes: `0` ok · `1` user error or refusal · `2` wait timeout ·
`3` store failure. `--json` on any read command.

## Design in one paragraph

One SQLite database (WAL). A message is a row; `seq` (AUTOINCREMENT) is the
total order, so there are no timestamp identities and no clock-skew hazards.
Delivery is a row per (message, reader) written in the same immediate
transaction that selected the messages — exactly-once per reader under any
mix of filtered and unfiltered reads, and under concurrent readers. A
watcher registry with heartbeat liveness makes a second concurrent `wait`
under one name a refused error instead of a silent mailbox split. The stop
guard is Python end to end and registered as a Claude Code `Stop` hook;
it fails open, logs every crash, and announces each distinct failure once.

## Development

```
python -m pytest tests
```

Stdlib-only runtime; pytest is the only dev dependency. Layout:

```
ccbus.py            zero-install entry shim
src/ccbus/store.py  SQLite layer: schema, delivery, watchers, claims
src/ccbus/cli.py    argument parsing, rendering, exit codes
src/ccbus/guard.py  Stop-hook guard (fail-open, announce-once)
skill/              agent-facing skill files, installed at cutover
docs/               protocol contract and design record
install.py          user-run installer (skill files + Stop hook)
```
