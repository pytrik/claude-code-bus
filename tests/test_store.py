"""Store-level behaviour, including the acceptance criteria from the
prototype's DESIGN.md §9 that live at this layer."""

import sqlite3

import pytest

from ccbus.store import Bus, StoreError, UserError


def test_send_assigns_monotonic_seqs(bus):
    a = bus.send("alice", "bob", None, "one")
    b = bus.send("alice", "bob", None, "two")
    assert b.seq > a.seq


def test_message_to_future_reader_is_delivered_on_first_read(bus):
    """§9.1: a message sent while the recipient does not exist is delivered
    when it first runs. Also covers broadcasts to readers born later."""
    bus.send("alice", "bob", None, "direct before bob existed")
    bus.send("alice", "*", None, "broadcast before bob existed")
    got = bus.deliver("bob")
    assert [m.body for m in got] == ["direct before bob existed",
                                    "broadcast before bob existed"]


def test_exactly_once_and_ordered(bus):
    for i in range(5):
        bus.send("alice", "bob", None, f"m{i}")
    first = bus.deliver("bob")
    second = bus.deliver("bob")
    assert [m.body for m in first] == [f"m{i}" for i in range(5)]
    assert second == []


def test_filtered_and_unfiltered_reads_compose(bus):
    """§9.2: every message delivered exactly once, in order, regardless of
    read pattern."""
    bus.send("alice", "bob", "review", "r1")
    bus.send("alice", "bob", "deploy", "d1")
    bus.send("alice", "bob", "review", "r2")
    bus.send("alice", "bob", None, "plain")

    reviews = bus.deliver("bob", topics=["review"])
    assert [m.body for m in reviews] == ["r1", "r2"]

    rest = bus.deliver("bob")
    assert [m.body for m in rest] == ["d1", "plain"]

    assert bus.deliver("bob") == []
    assert bus.deliver("bob", topics=["review"]) == []


def test_filtered_read_pattern_property(bus):
    """Randomised §9.2: any interleaving of filtered and unfiltered reads
    delivers each message exactly once, in order within each read."""
    import random
    rng = random.Random(20260806)
    topics = ["a", "b", "c", ""]
    sent = []
    delivered = []
    for step in range(60):
        if rng.random() < 0.6 or not sent:
            t = rng.choice(topics)
            m = bus.send("alice", "bob", t, f"msg-{step}")
            sent.append(m.body)
        else:
            pick = rng.choice([None, ["a"], ["b"], ["c", "a"], [""]])
            batch = bus.deliver("bob", topics=pick)
            delivered.extend(m.body for m in batch)
    delivered.extend(m.body for m in bus.deliver("bob"))
    # The premise must hold or the test is vacuous:
    assert len(sent) > 20
    assert sorted(delivered) == sorted(sent)
    assert len(delivered) == len(set(delivered))


def test_peek_does_not_consume(bus):
    bus.send("alice", "bob", None, "hello")
    assert len(bus.pending("bob")) == 1
    assert len(bus.pending("bob")) == 1
    assert len(bus.deliver("bob")) == 1
    assert bus.pending("bob") == []


def test_sender_does_not_receive_own_broadcast(bus):
    bus.send("alice", "*", None, "to everyone else")
    assert bus.deliver("alice") == []
    assert len(bus.deliver("bob")) == 1


def test_name_and_topic_normalisation(bus):
    """Alice/alice and Review/review are one mailbox and one topic."""
    bus.send("Alice", "  BOB  ", "  Review ", "hi")
    got = bus.deliver("bob", topics=["review"])
    assert len(got) == 1
    assert got[0].sender == "alice"
    assert got[0].topic == "review"


@pytest.mark.parametrize("bad", ["", "*", "has space", "a,b", "semi;colon"])
def test_invalid_names_refused(bus, bad):
    with pytest.raises(UserError):
        bus.send(bad, "bob", None, "x")
    with pytest.raises(UserError):
        bus.send("alice", bad if bad != "*" else "", None, "x")


def test_star_topic_refused(bus):
    with pytest.raises(UserError):
        bus.send("alice", "bob", "*", "x")


def test_pending_elsewhere_reports_starvation(bus):
    bus.send("alice", "bob", "review", "r")
    bus.send("alice", "bob", "deploy", "d1")
    bus.send("alice", "bob", "deploy", "d2")
    bus.deliver("bob", topics=["review"])
    counts = bus.pending_elsewhere("bob", ["review"])
    assert counts == {"deploy": 2}


def test_trace_names_delivery_and_awaiting(bus):
    m = bus.send("alice", "bob", None, "x")
    info = bus.trace(m.seq)
    assert info["deliveries"] == []
    assert info["awaiting"] == ["bob"]
    bus.deliver("bob", pid=1234)
    info = bus.trace(m.seq)
    assert info["awaiting"] == []
    assert info["deliveries"][0]["reader"] == "bob"
    assert info["deliveries"][0]["pid"] == 1234


def test_trace_broadcast_awaits_known_readers(bus):
    bus.send("carol", "alice", None, "make carol a sender")
    bus.deliver("alice")   # alice is now a known reader; bob never read
    m = bus.send("carol", "*", None, "hear ye")
    bus.deliver("alice")
    info = bus.trace(m.seq)
    # alice got it; bob never successfully read anything so is not expected
    assert [d["reader"] for d in info["deliveries"]] == ["alice"]
    assert info["awaiting"] == []


def test_gc_archives_only_delivered_and_old(bus):
    old = bus.send("alice", "bob", None, "old delivered")
    undelivered = bus.send("alice", "carol", None, "old undelivered")
    bus.deliver("bob")
    # Backdate both messages ten days.
    bus.conn.execute(
        "UPDATE messages SET ts = '2026-07-01T00:00:00+00:00' "
        "WHERE seq IN (?, ?)", (old.seq, undelivered.seq))
    report = bus.gc(age_days=7, force=False)
    assert report["eligible"] == [old.seq]
    assert report["kept"] == {"carol": 1}
    # Preview must not archive.
    assert bus.log() and len(bus.log()) == 2

    report = bus.gc(age_days=7, force=True)
    assert report["eligible"] == [old.seq]
    bodies = [m.body for m in bus.log()]
    assert bodies == ["old undelivered"]
    assert len(bus.log(include_archived=True)) == 2
    # §9.6 analogue: the kept message is still deliverable, never dropped.
    assert [m.body for m in bus.deliver("carol")] == ["old undelivered"]


def test_gc_broadcast_needs_every_known_reader(bus):
    bus.send("alice", "bob", None, "seed")
    bus.deliver("bob")
    bus.send("bob", "carol", None, "seed2")
    bus.deliver("carol")
    m = bus.send("alice", "*", None, "old broadcast")
    bus.conn.execute("UPDATE messages SET ts = '2026-07-01T00:00:00+00:00' "
                     "WHERE seq = ?", (m.seq,))
    bus.deliver("bob")
    report = bus.gc(age_days=7, force=False)
    assert m.seq not in report["eligible"]  # carol has not read it
    bus.deliver("carol")
    report = bus.gc(age_days=7, force=False)
    assert m.seq in report["eligible"]


def test_reset_wipes_everything(bus):
    bus.send("alice", "bob", None, "x")
    bus.deliver("bob")
    counts = bus.reset()
    assert counts["messages"] == 1
    assert bus.log() == []
    assert bus.agents() == []
    # Seq numbering restarts on an empty bus.
    assert bus.send("alice", "bob", None, "y").seq == 1


def test_foreign_database_refused(tmp_path):
    """§9.8: a store we cannot understand is never written to."""
    root = tmp_path / "foreign"
    root.mkdir()
    db = root / "bus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE somebody_elses_data (x)")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    b = Bus(root)
    with pytest.raises(StoreError):
        b.conn
    assert db.read_bytes() == before


def test_garbage_file_refused_loudly(tmp_path):
    """§9.3/§9.4 analogue: an unreadable store fails loudly and is left
    untouched; nothing silently recreates it."""
    root = tmp_path / "garbage"
    root.mkdir()
    db = root / "bus.db"
    db.write_bytes(b"this is not a sqlite database, it is a poem")
    before = db.read_bytes()
    b = Bus(root)
    with pytest.raises(StoreError) as exc:
        b.conn
    assert "Refusing" in str(exc.value)
    assert db.read_bytes() == before


def test_newer_schema_refused(bus):
    bus.conn.execute("PRAGMA user_version = 99")
    bus.close()
    b = Bus(bus.root)
    with pytest.raises(StoreError):
        b.conn


def test_agents_distinguishes_three_states(bus):
    """§9.9: never-existed vs has-sent-never-read vs last-read-at."""
    bus.send("alice", "ghost", None, "anyone there?")
    bus.send("alice", "bob", None, "hi bob")
    bus.deliver("bob")
    agents = {a["name"]: a for a in bus.agents()}
    assert agents["ghost"]["last_read"] is None
    assert not agents["ghost"]["has_sent"]           # NEVER SEEN
    assert agents["alice"]["last_read"] is None
    assert agents["alice"]["has_sent"]               # alive, mid-exchange
    assert agents["bob"]["last_read"] is not None    # has read


def test_status_spoke_last(bus):
    bus.send("alice", "bob", None, "ping")
    assert bus.status("alice")["spoke_last"] is True
    assert bus.status("bob")["spoke_last"] is False
    bus.deliver("bob")
    bus.send("bob", "alice", None, "pong")
    assert bus.status("alice")["spoke_last"] is False
    assert bus.status("bob")["spoke_last"] is True
