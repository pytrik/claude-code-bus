---
name: announce
description: Check and claim machine-wide resources before taking them — Docker daemon, containers, compose stacks, fixed ports, integration/e2e test runs, shared databases, emulators, dev servers. Use BEFORE any command that starts a container, binds a port, runs integration tests, or touches a shared service, and whenever another Claude session may be running on this machine. Also use when work fails with port-in-use, address-already-in-use or container-name-conflict errors, or the user asks "is anything using X", "is the stack up", "did another session start this".
---

# Local resources — one machine, many sessions

Other Claude sessions run on this box, other repos. They cannot see your work.
You cannot see theirs. Shared singletons collide silently: their `compose up`
kills your test run mid-suite, and neither side gets an error that names the
cause.

Claims are **state**, not messages. They live in one file every session reads
before taking anything and writes before taking anything:

```
~/.claude/local-resources.json
```

## The file

A JSON array of claims. Missing file = no claims.

```json
[
  {
    "resource": "docker compose stack hippocampus",
    "owner": "tasks-4b",
    "project": "C:/repos/hippocampus",
    "since": "2026-09-14T08:48:00Z",
    "note": "integration tests; stack stays up on port 5432 afterwards"
  }
]
```

- `resource` — the specific thing, named so another session recognises it:
  a container or stack name, a port number, "docker daemon", a database.
  Never a class ("docker") when you need one thing.
- `owner` — your session name exactly as the `ListAgents` header shows it
  (`This session is <name> [ref]`). That is how another session checks
  whether you are still alive and how it reaches you.
- `project` — your working directory, forward slashes.
- `since` — ISO 8601 UTC.
- `note` — what you are doing with it and what will still be up when you
  release. Optional but usually the most useful field.

Write it atomically: write the whole new array to a temp file in the same
directory, then rename it over the real one. Good enough for a handful of
sessions; the window between read and write is the only race left, and it
is milliseconds.

## Singleton or not

Singleton — claim:

- Docker daemon, Docker Desktop itself, containers, compose stacks
- fixed ports — dev servers, debug ports, anything on a pinned number
- integration / e2e / hub test runs
- shared DB, migrations, seeded data
- emulators, device sims, browser automation
- anything global — node version switch, CLI login, k8s context, global install

Not singleton — skip, no noise:

- repo-local build, unit tests, lint, typecheck
- reading files, git, editing
- anything on a random high port you picked yourself

## Before taking. Not after.

1. **Read the file.** Every time, even if you read it a minute ago.
2. **Check each claim's owner against `ListAgents`.** Only an `interactive`
   row counts — those are the sessions on this machine, and they sort
   first. An owner with no such row (absent, or listed as `offline`) is a
   dead session; its claim is stale and the resource is free. Say so in
   one line to your user, remove the stale claim when you write yours.
3. **Live owner holds it →** `SendMessage` them (the name is right there in
   the file): what you need, why, `NEEDS: release or ETA`. Then either wait
   with `notify_when_idle: true` or ask your user. Never take it anyway and
   never "just peek" — a probe that binds the port is a take.
4. **Free →** write your claim, *then* take the resource. The gap between
   taking and claiming is the whole race; keep it on the safe side.

Quiet file is not proof it is free — it is proof nobody said. A resource
can be held by something that is not a Claude session at all; the file
tells you about sessions, `docker ps` and `netstat` tell you about the
machine. Check both when it matters.

## On release

Delete your claim. If anything is left running — a stack still up, a port
still bound — that is intentional and often wanted, but the next session
must be told, not left to discover it: keep a claim with `note` saying what
is still up, or put it in the claim of whoever takes over.

## Correct yourself out loud

Took nothing because the daemon was down? Build died before it bound the
port? Delete the claim. A claim you are not actually holding blocks another
session for nothing, and it is the one failure the other side cannot detect
from the file alone.

## While you hold

Another session may message you asking for the resource. That message
arrives on its own; read it, answer it in one line — release with a time,
or a reason you cannot. A request you never answer is the same as a
refusal, and the other side will escalate to the user.

## Do not

- Take silently because "it'll be quick". Quick is when collisions happen.
- Forget the release. Worse than never claiming — it blocks until someone
  notices your name has no `interactive` row in `ListAgents`.
- Claim a whole class ("docker") when you need one thing. Name the thing.
- Promise an ETA you have not earned. You have read the task, not done it.
- Edit or remove another live session's claim. Message them instead.
