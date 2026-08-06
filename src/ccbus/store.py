"""SQLite-backed store: messages, deliveries, watchers, and guard state.

One database file holds everything. The schema replaces the prototype's
file-per-message design and its cursor machinery:

- ``messages.seq`` (AUTOINCREMENT) is the total order. Insertion order under
  the database's write serialization *is* the delivery order, so there is no
  timestamp-derived id, no clock-skew window, and no settling margin.
- Delivery is a row per (message, reader) in ``deliveries``, written in the
  same immediate transaction that selected the message. Exactly-once per
  reader holds under any mix of filtered and unfiltered reads, and under
  concurrent readers, because the primary key and the transaction make a
  second delivery of the same message to the same name impossible.
- A reader that has never run simply has no delivery rows, so its first read
  returns the full history addressed to it -- including broadcasts sent
  before it existed.

Every write path either commits or raises; no error path may leave the
caller believing a write happened. A database this module cannot understand
raises :class:`StoreError` and is never written to.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
OFFER_TTL_SECONDS = 3600
# A watcher is live while its heartbeat is younger than this many of its own
# poll intervals (with a floor for very short intervals, so one delayed poll
# does not read as death).
WATCHER_STALE_INTERVALS = 3
WATCHER_STALE_FLOOR_SECONDS = 10.0

_SCHEMA = f"""
CREATE TABLE messages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    sender      TEXT NOT NULL CHECK (sender <> '' AND sender <> '*'),
    recipient   TEXT NOT NULL CHECK (recipient <> ''),
    topic       TEXT NOT NULL DEFAULT '' CHECK (topic <> '*'),
    body        TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX idx_messages_live ON messages(recipient, seq) WHERE archived_at IS NULL;

CREATE TABLE deliveries (
    seq          INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
    reader       TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    pid          INTEGER,
    PRIMARY KEY (seq, reader)
) WITHOUT ROWID;
CREATE INDEX idx_deliveries_reader ON deliveries(reader, seq);

CREATE TABLE watchers (
    reader     TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat  REAL NOT NULL,
    interval   REAL NOT NULL,
    topics     TEXT
);

CREATE TABLE offers (
    dir     TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    made_at REAL NOT NULL
);

CREATE TABLE bindings (
    session_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    dir        TEXT NOT NULL,
    bound_at   REAL NOT NULL
);

CREATE TABLE guard_stamps (
    session_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    state      TEXT NOT NULL
);

PRAGMA user_version = {SCHEMA_VERSION};
"""

_OUR_TABLES = {"messages", "deliveries", "watchers", "offers", "bindings",
               "guard_stamps"}


class StoreError(Exception):
    """The store is unreadable or not ours. Never write after raising this."""


class UserError(Exception):
    """Bad input from the caller; the store is fine."""


class WatcherConflict(Exception):
    """A live watcher already holds this reader name."""

    def __init__(self, pid: int, age: float):
        self.pid = pid
        self.age = age
        super().__init__(f"live watcher pid {pid}, heartbeat {age:.0f}s ago")


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def norm_name(name: str | None) -> str:
    """One canonical spelling per agent name: trimmed, case-insensitive.

    ``Alice`` and ``alice`` addressing two different mailboxes is a silent
    split-brain with a typo as the trigger, same failure shape the prototype
    normalised topics to avoid.
    """
    return (name or "").strip().casefold()


def require_name(name: str | None) -> str:
    """Normalise and validate a name at every write boundary."""
    n = norm_name(name)
    if not _NAME_RE.match(n):
        raise UserError(f"{name!r} is not a usable agent name "
                        f"(lowercase letters, digits, . _ - only)")
    return n


def norm_topic(topic: str | None) -> str:
    return (topic or "").strip().casefold()


def norm_dir(path: str | os.PathLike) -> str:
    """Canonical directory spelling for offer/binding matching.

    The prototype compared raw strings, so a trailing separator or drive-case
    difference meant an offer that never bound -- and never-binds is
    indistinguishable from unclaimed.
    """
    return os.path.normcase(str(Path(path).resolve()))


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_root() -> Path:
    env = os.environ.get("CCBUS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude-code-bus"


@dataclass(frozen=True)
class Msg:
    seq: int
    ts: str
    sender: str
    recipient: str
    topic: str
    body: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "from": self.sender,
                "to": self.recipient, "topic": self.topic, "text": self.body}


def _msg(row: sqlite3.Row) -> Msg:
    return Msg(seq=row["seq"], ts=row["ts"], sender=row["sender"],
               recipient=row["recipient"], topic=row["topic"], body=row["body"])


class Bus:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else default_root()
        self.db_path = self.root / "bus.db"
        self._conn: sqlite3.Connection | None = None

    # -- connection ---------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def _open(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10,
                               isolation_level=None)  # explicit transactions
        conn.row_factory = sqlite3.Row
        try:
            # Ownership checks first: everything up to the WAL switch is
            # read-only, so a file that turns out not to be ours is returned
            # byte-identical.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                existing = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if existing - {"sqlite_sequence"}:
                    # A populated database that is not ours. Adding our tables
                    # to it would entangle two applications in one file.
                    raise StoreError(
                        f"{self.db_path} is a database, but not a bus "
                        f"(tables: {', '.join(sorted(existing))}). Refusing "
                        f"to write to it. Point CCBUS_DIR elsewhere or move "
                        f"the file.")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
            elif version > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.db_path} has schema version {version}; this tool "
                    f"understands up to {SCHEMA_VERSION}. Upgrade the tool "
                    f"instead of downgrading the bus.")
            else:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError as e:
            conn.close()
            raise StoreError(
                f"cannot open {self.db_path} ({e}). Refusing to touch it. "
                f"If it is corrupt, move it aside and re-run; the bus starts "
                f"empty. Nothing has been written.") from e
        except StoreError:
            conn.close()
            raise
        return conn

    def _txn(self):
        """Immediate transaction: acquires the write lock up front so
        select-then-write sequences are serialized against other writers."""
        return _Txn(self.conn)

    # -- messages -----------------------------------------------------------

    def send(self, sender: str, recipient: str, topic: str | None,
             body: str) -> Msg:
        sender = require_name(sender)
        recipient = norm_name(recipient)
        if recipient != "*":
            recipient = require_name(recipient)
        topic = norm_topic(topic)
        if topic == "*":
            raise UserError("'*' is not a valid topic name")
        ts = utcnow_iso()
        with self._txn():
            cur = self.conn.execute(
                "INSERT INTO messages (ts, sender, recipient, topic, body) "
                "VALUES (?, ?, ?, ?, ?)", (ts, sender, recipient, topic, body))
            seq = cur.lastrowid
        return Msg(seq=seq, ts=ts, sender=sender, recipient=recipient,
                   topic=topic, body=body)

    def _pending_sql(self, topics: list[str] | None) -> tuple[str, list]:
        sql = ("SELECT * FROM messages m WHERE m.archived_at IS NULL "
               "AND m.recipient IN (?, '*') AND m.sender <> ? "
               "AND NOT EXISTS (SELECT 1 FROM deliveries d "
               "                WHERE d.seq = m.seq AND d.reader = ?)")
        params: list = []
        if topics is not None:
            sql += f" AND m.topic IN ({','.join('?' * len(topics))})"
            params = list(topics)
        sql += " ORDER BY m.seq"
        return sql, params

    def pending(self, reader: str, topics: list[str] | None = None) -> list[Msg]:
        """Undelivered messages for ``reader``; does not mark anything."""
        reader = norm_name(reader)
        topics = [norm_topic(t) for t in topics] if topics else None
        sql, extra = self._pending_sql(topics)
        rows = self.conn.execute(sql, [reader, reader, reader] + extra)
        return [_msg(r) for r in rows]

    def deliver(self, reader: str, topics: list[str] | None = None,
                pid: int | None = None) -> list[Msg]:
        """Select pending messages and mark them delivered, atomically.

        The select and the delivery rows commit together, so a concurrent
        reader under the same name sees either nothing (rows already marked)
        or everything (transaction not yet begun) -- never a split.
        """
        reader = require_name(reader)
        topics = [norm_topic(t) for t in topics] if topics else None
        sql, extra = self._pending_sql(topics)
        now = utcnow_iso()
        with self._txn():
            rows = self.conn.execute(
                sql, [reader, reader, reader] + extra).fetchall()
            self.conn.executemany(
                "INSERT INTO deliveries (seq, reader, delivered_at, pid) "
                "VALUES (?, ?, ?, ?)",
                [(r["seq"], reader, now, pid) for r in rows])
        return [_msg(r) for r in rows]

    def pending_elsewhere(self, reader: str,
                          topics: list[str]) -> dict[str, int]:
        """Per-topic counts of pending mail on topics a filtered read skipped."""
        reader = norm_name(reader)
        wanted = {norm_topic(t) for t in topics}
        counts: dict[str, int] = {}
        for m in self.pending(reader):
            if m.topic not in wanted:
                counts[m.topic] = counts.get(m.topic, 0) + 1
        return counts

    def log(self, limit: int = 50, topics: list[str] | None = None,
            include_archived: bool = False) -> list[Msg]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if not include_archived:
            sql += " AND archived_at IS NULL"
        if topics:
            normed = [norm_topic(t) for t in topics]
            sql += f" AND topic IN ({','.join('?' * len(normed))})"
            params += normed
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_msg(r) for r in reversed(rows)]

    def trace(self, seq: int) -> dict | None:
        """Full delivery audit for one message: who got it, when, which pid.

        This is what turns "a message vanished" from a mystery into a lookup.
        """
        row = self.conn.execute(
            "SELECT * FROM messages WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            return None
        deliveries = [dict(r) for r in self.conn.execute(
            "SELECT reader, delivered_at, pid FROM deliveries "
            "WHERE seq = ? ORDER BY delivered_at", (seq,))]
        delivered_to = {d["reader"] for d in deliveries}
        if row["recipient"] == "*":
            expected = self.known_readers() - {row["sender"]}
        else:
            expected = {row["recipient"]}
        return {"message": _msg(row).to_dict(),
                "archived_at": row["archived_at"],
                "deliveries": deliveries,
                "awaiting": sorted(expected - delivered_to)}

    # -- roster -------------------------------------------------------------

    def known_readers(self) -> set[str]:
        """Names that have ever successfully read anything."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT reader FROM deliveries")}

    def agents(self) -> list[dict]:
        c = self.conn
        senders = {r[0] for r in c.execute("SELECT DISTINCT sender FROM messages")}
        recipients = {r[0] for r in c.execute(
            "SELECT DISTINCT recipient FROM messages WHERE recipient <> '*'")}
        readers = self.known_readers()
        bound = {r[0] for r in c.execute("SELECT DISTINCT name FROM bindings")}
        offered = {r[0] for r in c.execute("SELECT DISTINCT name FROM offers")}
        watching = {r[0] for r in c.execute("SELECT reader FROM watchers")}
        out = []
        for name in sorted(senders | recipients | readers | bound | offered
                           | watching):
            last_read = c.execute(
                "SELECT MAX(delivered_at) FROM deliveries WHERE reader = ?",
                (name,)).fetchone()[0]
            watcher = self.live_watcher(name)
            out.append({
                "name": name,
                "unread": len(self.pending(name)),
                "last_read": last_read,
                "has_sent": name in senders,
                "watching": bool(watcher),
            })
        return out

    def topics(self) -> list[dict]:
        c = self.conn
        readers = sorted(self.known_readers() | {
            r[0] for r in c.execute(
                "SELECT DISTINCT recipient FROM messages WHERE recipient <> '*'")})
        rows = c.execute(
            "SELECT topic, COUNT(*) AS n, MAX(ts) AS last_ts, MAX(seq) AS last_seq "
            "FROM messages WHERE archived_at IS NULL GROUP BY topic "
            "ORDER BY topic").fetchall()
        out = []
        for row in rows:
            unread = {r: len(self.pending(r, [row["topic"]])) for r in readers}
            out.append({"topic": row["topic"], "messages": row["n"],
                        "last_ts": row["last_ts"], "last_seq": row["last_seq"],
                        "unread": unread})
        return out

    def status(self, name: str) -> dict:
        """One-shot state for the stop guard."""
        name = norm_name(name)
        c = self.conn
        pending = self.pending(name)
        mine = c.execute(
            "SELECT MAX(seq) FROM messages WHERE sender = ? "
            "AND archived_at IS NULL", (name,)).fetchone()[0] or 0
        theirs = c.execute(
            "SELECT MAX(seq) FROM messages WHERE recipient IN (?, '*') "
            "AND sender <> ? AND archived_at IS NULL",
            (name, name)).fetchone()[0] or 0
        last = c.execute(
            "SELECT MAX(seq) FROM messages WHERE archived_at IS NULL"
        ).fetchone()[0] or 0
        watcher = self.live_watcher(name)
        return {
            "me": name,
            "bus": str(self.root),
            "unread": len(pending),
            "pending": [m.seq for m in pending],
            "last_seq": last,
            # True means *I* am owed a reply: computed against mail addressed
            # to me, so third parties talking to each other do not clear it.
            "spoke_last": mine > 0 and mine > theirs,
            "watcher": watcher,
        }

    # -- watchers -----------------------------------------------------------

    def stale_after(self, interval: float) -> float:
        return max(WATCHER_STALE_INTERVALS * interval,
                   WATCHER_STALE_FLOOR_SECONDS)

    def live_watcher(self, reader: str) -> dict | None:
        reader = norm_name(reader)
        row = self.conn.execute(
            "SELECT * FROM watchers WHERE reader = ?", (reader,)).fetchone()
        if row is None:
            return None
        age = time.time() - row["heartbeat"]
        if age > self.stale_after(row["interval"]):
            return None
        return {"pid": row["pid"], "started_at": row["started_at"],
                "heartbeat_age": age, "interval": row["interval"],
                "topics": json.loads(row["topics"]) if row["topics"] else None}

    def watcher_register(self, reader: str, pid: int, interval: float,
                         topics: list[str] | None = None) -> None:
        """Claim the single watcher slot for ``reader``.

        A second live watcher under one name is refused, not tolerated: two
        readers splitting one mailbox silently ate addressed messages three
        times in one day of the prototype's life. Liveness is the heartbeat,
        not the pid -- a heartbeat cannot be recycled by an unrelated process.
        """
        reader = require_name(reader)
        now = time.time()
        with self._txn():
            row = self.conn.execute(
                "SELECT * FROM watchers WHERE reader = ?", (reader,)).fetchone()
            if row is not None and row["pid"] != pid:
                age = now - row["heartbeat"]
                if age <= self.stale_after(row["interval"]):
                    raise WatcherConflict(row["pid"], age)
            self.conn.execute(
                "INSERT OR REPLACE INTO watchers "
                "(reader, pid, started_at, heartbeat, interval, topics) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reader, pid, utcnow_iso(), now, interval,
                 json.dumps([norm_topic(t) for t in topics]) if topics else None))

    def watcher_beat(self, reader: str, pid: int) -> bool:
        """Refresh the heartbeat. False means the slot is no longer ours."""
        reader = norm_name(reader)
        with self._txn():
            cur = self.conn.execute(
                "UPDATE watchers SET heartbeat = ? WHERE reader = ? AND pid = ?",
                (time.time(), reader, pid))
        return cur.rowcount > 0

    def watcher_unregister(self, reader: str, pid: int) -> None:
        with self._txn():
            self.conn.execute(
                "DELETE FROM watchers WHERE reader = ? AND pid = ?",
                (norm_name(reader), pid))

    # -- claims / bindings (stop guard) -------------------------------------

    def offer(self, directory: str, name: str) -> str:
        """Offer ``name`` to the next session that stops in ``directory``.

        Offers are keyed per directory, so claims for different projects
        coexist -- the prototype had one offer for the whole bus and a second
        claim silently orphaned the first.
        """
        name = require_name(name)
        if not Path(directory).is_dir():
            # An offer for a directory that can never be a session's cwd never
            # binds, and the session that made it believes it is guarded.
            raise UserError(
                f"{directory} is not an existing directory; refusing to "
                f"claim it. Check for shell-mangled backslashes and pass the "
                f"project root again (forward slashes are safe).")
        ndir = norm_dir(directory)
        with self._txn():
            self._prune_offers()
            self.conn.execute(
                "INSERT OR REPLACE INTO offers (dir, name, made_at) "
                "VALUES (?, ?, ?)", (ndir, name, time.time()))
        return ndir

    def _prune_offers(self) -> None:
        self.conn.execute("DELETE FROM offers WHERE made_at < ?",
                          (time.time() - OFFER_TTL_SECONDS,))

    def binding_for(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM bindings WHERE session_id = ?",
            (session_id,)).fetchone()
        return dict(row) if row else None

    def take_offer(self, directory: str, session_id: str) -> dict | None:
        """Bind the offer for ``directory`` to ``session_id``, atomically.

        Two sessions stopping together in one directory race here; the
        transaction guarantees exactly one wins and the loser sees no offer.
        """
        ndir = norm_dir(directory)
        with self._txn():
            row = self.conn.execute(
                "SELECT * FROM offers WHERE dir = ? AND made_at >= ?",
                (ndir, time.time() - OFFER_TTL_SECONDS)).fetchone()
            if row is None:
                return None
            self.conn.execute("DELETE FROM offers WHERE dir = ?", (ndir,))
            self.conn.execute(
                "INSERT OR REPLACE INTO bindings "
                "(session_id, name, dir, bound_at) VALUES (?, ?, ?, ?)",
                (session_id, row["name"], ndir, time.time()))
        return {"session_id": session_id, "name": row["name"], "dir": ndir}

    def release(self, name: str) -> dict:
        """Free a name: drop every binding and offer holding it."""
        name = norm_name(name)
        with self._txn():
            bindings = [r["session_id"] for r in self.conn.execute(
                "SELECT session_id FROM bindings WHERE name = ?", (name,))]
            offers = [r["dir"] for r in self.conn.execute(
                "SELECT dir FROM offers WHERE name = ?", (name,))]
            self.conn.execute("DELETE FROM bindings WHERE name = ?", (name,))
            self.conn.execute("DELETE FROM offers WHERE name = ?", (name,))
            self.conn.execute("DELETE FROM guard_stamps WHERE name = ?", (name,))
        return {"bindings": bindings, "offers": offers}

    def stamp_get(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM guard_stamps WHERE session_id = ?",
            (session_id,)).fetchone()
        return row["state"] if row else None

    def stamp_set(self, session_id: str, name: str, state: str) -> None:
        with self._txn():
            self.conn.execute(
                "INSERT OR REPLACE INTO guard_stamps (session_id, name, state) "
                "VALUES (?, ?, ?)", (session_id, norm_name(name), state))

    # -- maintenance --------------------------------------------------------

    def gc(self, age_days: int, force: bool) -> dict:
        """Archive old, fully delivered messages (soft: sets ``archived_at``).

        A message qualifies only when older than ``age_days`` and its
        recipient has a delivery row -- every known reader's, for a
        broadcast. Undelivered mail is never archived, and everything kept
        back is counted by name: a quiet gc that skipped something would be
        indistinguishable from one that covered everything.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=age_days)
                  ).isoformat(timespec="seconds")
        readers = self.known_readers()
        eligible: list[int] = []
        kept: dict[str, int] = {}
        rows = self.conn.execute(
            "SELECT m.*, (SELECT GROUP_CONCAT(reader) FROM deliveries d "
            "             WHERE d.seq = m.seq) AS readers "
            "FROM messages m WHERE m.archived_at IS NULL AND m.ts < ? "
            "ORDER BY m.seq", (cutoff,)).fetchall()
        for row in rows:
            got = set(row["readers"].split(",")) if row["readers"] else set()
            if row["recipient"] == "*":
                expected = readers - {row["sender"]}
                missing = expected - got if expected else {"(nobody has ever read)"}
            else:
                missing = {row["recipient"]} - got
            if missing:
                for name in missing:
                    kept[name] = kept.get(name, 0) + 1
            else:
                eligible.append(row["seq"])
        if force and eligible:
            now = utcnow_iso()
            with self._txn():
                self.conn.executemany(
                    "UPDATE messages SET archived_at = ? WHERE seq = ?",
                    [(now, s) for s in eligible])
        return {"eligible": eligible, "kept": kept, "forced": force,
                "age_days": age_days}

    def reset(self) -> dict:
        counts = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in sorted(_OUR_TABLES)}
        with self._txn():
            for t in _OUR_TABLES:
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
        self.conn.execute("VACUUM")
        return counts

    def doctor(self) -> dict:
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        watchers = []
        for row in self.conn.execute("SELECT * FROM watchers"):
            age = time.time() - row["heartbeat"]
            watchers.append({"reader": row["reader"], "pid": row["pid"],
                             "heartbeat_age": age,
                             "live": age <= self.stale_after(row["interval"])})
        return {
            "bus": str(self.root),
            "db": str(self.db_path),
            "integrity": integrity,
            "messages": self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE archived_at IS NULL"
            ).fetchone()[0],
            "archived": self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE archived_at IS NOT NULL"
            ).fetchone()[0],
            "watchers": watchers,
            "offers": [dict(r) for r in self.conn.execute("SELECT * FROM offers")],
            "bindings": [dict(r) for r in self.conn.execute(
                "SELECT * FROM bindings")],
        }


class _Txn:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK`` context manager."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
