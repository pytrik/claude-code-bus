# claude-code-bus (deprecated)

Coordination between Claude Code sessions on one machine. No longer maintained.

## Tags

- **`v1-final`** — SQLite message bus with a CLI, a Stop-hook guard and a watcher
  protocol, built when sessions could not reach each other directly.
- **`v2-final`** — two pure-skill plugins over Claude Code's built-in cross-session
  messaging (`ListAgents` / `SendMessage`): `claude-code-bus:bus`, the messaging
  protocol, and `local-resources:announce`, claims on machine-wide singletons
  (Docker, fixed ports, test runs, shared databases) in a shared state file.

## Why deprecated

With the transport built into Claude Code, all that remained was one skill
describing how Claude should communicate with other sessions — by now a
reflection of personal taste rather than a shared tool. A plugin and
marketplace is needlessly complex setup for that; it lives on as a local skill
in `~/.claude/skills` instead.
