"""Command-line interface. Every invocation is a fresh process; all state
lives in the store. Exit codes: 0 ok, 1 user error or refusal, 2 wait
timeout, 3 store failure."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ccbus.store import Bus, Msg, StoreError, UserError, WatcherConflict

EXIT_OK = 0
EXIT_USER = 1
EXIT_TIMEOUT = 2
EXIT_STORE = 3


def _topic_label(topic: str) -> str:
    return topic if topic else "(none)"


def _render(msgs: list[Msg], as_json: bool) -> str:
    if as_json:
        return json.dumps([m.to_dict() for m in msgs], indent=2)
    out = []
    for m in msgs:
        tag = f" [{m.topic}]" if m.topic else ""
        out.append(f"--- #{m.seq} {m.sender} -> {m.recipient}{tag} @ {m.ts}\n{m.body}")
    return "\n\n".join(out)


def _starvation_note(bus: Bus, me: str, topics: list[str] | None) -> str:
    """What a filtered read passed over. Filtering without this is silent
    starvation: the mail is not lost, but undelivered-forever looks identical
    to lost from where the reader sits."""
    if not topics:
        return ""
    counts = bus.pending_elsewhere(me, topics)
    if not counts:
        return ""
    items = ", ".join(f"{_topic_label(t)} x{n}" for t, n in sorted(counts.items()))
    return f"(also pending on topics you did not read: {items})"


def _warn_live_watcher(bus: Bus, me: str) -> None:
    w = bus.live_watcher(me)
    if w and w["pid"] != os.getpid():
        print(f"ccbus: note: a live wait (pid {w['pid']}) is also reading "
              f"'{me}'; whichever polls first gets each message.",
              file=sys.stderr)


def cmd_send(bus: Bus, args) -> int:
    body = args.text
    if body == "-":
        # PowerShell pipes UTF-8 with BOM; utf-8-sig strips it and is a no-op
        # for everything else.
        body = sys.stdin.buffer.read().decode("utf-8-sig")
    m = bus.send(args.me, args.to, args.topic, body)
    tag = f" [{m.topic}]" if m.topic else ""
    print(f"sent #{m.seq} -> {m.recipient}{tag}")
    return EXIT_OK


def cmd_recv(bus: Bus, args) -> int:
    topics = args.topic or None
    _warn_live_watcher(bus, args.me)
    msgs = bus.deliver(args.me, topics, pid=os.getpid())
    if msgs:
        print(_render(msgs, args.json))
    else:
        print("[]" if args.json else "(no new messages)")
    if not args.json and (note := _starvation_note(bus, args.me, topics)):
        print(note)
    return EXIT_OK


def cmd_peek(bus: Bus, args) -> int:
    msgs = bus.pending(args.me, args.topic or None)
    print(_render(msgs, args.json) if msgs
          else ("[]" if args.json else "(no new messages)"))
    return EXIT_OK


def cmd_wait(bus: Bus, args) -> int:
    topics = args.topic or None
    pid = os.getpid()
    try:
        bus.watcher_register(args.me, pid, args.interval, topics)
    except WatcherConflict as e:
        stale = bus.stale_after(args.interval)
        print(f"ccbus: a live wait for '{args.me}' is already running "
              f"(pid {e.pid}, heartbeat {e.age:.0f}s ago). Two readers under "
              f"one name split the mailbox between them, so this wait "
              f"refuses to start. If that watcher is wanted, rely on it; if "
              f"it is stray, kill pid {e.pid} or let its heartbeat go stale "
              f"(~{stale:.0f}s) and retry.", file=sys.stderr)
        return EXIT_USER
    try:
        return _wait_loop(bus, args, topics, pid)
    finally:
        bus.watcher_unregister(args.me, pid)


def _wait_loop(bus: Bus, args, topics: list[str] | None, pid: int) -> int:
    deadline = time.monotonic() + args.timeout
    data_version = -1  # changes when any *other* connection commits
    while True:
        current = bus.conn.execute("PRAGMA data_version").fetchone()[0]
        if current != data_version:
            data_version = current
            # Read-only check first: a wake with nothing for us must not
            # take the write lock just to find that out.
            msgs = (bus.deliver(args.me, topics, pid=pid)
                    if bus.pending(args.me, topics) else [])
            if msgs:
                print(_render(msgs, args.json))
                if not args.json and (note := _starvation_note(bus, args.me, topics)):
                    print(note)
                return EXIT_OK
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("[]" if args.json
                  else f"(timeout after {args.timeout:g}s, no messages)")
            return EXIT_TIMEOUT
        if not bus.watcher_beat(args.me, pid):
            print(f"ccbus: the watcher slot for '{args.me}' was taken over "
                  f"by another process; stopping this wait so the mailbox "
                  f"has one reader.", file=sys.stderr)
            return EXIT_USER
        time.sleep(min(args.interval, remaining))


def cmd_log(bus: Bus, args) -> int:
    msgs = bus.log(limit=args.limit, topics=args.topic or None,
                   include_archived=args.archived)
    print(_render(msgs, args.json) if msgs
          else ("[]" if args.json else "(empty bus)"))
    return EXIT_OK


def cmd_agents(bus: Bus, args) -> int:
    agents = bus.agents()
    if args.json:
        print(json.dumps(agents, indent=2))
        return EXIT_OK
    if not agents:
        print("(no agents yet)")
        return EXIT_OK
    for a in agents:
        if a["last_read"]:
            status = f"last read {a['last_read']}"
        elif a["has_sent"]:
            # A first responder on a fresh exchange has sent but not yet
            # read; that is normal, not absence.
            status = "no reads yet (alive, has sent)"
        else:
            status = "NEVER SEEN"
        watch = "  [watching]" if a["watching"] else ""
        print(f"{a['name']}\t{a['unread']} unread\t{status}{watch}")
    return EXIT_OK


def cmd_topics(bus: Bus, args) -> int:
    rows = bus.topics()
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("(empty bus)")
        return EXIT_OK
    for r in rows:
        behind = " ".join(f"{name}:{n}" for name, n in sorted(r["unread"].items()))
        print(f"{_topic_label(r['topic'])}\t{r['messages']} msgs\t"
              f"last #{r['last_seq']} @ {r['last_ts']}\t{behind}")
    return EXIT_OK


def cmd_status(bus: Bus, args) -> int:
    print(json.dumps(bus.status(args.me), indent=2))
    return EXIT_OK


def cmd_trace(bus: Bus, args) -> int:
    info = bus.trace(args.seq)
    if info is None:
        print(f"ccbus: no message #{args.seq} on this bus", file=sys.stderr)
        return EXIT_USER
    if args.json:
        print(json.dumps(info, indent=2))
        return EXIT_OK
    m = info["message"]
    tag = f" [{m['topic']}]" if m["topic"] else ""
    archived = f"  (archived {info['archived_at']})" if info["archived_at"] else ""
    print(f"#{m['seq']} {m['from']} -> {m['to']}{tag} @ {m['ts']}{archived}")
    for d in info["deliveries"]:
        pid = f" (pid {d['pid']})" if d["pid"] else ""
        print(f"  delivered to {d['reader']} @ {d['delivered_at']}{pid}")
    if not info["deliveries"]:
        print("  delivered to nobody")
    if info["awaiting"]:
        print(f"  awaiting: {', '.join(info['awaiting'])}")
    return EXIT_OK


def cmd_claim(bus: Bus, args) -> int:
    stored = bus.offer(args.dir or os.getcwd(), args.me)
    print(f"{args.me} claimed for the next session stopping in {stored} "
          f"(bus {bus.root}). The offer binds when that session next ends a "
          f"turn; it expires in an hour if nothing does.")
    return EXIT_OK


def cmd_release(bus: Bus, args) -> int:
    dropped = bus.release(args.me)
    for sid in dropped["bindings"]:
        print(f"released binding {sid} -> {args.me}; that session is no "
              f"longer guarded and will not be told")
    for d in dropped["offers"]:
        print(f"dropped pending offer for {args.me} in {d}")
    if not dropped["bindings"] and not dropped["offers"]:
        print(f"nothing bound or offered for {args.me}")
    else:
        print(f"{args.me} is free; claim again to restart the handshake")
    return EXIT_OK


def cmd_gc(bus: Bus, args) -> int:
    report = bus.gc(args.age, args.force)
    n = len(report["eligible"])
    if n and args.force:
        print(f"archived {n} delivered message(s) older than {args.age}d. "
              f"They leave `log` (use --archived to still see them); "
              f"nothing is deleted.")
    elif n:
        print(f"gc would archive {n} delivered message(s) older than "
              f"{args.age}d. Re-run with --force to do it. Nothing is ever "
              f"deleted; `reset` remains the only destructor.")
    else:
        print(f"nothing to archive (no delivered messages older than {args.age}d)")
    for name, count in sorted(report["kept"].items()):
        print(f"kept {count} undelivered for {name}")
    return EXIT_OK


def cmd_reset(bus: Bus, args) -> int:
    if not args.force:
        counts = bus.doctor()
        print(f"reset would delete {counts['messages']} live and "
              f"{counts['archived']} archived message(s), plus all delivery "
              f"records, watchers, claims and bindings on {bus.root}, with "
              f"no way back.\nRe-run with --force if that is what you want. "
              f"To throw away a test bus instead of the live one, point "
              f"CCBUS_DIR at a scratch directory.", file=sys.stderr)
        return EXIT_USER
    counts = bus.reset()
    print(f"bus reset ({counts['messages']} message(s) deleted from {bus.root})")
    return EXIT_OK


def cmd_doctor(bus: Bus, args) -> int:
    info = bus.doctor()
    if args.json:
        print(json.dumps(info, indent=2))
        return EXIT_OK
    print(f"bus:       {info['bus']}")
    print(f"database:  {info['db']}")
    print(f"integrity: {info['integrity']}")
    print(f"messages:  {info['messages']} live, {info['archived']} archived")
    for w in info["watchers"]:
        state = "live" if w["live"] else "STALE"
        print(f"watcher:   {w['reader']} pid {w['pid']} ({state}, "
              f"heartbeat {w['heartbeat_age']:.0f}s ago)")
    for o in info["offers"]:
        print(f"offer:     '{o['name']}' pending for {o['dir']}")
    for b in info["bindings"]:
        print(f"binding:   {b['session_id']} -> {b['name']} ({b['dir']})")
    return EXIT_OK


def cmd_guard(bus: Bus, args) -> int:
    # Import here: the guard wraps everything in its own fail-open handler
    # and must not depend on this module's error handling. Stdin comes from
    # whatever shell invoked the hook; utf-8-sig strips the BOM PowerShell
    # pipes emit.
    from ccbus.guard import run_guard
    payload = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    print(run_guard(payload))
    return EXIT_OK


_NEEDS_ME = {"send", "recv", "peek", "wait", "status", "claim", "release"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccbus",
        description="Async message bus for Claude Code sessions on one machine")
    p.add_argument("--me", default=os.environ.get("CCBUS_ME"),
                   help="my agent name (or $CCBUS_ME)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    # The same flags are accepted after the subcommand; SUPPRESS keeps an
    # omitted flag from clobbering the value parsed at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--me", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=lambda **kw: argparse.ArgumentParser(
                               parents=[common], **kw))

    s = sub.add_parser("send", help="send a message")
    s.add_argument("to", help="recipient name, or * for broadcast")
    s.add_argument("text", help="message body, or - to read stdin")
    s.add_argument("--topic", help="topic tag; readers can filter on it")
    s.set_defaults(func=cmd_send)

    read_topic = dict(action="append", metavar="TOPIC", dest="topic",
                      help="only this topic; repeatable. Delivery is "
                           "per-message, so filtering never skips another "
                           "topic's mail")

    s = sub.add_parser("recv", help="read new messages, mark them delivered")
    s.add_argument("--topic", **read_topic)
    s.set_defaults(func=cmd_recv)

    s = sub.add_parser("peek", help="read new messages without marking them")
    s.add_argument("--topic", **read_topic)
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("wait", help="block until a message arrives (exit 2 on timeout)")
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--interval", type=float, default=2)
    s.add_argument("--topic", **read_topic)
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("log", help="full transcript, no delivery marks touched")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--topic", action="append", metavar="TOPIC", dest="topic")
    s.add_argument("--archived", action="store_true",
                   help="include archived messages")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("agents", help="who is on the bus, unread counts, liveness")
    s.set_defaults(func=cmd_agents)

    s = sub.add_parser("topics", help="topics, traffic, who is behind on each")
    s.set_defaults(func=cmd_topics)

    s = sub.add_parser("status", help="one-shot state for the stop guard (JSON)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("trace", help="delivery audit for one message")
    s.add_argument("seq", type=int, help="message number (#N)")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("claim", help="offer this session's name to the stop guard")
    s.add_argument("dir", nargs="?", help="project root (default: cwd)")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("release", help="drop --me's stop-guard bindings and offers")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("gc", help="archive old delivered messages (soft, reversible)")
    s.add_argument("--age", type=int, default=30,
                   help="minimum age in days (default 30)")
    s.add_argument("--force", action="store_true",
                   help="actually archive; without it gc only reports")
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("reset", help="delete every message and all state")
    s.add_argument("--force", action="store_true",
                   help="required; there is no undo")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("doctor", help="integrity check and state overview")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("guard", help="stop-hook entry point (stdin: hook payload)")
    s.set_defaults(func=cmd_guard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in _NEEDS_ME and not args.me:
        parser.error("--me is required (or set $CCBUS_ME)")
    bus = Bus()
    try:
        return args.func(bus, args)
    except UserError as e:
        print(f"ccbus: {e}", file=sys.stderr)
        return EXIT_USER
    except StoreError as e:
        print(f"ccbus: {e}", file=sys.stderr)
        return EXIT_STORE
    except KeyboardInterrupt:
        return EXIT_USER
    finally:
        bus.close()
