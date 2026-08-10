"""Stop-guard behaviour: §9.7 (fail-open announced once per cause), §9.10
(cannot end a turn with the bus unattended), §9.11 (binding survives
compaction -- it is keyed to the session id and nothing else)."""

import json

import pytest

from ccbus.guard import ALLOW, run_guard
from ccbus.store import Bus


def payload(session_id="sess-1", cwd=None, root=None):
    return json.dumps({"session_id": session_id,
                       "cwd": cwd or str(root or "")})


def decision(out: str) -> dict:
    return json.loads(out)


def test_no_session_id_allows(root):
    assert run_guard("{}", root) == ALLOW
    assert run_guard("", root) == ALLOW


def test_missing_bus_allows_and_creates_nothing(tmp_path):
    root = tmp_path / "nobus"
    out = run_guard(payload(cwd=str(tmp_path)), root)
    assert out == ALLOW
    assert not root.exists()  # a hook firing on every stop must not mint buses


def test_unbound_session_allows(bus, tmp_path):
    bus.send("alice", "bob", None, "mail exists")
    out = run_guard(payload(cwd=str(tmp_path)), bus.root)
    assert out == ALLOW  # unclaimed sessions are not guarded


def test_offer_binds_and_announces_once(bus, tmp_path):
    bus.offer(str(tmp_path), "bob")
    out = run_guard(payload(cwd=str(tmp_path)), bus.root)
    d = decision(out)
    assert d["decision"] == "block"
    assert "bound to THIS session" in d["reason"]
    assert bus.binding_for("sess-1")["name"] == "bob"
    # Second stop, nothing pending: allowed, and the offer is consumed.
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW


def test_offers_are_per_directory(bus, tmp_path):
    """The prototype had one offer per bus; a second claim orphaned the
    first. Offers now rendezvous per directory."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    bus.offer(str(dir_a), "alice")
    bus.offer(str(dir_b), "bob")
    run_guard(payload("sess-a", cwd=str(dir_a)), bus.root)
    run_guard(payload("sess-b", cwd=str(dir_b)), bus.root)
    assert bus.binding_for("sess-a")["name"] == "alice"
    assert bus.binding_for("sess-b")["name"] == "bob"


def test_expired_offer_does_not_bind(bus, tmp_path):
    bus.offer(str(tmp_path), "bob")
    bus.conn.execute("UPDATE offers SET made_at = made_at - 7200")
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW
    assert bus.binding_for("sess-1") is None


def test_binding_matches_normalised_paths(bus, tmp_path):
    """A differently-spelled cwd must not stop a bind; in the prototype
    'never binds' was indistinguishable from 'unclaimed'. Case differences
    only count on Windows -- on a case-sensitive filesystem they are simply
    different directories -- but trailing separators are sloppy everywhere."""
    import os
    bus.offer(str(tmp_path), "bob")
    if os.name == "nt":
        sloppy = str(tmp_path).upper() + "\\"
    else:
        sloppy = str(tmp_path) + "/"
    out = run_guard(payload(cwd=sloppy), bus.root)
    assert decision(out)["decision"] == "block"
    assert bus.binding_for("sess-1")["name"] == "bob"


def _bind(bus, tmp_path, sid="sess-1"):
    bus.offer(str(tmp_path), "bob")
    run_guard(payload(sid, cwd=str(tmp_path)), bus.root)
    assert bus.binding_for(sid)["name"] == "bob"


def test_unread_blocks_once_then_stays_quiet_until_state_changes(bus, tmp_path):
    """§9.10 plus the no-trap rule: same state never blocks twice."""
    _bind(bus, tmp_path)
    bus.send("alice", "bob", None, "first")
    d = decision(run_guard(payload(cwd=str(tmp_path)), bus.root))
    assert d["decision"] == "block"
    assert "waiting for 'bob'" in d["reason"]
    # Unchanged state: allowed.
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW
    # New mail is new state: blocks again.
    bus.send("alice", "bob", None, "second")
    d = decision(run_guard(payload(cwd=str(tmp_path)), bus.root))
    assert d["decision"] == "block"


def test_spoke_last_blocks_unless_watcher_armed(bus, tmp_path):
    _bind(bus, tmp_path)
    bus.send("bob", "alice", None, "question for alice")
    d = decision(run_guard(payload(cwd=str(tmp_path)), bus.root))
    assert d["decision"] == "block"
    assert "no wait is armed" in d["reason"]
    # Arm a watcher: the mailbox is covered, the stop is fine.
    bus.watcher_register("bob", pid=42, interval=2)
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW


def test_reply_clears_spoke_last(bus, tmp_path):
    _bind(bus, tmp_path)
    bus.send("bob", "alice", None, "ping")
    run_guard(payload(cwd=str(tmp_path)), bus.root)  # blocked once
    bus.send("alice", "bob", None, "pong")
    bus.deliver("bob")
    # Bob read the reply and owes nothing: alice spoke last now.
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW


def test_binding_survives_compaction(bus, tmp_path):
    """§9.11: the binding is keyed to session_id alone, so a session whose
    cwd or anything else changed is still guarded."""
    _bind(bus, tmp_path)
    bus.send("alice", "bob", None, "mail")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    d = decision(run_guard(payload("sess-1", cwd=str(elsewhere)), bus.root))
    assert d["decision"] == "block"


def test_release_frees_name_and_unguards(bus, tmp_path):
    _bind(bus, tmp_path)
    dropped = bus.release("bob")
    assert dropped["bindings"] == ["sess-1"]
    bus.send("alice", "bob", None, "mail")
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW


class _Boom:
    """Bus stand-in whose every use explodes with a chosen error."""

    def __init__(self, exc_type):
        self.exc_type = exc_type

    def __call__(self, root=None):
        raise self.exc_type("kaboom with variable data 12345")


def test_crash_fails_open_and_announces_once_per_signature(bus, tmp_path,
                                                           monkeypatch):
    """§9.7: fail open, log it, announce once per distinct cause -- and the
    signature must not include the message text, or suppression never
    engages and the guard blocks every stop."""
    monkeypatch.setattr("ccbus.guard.Bus", _Boom(RuntimeError))
    d = decision(run_guard(payload(cwd=str(tmp_path)), bus.root))
    assert d["decision"] == "block"
    assert "CRASHED" in d["reason"]
    # Same cause again (different interpolated data would not matter): quiet.
    assert run_guard(payload(cwd=str(tmp_path)), bus.root) == ALLOW
    # A different cause speaks again.
    monkeypatch.setattr("ccbus.guard.Bus", _Boom(ValueError))
    d = decision(run_guard(payload(cwd=str(tmp_path)), bus.root))
    assert d["decision"] == "block"
    # Every crash is in the log even when the announcement was suppressed.
    log = (bus.root / "guard-crash.log").read_text(encoding="utf-8")
    assert log.count("kaboom") == 3


def test_crash_with_unwritable_root_still_allows(monkeypatch, tmp_path):
    """Reporting the failure must not become a second way to fail."""
    monkeypatch.setattr("ccbus.guard.Bus", _Boom(RuntimeError))
    # Root path that cannot be a directory: a file stands in its place.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    out = run_guard(payload(cwd=str(tmp_path)), blocker / "sub")
    assert out == ALLOW
