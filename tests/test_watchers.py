"""Watcher registry: the single-reader-per-name enforcement (§9.5) and
heartbeat-based liveness (no pid recycling hazard)."""

import time

import pytest

from ccbus.store import WatcherConflict


def test_second_live_watcher_refused(bus):
    bus.watcher_register("bob", pid=111, interval=2)
    assert bus.live_watcher("bob")  # premise: the first slot is live
    with pytest.raises(WatcherConflict) as exc:
        bus.watcher_register("bob", pid=222, interval=2)
    assert exc.value.pid == 111


def test_same_pid_may_reregister(bus):
    bus.watcher_register("bob", pid=111, interval=2)
    bus.watcher_register("bob", pid=111, interval=5)  # no raise
    assert bus.live_watcher("bob")["interval"] == 5


def test_stale_watcher_slot_can_be_taken(bus):
    bus.watcher_register("bob", pid=111, interval=2)
    # Age the heartbeat past the staleness window instead of sleeping.
    bus.conn.execute("UPDATE watchers SET heartbeat = ? WHERE reader = 'bob'",
                     (time.time() - 60,))
    assert bus.live_watcher("bob") is None
    bus.watcher_register("bob", pid=222, interval=2)
    assert bus.live_watcher("bob")["pid"] == 222


def test_beat_fails_after_takeover(bus):
    bus.watcher_register("bob", pid=111, interval=2)
    bus.conn.execute("UPDATE watchers SET heartbeat = ? WHERE reader = 'bob'",
                     (time.time() - 60,))
    bus.watcher_register("bob", pid=222, interval=2)
    assert bus.watcher_beat("bob", 111) is False  # old owner told, not silent
    assert bus.watcher_beat("bob", 222) is True


def test_unregister_only_removes_own_slot(bus):
    bus.watcher_register("bob", pid=111, interval=2)
    bus.watcher_unregister("bob", pid=999)  # someone else's pid: no-op
    assert bus.live_watcher("bob")["pid"] == 111
    bus.watcher_unregister("bob", pid=111)
    assert bus.live_watcher("bob") is None


def test_watcher_visible_in_agents_and_status(bus):
    bus.send("alice", "bob", None, "seed")
    bus.watcher_register("bob", pid=111, interval=2)
    agents = {a["name"]: a for a in bus.agents()}
    assert agents["bob"]["watching"] is True
    assert bus.status("bob")["watcher"]["pid"] == 111
