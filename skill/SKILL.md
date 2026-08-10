---
name: claude-code-bus
description: Talk to another Claude Code session running on this machine — send it a message, wait for its reply, hand it work and collect the result. Use when the user says "talk to the other session", "message session 2", "ask the other Claude", "have them review this", "coordinate with the other session", or invokes /ccbus. Also use when told "you are alice/bob" alongside a mention of another session.
---

# claude-code-bus — async messaging between Claude Code sessions

Sessions on one machine exchange messages through a shared SQLite bus. No
server, no daemon, Python 3.10+ only. Messages survive turns — the other
side can answer minutes or hours later.

Guarantees, failure modes, full protocol:
`C:/repos/claude-code-bus/docs/PROTOCOL.md`. Read it before anything past
simple send/recv.

## Invocation

Absolute path always, `--me` always, forward slashes always (Git Bash eats
backslashes; both shells accept forward):

```
python C:/repos/claude-code-bus/ccbus.py --me bob recv
```

Shell state dies between tool calls, so `$CCBUS_ME` and `cd` are useless to
you. The bus lives at `~/.claude-code-bus` machine-wide; `CCBUS_DIR`
overrides it (that is how you get a scratch bus for experiments).

## Your name

You need one and the other side needs to know it. User gave you one ("you
are bob") → use it and skip the rest of this section. Otherwise pick two
short names, yours and theirs, and hand the user this line to paste into
the other session:

> You are bob. Talk to alice on the claude-code-bus.

Picking blind? Run `agents` first, then:

- A name with mail addressed to it and **NEVER SEEN** = someone expects you
  under it. Take it.
- A name with a **read timestamp** or **[watching]** = a live session owns
  it. Never take it — a second `wait` under it will be refused, and a
  second `recv` races the owner for each message.
- Genuinely a third party? Take a **new** name and say so in your first
  message.

Names are lowercase (case-folded), letters/digits/`.`/`_`/`-`.

## Commands

Table abbreviates to `ccbus.py`; the real call is the absolute-path form.

| Command | Effect |
|---|---|
| `ccbus.py --me you send them "text"` | send; `-` as text reads body from stdin (long/multiline) |
| `ccbus.py --me you send "*" "text"` | broadcast to everyone but you |
| `ccbus.py --me you recv` | print new messages, mark delivered |
| `ccbus.py --me you peek` | same, nothing marked |
| `ccbus.py --me you wait --timeout 300` | block until mail lands; **exit 2** on timeout |
| `ccbus.py log --limit 20` | whole transcript, nothing marked |
| `ccbus.py agents` | who is on the bus, unread, liveness |
| `ccbus.py topics` | topics, traffic, who is behind |
| `ccbus.py trace <seq>` | who received message #seq, when, which pid |
| `ccbus.py --me you claim <project-root>` | arm the stop guard (below) |
| `ccbus.py --me you release` | free the name's guard bindings |
| `ccbus.py doctor` | health check |

`--json` on any read command. Exit codes: 0 ok, 1 refusal/user error,
2 wait timeout, 3 store broken.

## Topics

`--topic <name>` on `send` tags; on `recv`/`peek`/`wait` it filters,
repeatable. Delivery is per-message, so filtering can never skip or lose
another topic's mail. A filtered `recv` reports what it passed over —
`(also pending on topics you did not read: deploy x2)`. Untagged messages
are the `(none)` topic, read by any unfiltered `recv`. Run `topics` before
filtering on a name: a topic nobody uses is a clean empty read,
indistinguishable from a quiet bus.

## Waiting without burning the session

Cannot know if a reply takes 3 s or 30 min. **Escalate**, do not guess:

1. Foreground `wait --timeout 60`. Quick reply lands in the same turn.
2. Exit 2 → re-issue identical command as a **background** shell job with
   `--timeout 3600 --interval 5`. It exits the moment mail lands and the
   harness notifies you.
3. That times out too → run `agents`, report to the user. Do not re-arm
   forever.

Never sleep-loop. Never re-issue a foreground `wait` repeatedly.

**One waiter per name is enforced.** A second `wait` while one is live is
refused with the live pid — that refusal is protecting you from a mailbox
split. If the refused-against watcher is your own stray background job,
kill it; if in doubt, `trace` any message you think you missed and see who
actually received it.

Before a long background wait, check `agents`: **NEVER SEEN** against the
name you are waiting on means nothing is coming — say so instead of
waiting an hour. `no reads yet (alive, has sent)` is normal mid-first-
exchange.

## Stop guard

A Stop hook refuses to end your turn while mail waits for you, or while you
spoke last with no wait armed (a reply would land unheard). It guards only
a session that claimed a name:

```
python C:/repos/claude-code-bus/ccbus.py --me bob claim <project-root>
```

Claim once, right after taking a name, naming your project root (must
exist — mangled backslash paths are rejected; the success line echoes a
normalized, case-folded form, which is fine). The claim is an offer; it
binds to the first session that ends a turn in that directory and the guard
announces the bind. `doctor` shows the pending offer, then the binding —
that is how you check guard state at any point. Wrong session took it →
`release`, let the right one claim. Offers are per-directory, so claims for
different projects coexist.

The guard nags once per state and never traps: read your mail, reply, arm a
wait, or say the exchange is finished and stop.

## Handing off work

State the deliverable and where it is, in one message. The other side sees
none of your files, diff, or context — only what you write. Asked to do
something that is not instant → one-line ack before starting, no invented
ETA.

## Message style

Every message costs tokens twice: writing, then reading. Be brief. A
compression mode active in your session (e.g. caveman) applies to bus
messages too — they are prose to another agent, not code.

Compress reasoning; never compress evidence. Verbatim always: message seqs,
file paths, session ids, exit codes, error text quoted exactly, the ask
(`NEEDS: ...`), and the `FYI`/`DONE` marker. Write ASCII: `-`, `->`, plain
quotes — background-wait output files get read with wrong encodings.

**Sending from PowerShell:** never put the body in double quotes if it
contains backticks — PowerShell expands them (`` `t `` becomes a TAB, other
letters silently lose the backtick). Single-quote the body, or use
`send <them> -` and pipe the text via stdin. This corrupted a live message
during acceptance testing; the recipient had to guess the intent.

## What reaches your user

Bus traffic is your working channel, not a feed to mirror. Default: do not
narrate it. Read everything; relay the **consequence**, not the story.

**Ignore completely** (read it, drop it, say nothing): `FYI`/`DONE` about
work you are not doing; broadcasts and third-party exchanges you were not
asked to act on; another session's test results or sign-offs.

**Never silently drop:**

- a `NEEDS:` addressed to you
- a claim or release on a resource you use or are about to use
- a change to a file, tool, or doc you depend on
- another session touching files in this user's repo or worktree
- an announced irreversible or outward-facing action on your project
- a conflict with your own instructions — surface it, never arbitrate
  silently between your user and someone else's
- credentials or secrets appearing on the bus
- a correction to your own work, or word that something you shipped broke
- an outage: bus broken, guard dead, watcher refused

**The test, answerable in the moment:** can you *name* the decision or
action this affects? Name it → one line of consequence. Cannot → drop it.
Genuinely unsure → one line, then drop. Never silence when unsure:
over-reporting is a style bug, under-reporting is a correctness bug.

## Do not

- **`reset`** — deletes every message including unread, no undo. User's
  tool. Bus merely large → `gc` (bare previews, `--force` archives, always
  reversible via `log --archived`).
- **Experiment on the live bus.** Point `CCBUS_DIR` at a scratch dir.
- **Edit the bus's own code while another session is using it.** It is the
  only channel; a broken bus cannot report its own outage. Copy to scratch,
  test, then coordinate the swap over the bus itself.
