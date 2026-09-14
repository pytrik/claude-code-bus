# claude-code-bus

Two Claude Code skills for sessions sharing one machine:

- **`claude-code-bus:bus`** — how to talk to another session: find it with
  `ListAgents`, message it with `SendMessage`, wait with `notify_when_idle`,
  hand off work, and decide what of the exchange reaches your user.
- **`local-resources:announce`** (optional companion) — claim machine-wide
  singletons — Docker, fixed ports, integration-test runs, shared databases —
  in `~/.claude/local-resources.json` before taking them, and message the
  live owner instead of colliding.

Both are pure skills. The transport is Claude Code's built-in cross-session
messaging; there is no daemon, no database, no hook, no Python.

## Install

```
claude plugin marketplace add pytrik/claude-code-bus
claude plugin install claude-code-bus@pytrik
claude plugin install local-resources@pytrik
```

For plugin development, run a session with `claude --plugin-dir <repo>`.

## History

Versions 1.x were a SQLite message bus with a CLI, a Stop-hook guard and a
watcher protocol, built when sessions could not reach each other directly.
Claude Code now delivers messages between live sessions itself — an idle
session wakes on a message, a busy one receives it after its current tool
call — so the machinery went and the protocol stayed. Lost on purpose:
durability across session ends, a shared log, topics, broadcast.

## Layout

```
skills/bus/SKILL.md                                  the messaging protocol
plugins/local-resources/skills/announce/SKILL.md     the resource-claim protocol
.claude-plugin/                                      plugin manifest and marketplace listing
```
