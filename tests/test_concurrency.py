"""§9.5: concurrent readers under one name must not silently split or lose
traffic. Delivery is transactional, so N racing readers see each message
exactly once *in total*, and the audit trail says which pid got it."""

import threading

from ccbus.store import Bus


def test_concurrent_deliver_no_loss_no_duplication(root):
    writer = Bus(root)
    total = 200
    for i in range(total):
        writer.send("alice", "bob", None, f"m{i}")
    writer.close()

    results: list[list] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def reader(idx: int):
        b = Bus(root)
        got = []
        try:
            barrier.wait()
            for _ in range(20):
                got.extend(m.body for m in b.deliver("bob", pid=idx))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)
        finally:
            b.close()
            results.append(got)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    combined = [body for got in results for body in got]
    assert sorted(combined) == sorted(f"m{i}" for i in range(total))
    assert len(combined) == len(set(combined))


def test_concurrent_send_and_deliver(root):
    """Senders racing a reader: everything sent is eventually delivered
    exactly once, and seq order is strictly increasing per read batch."""
    stop = threading.Event()
    sent: list[str] = []

    def sender():
        b = Bus(root)
        for i in range(100):
            b.send("alice", "bob", None, f"s{i}")
            sent.append(f"s{i}")
        b.close()
        stop.set()

    delivered: list = []

    def reader():
        b = Bus(root)
        while not stop.is_set() or b.pending("bob"):
            batch = b.deliver("bob")
            assert all(x.seq < y.seq for x, y in zip(batch, batch[1:]))
            delivered.extend(m.body for m in batch)
        b.close()

    ts, tr = threading.Thread(target=sender), threading.Thread(target=reader)
    ts.start(); tr.start()
    ts.join(); tr.join()

    assert sorted(delivered) == sorted(sent)
    assert len(sent) == 100  # premise check: the sender actually ran
