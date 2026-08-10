---
name: announce
description: Check and announce machine-wide resources before taking them — Docker daemon, containers, compose stacks, fixed ports, integration/e2e test runs, shared databases, emulators, dev servers. Use BEFORE any command that starts a container, binds a port, runs integration tests, or touches a shared service, and whenever another Claude session may be running on this machine. Also use when work fails with port-in-use, address-already-in-use or container-name-conflict errors, or the user asks "is anything using X", "is the stack up", "did another session start this".
---

# Local resources — one machine, many sessions

Other Claude sessions run on this box, other repos. They cannot see your work.
You cannot see theirs. Shared singletons collide silently: their `compose up`
kills your test run mid-suite, and neither side gets an error that names the
cause.

## First: load the bus

All announcing and listening happens over the **claude-code-bus** plugin's
`bus` skill. Load it and follow it. Nothing about how it works is repeated
here — that is its job, not this one's.

Topic: `local-resources`. Everything below goes on that topic.

## Singleton or not

Singleton — announce:

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

## Three announcements

1. **Session start.** Repo, ticket, what you expect to need later. "No
   singletons in use yet" is a real announcement — it says a session exists.
2. **Before taking. Not after.** Read the topic first. Announce, then take. The
   gap between taking and announcing is the whole race.
3. **On release.** Name what you freed.

Read before 2 every time. Quiet topic is not proof it is free — it is proof
nobody said. Check, then announce.

Someone else holds it: wait for their release, or ask the user. Do not take it
anyway and do not "just peek".

## Watch while you hold

Keep listening for the whole time you hold a resource. Another session may need
it, and a request you never read is the same as a refusal. The bus skill
covers how to wait without burning the session.

## Left running

Leaving a stack or port up is fine and often wanted. Say so **explicitly** on
release: what is still up, on what port. The next session must be told, not
left to discover it.

## Correct yourself out loud

Took nothing because the daemon was down? Build died before it bound the port?
Announce the correction. A claim you are not actually holding blocks another
session for nothing, and it is the one failure the other side cannot detect.

## Do not

- Take silently because "it'll be quick". Quick is when collisions happen.
- Forget the release. Worse than never announcing — it blocks indefinitely.
- Claim a whole class ("docker") when you need one thing. Name the thing.
- Promise an ETA you have not earned. You have read the task, not done it.
