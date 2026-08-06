"""Stop-hook guard: refuse to end a turn while the bus is unattended.

Runs as a Claude Code ``Stop`` hook: JSON payload on stdin, hook decision on
stdout. Blocks when the bound session has unread mail, or spoke last with no
live watcher armed. Allows when a watcher is live, when nothing is pending,
or when it already blocked on exactly this state -- blocking on unchanged
state twice could trap a session, and acting on the bus always changes the
state.

The prototype's guard was PowerShell and every platform trap it hit (BOM,
ErrorActionPreference, locale dates, pid recycling) came from that. This one
is Python end to end; the hook command is ``python .../ccbus.py guard``.

Failure policy: a broken guard must never wedge a session, so every crash
fails open -- but announced once per distinct error signature, because a
guard that crashes silently is indistinguishable from a guard with nothing
to say. Crash bookkeeping uses plain files in the bus root, not the
database, because the most likely crash cause is the database itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ccbus.store import Bus, default_root

ALLOW = "{}"


def invocation_hint() -> str:
    """The command a session should copy to run the bus."""
    entry = Path(__file__).resolve().parents[2] / "ccbus.py"
    if entry.is_file():
        path = entry.as_posix()
        if " " in path:
            path = f'"{path}"'
        return f"python {path}"
    return "python -m ccbus"


def _block(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def run_guard(stdin_text: str, root: Path | str | None = None) -> str:
    try:
        return _guard(stdin_text, root)
    except Exception:
        return _crashed(root)


def _guard(stdin_text: str, root: Path | str | None) -> str:
    payload = json.loads(stdin_text) if stdin_text.strip() else {}
    session_id = payload.get("session_id")
    if not session_id:
        return ALLOW
    cwd = payload.get("cwd") or os.getcwd()

    bus = Bus(root)
    if not bus.exists():
        # No bus on this machine: never create one from a hook that fires on
        # every session's every stop.
        return ALLOW

    binding = bus.binding_for(session_id)
    if binding is None:
        bound = bus.take_offer(cwd, session_id)
        if bound is None:
            return ALLOW  # unbound sessions are not guarded; guessing would
            # guard the wrong one
        # Announce the bind, once. Binding is first-come: if another session
        # in this directory was meant to hold the name, that one is unguarded
        # and this message is how anyone finds out.
        return _block(
            f"ccbus: the pending claim for '{bound['name']}' just bound to "
            f"THIS session (id {session_id}, directory {bound['dir']}), "
            f"which is now the only session the stop guard watches for that "
            f"name.\nIf another session here was meant to be "
            f"'{bound['name']}', it is unguarded and will not be told. To "
            f"hand the name back: {invocation_hint()} --me {bound['name']} "
            f"release, then have the right session claim again.\nIf this is "
            f"the session you meant, carry on; this fires once.")

    name = binding["name"]
    st = bus.status(name)
    if st["watcher"]:
        return ALLOW  # a live wait is covering the mailbox
    if st["unread"] == 0 and not st["spoke_last"]:
        return ALLOW

    state = (",".join(str(s) for s in st["pending"])
             + f"|{st['last_seq']}|{st['spoke_last']}")
    if bus.stamp_get(session_id) == state:
        return ALLOW  # already blocked on exactly this state; nagging twice
        # running would trap the session
    bus.stamp_set(session_id, name, state)

    if st["unread"] > 0:
        reason = (f"ccbus: {st['unread']} message(s) waiting for '{name}' "
                  f"and nothing is reading them. Run: {invocation_hint()} "
                  f"--me {name} recv")
    else:
        reason = (f"ccbus: '{name}' sent the last message and no wait is "
                  f"armed, so a reply would land unnoticed. Arm one as a "
                  f"background shell job: {invocation_hint()} --me {name} "
                  f"wait --timeout 3600 --interval 5 -- or say the exchange "
                  f"is finished and stop; this will not block on the same "
                  f"state twice.")
    return _block(reason)


def _crashed(root: Path | str | None) -> str:
    """Fail open, log the crash, announce it once per distinct signature.

    The signature is exception type + location, deliberately not the message:
    messages interpolate variable data (paths, pids, quoted content), and a
    signature that varies per crash would never suppress, turning the
    announcement into a block on every stop -- the wedge failing open exists
    to prevent, rebuilt out of the announcement.
    """
    try:
        etype, exc, tb = sys.exc_info()
        frame = tb
        chosen = tb
        while frame is not None:  # deepest frame inside this package
            if "ccbus" in frame.tb_frame.f_code.co_filename:
                chosen = frame
            frame = frame.tb_next
        location = (f"{os.path.basename(chosen.tb_frame.f_code.co_filename)}"
                    f":{chosen.tb_lineno}")
        sig = f"{etype.__name__} at {location}"
        detail = f"{sig}: {exc}"

        root_path = Path(root) if root else default_root()
        root_path.mkdir(parents=True, exist_ok=True)
        log = root_path / "guard-crash.log"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  {detail}\n")

        last = root_path / "guard-crash.last"
        seen = last.read_text(encoding="utf-8-sig").strip() if last.is_file() else ""
        if seen != sig:
            last.write_text(sig, encoding="utf-8")
            return _block(
                f"ccbus stop guard CRASHED and allowed this stop without "
                f"checking the bus, so you are not guarded right now: "
                f"{detail}\nCheck the bus by hand and fix the guard. Full "
                f"log: {log}\nThis will not block again on the same error.")
    except Exception:
        pass  # reporting the failure must not become a second way to fail
    return ALLOW
