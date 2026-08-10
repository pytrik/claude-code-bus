"""End-to-end CLI behaviour: exit codes, rendering, refusals, and the
shell-facing traps (BOM on stdin) that bit the prototype."""

import json

from ccbus.cli import EXIT_OK, EXIT_STORE, EXIT_TIMEOUT, EXIT_USER
from ccbus.store import Bus


def test_send_recv_roundtrip(run):
    code, out, _ = run("--me", "alice", "send", "bob", "hello", "--topic", "greet")
    assert code == EXIT_OK
    assert "sent #1 -> bob [greet]" in out
    code, out, _ = run("--me", "bob", "recv")
    assert code == EXIT_OK
    assert "#1 alice -> bob [greet]" in out
    assert "hello" in out
    code, out, _ = run("--me", "bob", "recv")
    assert "(no new messages)" in out


def test_send_stdin_strips_bom(run):
    """PowerShell pipes UTF-8 with BOM; the body must not keep it."""
    code, out, _ = run("--me", "alice", "send", "bob", "-",
                       stdin="\ufeffbody from stdin".encode("utf-8"))
    assert code == EXIT_OK
    code, out, _ = run("--me", "bob", "recv", "--json")
    msgs = json.loads(out)
    assert msgs[0]["text"] == "body from stdin"


def test_recv_json_shape(run):
    run("--me", "alice", "send", "bob", "payload", "--topic", "t")
    code, out, _ = run("--me", "bob", "recv", "--json")
    msgs = json.loads(out)
    assert msgs == [{"seq": 1, "ts": msgs[0]["ts"], "from": "alice",
                     "to": "bob", "topic": "t", "text": "payload"}]


def test_filtered_recv_reports_starvation(run):
    run("--me", "alice", "send", "bob", "r", "--topic", "review")
    run("--me", "alice", "send", "bob", "d", "--topic", "deploy")
    code, out, _ = run("--me", "bob", "recv", "--topic", "review")
    assert "also pending on topics you did not read: deploy x1" in out


def test_peek_leaves_mail_pending(run):
    run("--me", "alice", "send", "bob", "x")
    run("--me", "bob", "peek")
    code, out, _ = run("--me", "bob", "recv")
    assert "x" in out


def test_wait_returns_immediately_when_mail_pending(run):
    run("--me", "alice", "send", "bob", "already here")
    code, out, _ = run("--me", "bob", "wait", "--timeout", "5",
                       "--interval", "0.05")
    assert code == EXIT_OK
    assert "already here" in out


def test_wait_times_out_with_exit_2(run):
    code, out, _ = run("--me", "bob", "wait", "--timeout", "0.3",
                       "--interval", "0.05")
    assert code == EXIT_TIMEOUT
    assert "timeout" in out


def test_wait_refused_while_another_watcher_lives(run, root):
    b = Bus(root)
    b.watcher_register("bob", pid=99999, interval=2)
    b.close()
    code, _, err = run("--me", "bob", "wait", "--timeout", "1",
                       "--interval", "0.05")
    assert code == EXIT_USER
    assert "pid 99999" in err
    assert "refuses to start" in err


def test_recv_warns_about_live_watcher(run, root):
    b = Bus(root)
    b.send("alice", "bob", None, "x")
    b.watcher_register("bob", pid=99999, interval=2)
    b.close()
    code, out, err = run("--me", "bob", "recv")
    assert code == EXIT_OK
    assert "pid 99999" in err  # delivered, but the co-reader is named


def test_missing_me_is_an_error(run):
    import pytest
    with pytest.raises(SystemExit):
        run("recv")


def test_invalid_name_exit_1(run):
    code, _, err = run("--me", "bad name", "send", "bob", "x")
    assert code == EXIT_USER
    assert "not a usable agent name" in err


def test_agents_render(run):
    run("--me", "alice", "send", "ghost", "hello?")
    run("--me", "alice", "send", "bob", "hi")
    run("--me", "bob", "recv")
    code, out, _ = run("agents")
    lines = dict(line.split("\t", 1) for line in out.strip().splitlines())
    assert "NEVER SEEN" in lines["ghost"]
    assert "no reads yet (alive, has sent)" in lines["alice"]
    assert "last read" in lines["bob"]
    assert "(just now)" in lines["bob"]  # staleness must be visible


def test_agents_shows_staleness(run, root):
    """A read timestamp hours old must say so: a bootstrapping session
    cannot otherwise tell a dead session from an idle one."""
    b = Bus(root)
    b.send("alice", "bob", None, "x")
    b.deliver("bob")
    b.conn.execute("UPDATE deliveries SET delivered_at = "
                   "'2026-08-01T00:00:00+00:00'")
    b.close()
    code, out, _ = run("agents")
    bob_line = next(l for l in out.splitlines() if l.startswith("bob"))
    assert "d ago" in bob_line


def test_topics_render(run):
    run("--me", "alice", "send", "bob", "r", "--topic", "review")
    run("--me", "alice", "send", "bob", "plain")
    code, out, _ = run("topics")
    assert "review" in out
    assert "(none)" in out


def test_trace_unknown_message(run):
    code, _, err = run("trace", "42")
    assert code == EXIT_USER
    assert "no message #42" in err


def test_trace_after_recv_names_reader(run):
    run("--me", "alice", "send", "bob", "x")
    run("--me", "bob", "recv")
    code, out, _ = run("trace", "1")
    assert "delivered to bob" in out


def test_reset_previews_then_acts(run):
    run("--me", "alice", "send", "bob", "x")
    code, _, err = run("reset")
    assert code == EXIT_USER
    assert "would delete" in err
    code, out, _ = run("reset", "--force")
    assert code == EXIT_OK
    code, out, _ = run("log")
    assert "(empty bus)" in out


def test_gc_preview_and_force(run, root):
    b = Bus(root)
    b.send("alice", "bob", None, "old")
    b.deliver("bob")
    b.conn.execute("UPDATE messages SET ts = '2026-07-01T00:00:00+00:00'")
    b.close()
    code, out, _ = run("gc", "--age", "7")
    assert "would archive 1" in out
    code, out, _ = run("gc", "--age", "7", "--force")
    assert "archived 1" in out
    code, out, _ = run("log")
    assert "(empty bus)" in out
    code, out, _ = run("log", "--archived")
    assert "old" in out


def test_corrupt_store_exit_3(run, root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "bus.db").write_bytes(b"not a database at all")
    code, _, err = run("--me", "bob", "recv")
    assert code == EXIT_STORE
    assert "Refusing" in err


def test_claim_rejects_missing_directory(run):
    code, _, err = run("--me", "bob", "claim", "C:/definitely/not/a/real/dir")
    assert code == EXIT_USER
    assert "refusing" in err.lower()


def test_claim_release_flow(run, tmp_path):
    code, out, _ = run("--me", "bob", "claim", str(tmp_path))
    assert code == EXIT_OK
    assert "claimed" in out
    code, out, _ = run("--me", "bob", "release")
    assert "dropped pending offer" in out
    code, out, _ = run("--me", "bob", "release")
    assert "nothing bound or offered" in out


def test_guard_subcommand_tolerates_bom(run, tmp_path):
    """The hook's stdin comes from an arbitrary shell; a BOM must not crash
    the guard (it did, in the first smoke test of this very repo)."""
    payload = json.dumps({"session_id": "s1", "cwd": str(tmp_path)})
    code, out, _ = run("guard", stdin=("\ufeff" + payload).encode("utf-8"))
    assert code == EXIT_OK
    assert out.strip() == "{}"


def test_doctor_reports_health(run):
    run("--me", "alice", "send", "bob", "x")
    code, out, _ = run("doctor")
    assert code == EXIT_OK
    assert "integrity: ok" in out
