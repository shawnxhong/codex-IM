"""Durable bridge state in SQLite (design D14).

All calls are synchronous and short; they run on the event loop thread. Every state transition that
must be atomic goes through :meth:`Store.tx`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from .types import InboundMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    message_key   TEXT NOT NULL,
    session_key   TEXT NOT NULL,
    chat_json     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    user_name     TEXT,
    text          TEXT NOT NULL,
    reply_to      TEXT,
    is_group      INTEGER NOT NULL DEFAULT 0,
    composed      INTEGER NOT NULL DEFAULT 0,  -- text already carries sender tags (e.g. /retry)
    kind          TEXT NOT NULL,                -- message | command
    state         TEXT NOT NULL,                -- queued | dispatched | steered | done | ignored
    turn_id       INTEGER,
    received_at   REAL NOT NULL,
    UNIQUE (platform, message_key)
);
CREATE INDEX IF NOT EXISTS inbound_session_state ON inbound (session_key, state, kind);

CREATE TABLE IF NOT EXISTS sessions (
    session_key       TEXT PRIMARY KEY,
    chat_json         TEXT NOT NULL,
    active_thread_id  TEXT,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    project           TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id       TEXT PRIMARY KEY,
    session_key     TEXT NOT NULL,
    state           TEXT NOT NULL,   -- created | materialized | lost | discarded
    cwd             TEXT NOT NULL,
    provider        TEXT NOT NULL,
    title           TEXT,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_active_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_session ON threads (session_key, last_active_at);

CREATE TABLE IF NOT EXISTS turns (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key       TEXT NOT NULL,
    thread_id         TEXT NOT NULL,
    codex_turn_id     TEXT,
    input_text        TEXT NOT NULL,
    state             TEXT NOT NULL,  -- pending | started | reconciling | completed | interrupted | failed | unknown | retried
    final_text        TEXT,
    error             TEXT,
    stream_ref_json   TEXT,
    reply_to          TEXT,
    created_at        REAL NOT NULL,
    completed_at      REAL
);
CREATE INDEX IF NOT EXISTS turns_state ON turns (state);
CREATE INDEX IF NOT EXISTS turns_session ON turns (session_key, id);

CREATE TABLE IF NOT EXISTS outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key         TEXT NOT NULL,
    chat_json           TEXT NOT NULL,
    turn_id             INTEGER,
    kind                TEXT NOT NULL,  -- final | async_question | notice | recovery
    seq                 INTEGER NOT NULL DEFAULT 0,
    text                TEXT NOT NULL,
    edit_ref_json       TEXT,
    reply_to            TEXT,
    state               TEXT NOT NULL,  -- pending | sent | dead
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     REAL NOT NULL,
    platform_message_id TEXT,
    last_error          TEXT,
    created_at          REAL NOT NULL,
    UNIQUE (turn_id, kind, seq)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (state, next_attempt_at);

CREATE TABLE IF NOT EXISTS open_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    turn_id     INTEGER,
    item_id     TEXT,
    created_at  REAL NOT NULL,
    closed_at   REAL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

UNFINISHED_TURN_STATES = ("pending", "started", "reconciling")
RETRYABLE_TURN_STATES = ("interrupted", "unknown", "failed")


@dataclass(slots=True)
class OutboxRow:
    kind: str
    seq: int
    text: str
    edit_ref_json: str | None = None
    reply_to: str | None = None


class Store:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _all(self, sql: str, args: Sequence[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args: Sequence[object] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _exec(self, sql: str, args: Sequence[object] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str) -> str | None:
        row = self._one("SELECT v FROM kv WHERE k = ?", (key,))
        return None if row is None else str(row["v"])

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    # ------------------------------------------------------------------ inbound
    def insert_inbound(
        self,
        msg: InboundMessage,
        *,
        kind: str,
        state: str = "queued",
        composed: bool = False,
    ) -> int | None:
        """Insert unless (platform, message_key) already exists. Returns the new row id, or None if duplicate."""
        cur = self._exec(
            """
            INSERT OR IGNORE INTO inbound
                (platform, message_key, session_key, chat_json, user_id, user_name, text, reply_to,
                 is_group, composed, kind, state, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.platform,
                msg.message_key,
                msg.chat.session_key.encode(),
                msg.chat.to_json(),
                msg.user_id,
                msg.user_name,
                msg.text,
                msg.reply_to,
                1 if msg.is_group else 0,
                1 if composed else 0,
                kind,
                state,
                msg.received_at,
            ),
        )
        if cur.rowcount != 1:
            return None
        return int(cur.lastrowid)

    def queued_messages(self, session_key: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM inbound WHERE session_key = ? AND kind = 'message' AND state = 'queued' ORDER BY id",
            (session_key,),
        )

    def sessions_with_queued(self) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT session_key FROM inbound WHERE kind = 'message' AND state = 'queued'"
        )
        return [str(r["session_key"]) for r in rows]

    def set_inbound_state(self, ids: Sequence[int], state: str, turn_id: int | None = None) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self._exec(
            f"UPDATE inbound SET state = ?, turn_id = COALESCE(?, turn_id) WHERE id IN ({marks})",
            (state, turn_id, *ids),
        )

    def mark_stale_commands(self) -> int:
        cur = self._exec("UPDATE inbound SET state = 'ignored' WHERE kind = 'command' AND state = 'queued'")
        return cur.rowcount

    # ------------------------------------------------------------------ sessions
    def get_session(self, session_key: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM sessions WHERE session_key = ?", (session_key,))

    def upsert_session(
        self, session_key: str, chat_json: str, *, provider: str, model: str, project: str | None
    ) -> sqlite3.Row:
        now = time.time()
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_key = ?", (session_key,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sessions (session_key, chat_json, provider, model, project, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_key, chat_json, provider, model, project, now, now),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET chat_json = ?, updated_at = ? WHERE session_key = ?",
                    (chat_json, now, session_key),
                )
            return conn.execute("SELECT * FROM sessions WHERE session_key = ?", (session_key,)).fetchone()

    def set_active_thread(self, session_key: str, thread_id: str | None) -> None:
        self._exec(
            "UPDATE sessions SET active_thread_id = ?, updated_at = ? WHERE session_key = ?",
            (thread_id, time.time(), session_key),
        )

    def set_session_model(self, session_key: str, model: str) -> None:
        self._exec("UPDATE sessions SET model = ?, updated_at = ? WHERE session_key = ?", (model, time.time(), session_key))

    def set_session_provider(self, session_key: str, provider: str) -> None:
        self._exec(
            "UPDATE sessions SET provider = ?, updated_at = ? WHERE session_key = ?", (provider, time.time(), session_key)
        )

    # ------------------------------------------------------------------ threads
    def insert_thread(self, thread_id: str, session_key: str, *, cwd: str, provider: str, title: str | None) -> None:
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO threads (thread_id, session_key, state, cwd, provider, title, turn_count, created_at, last_active_at)
                VALUES (?, ?, 'created', ?, ?, ?, 0, ?, ?)
                """,
                (thread_id, session_key, cwd, provider, title, now, now),
            )
            conn.execute(
                "UPDATE sessions SET active_thread_id = ?, updated_at = ? WHERE session_key = ?",
                (thread_id, now, session_key),
            )

    def get_thread(self, thread_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))

    def set_thread_state(self, thread_id: str, state: str) -> None:
        self._exec("UPDATE threads SET state = ? WHERE thread_id = ?", (state, thread_id))

    def list_threads(self, session_key: str, limit: int = 10) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT * FROM threads WHERE session_key = ? AND state != 'discarded'
            ORDER BY last_active_at DESC, created_at DESC LIMIT ?
            """,
            (session_key, limit),
        )

    # ------------------------------------------------------------------ turns
    def create_turn(
        self, session_key: str, thread_id: str, input_text: str, inbound_ids: Sequence[int], reply_to: str | None
    ) -> int:
        now = time.time()
        with self.tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO turns (session_key, thread_id, input_text, state, reply_to, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (session_key, thread_id, input_text, reply_to, now),
            )
            turn_id = int(cur.lastrowid)
            if inbound_ids:
                marks = ",".join("?" for _ in inbound_ids)
                conn.execute(
                    f"UPDATE inbound SET state = 'dispatched', turn_id = ? WHERE id IN ({marks})",
                    (turn_id, *inbound_ids),
                )
            return turn_id

    def set_turn_started(self, turn_id: int, codex_turn_id: str) -> None:
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                "UPDATE turns SET codex_turn_id = ?, state = 'started' WHERE id = ?", (codex_turn_id, turn_id)
            )
            row = conn.execute("SELECT thread_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is not None:
                conn.execute(
                    """
                    UPDATE threads SET turn_count = turn_count + 1, last_active_at = ?,
                        state = CASE WHEN state = 'created' THEN 'materialized' ELSE state END
                    WHERE thread_id = ?
                    """,
                    (now, row["thread_id"]),
                )

    def set_turn_stream_ref(self, turn_id: int, ref_json: str) -> None:
        self._exec("UPDATE turns SET stream_ref_json = ? WHERE id = ?", (ref_json, turn_id))

    def set_turn_state(self, turn_id: int, state: str, error: str | None = None) -> None:
        self._exec("UPDATE turns SET state = ?, error = COALESCE(?, error) WHERE id = ?", (state, error, turn_id))

    def get_turn(self, turn_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM turns WHERE id = ?", (turn_id,))

    def has_codex_turn(self, codex_turn_id: str) -> bool:
        return self._one("SELECT 1 FROM turns WHERE codex_turn_id = ?", (codex_turn_id,)) is not None

    def unfinished_turns(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in UNFINISHED_TURN_STATES)
        return self._all(f"SELECT * FROM turns WHERE state IN ({marks}) ORDER BY id", UNFINISHED_TURN_STATES)

    def last_retryable_turn(self, session_key: str) -> sqlite3.Row | None:
        row = self._one("SELECT * FROM turns WHERE session_key = ? ORDER BY id DESC LIMIT 1", (session_key,))
        if row is None or row["state"] not in RETRYABLE_TURN_STATES:
            return None
        return row

    def finish_turn(
        self,
        turn_id: int,
        *,
        state: str,
        final_text: str,
        error: str | None,
        session_key: str,
        chat_json: str,
        rows: Sequence[OutboxRow],
    ) -> None:
        """Record the outcome and its outbox rows in one transaction (deliver after persist)."""
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                "UPDATE turns SET state = ?, final_text = ?, error = ?, completed_at = ? WHERE id = ?",
                (state, final_text, error, now, turn_id),
            )
            conn.execute("UPDATE inbound SET state = 'done' WHERE turn_id = ? AND state = 'dispatched'", (turn_id,))
            for row in rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outbox
                        (session_key, chat_json, turn_id, kind, seq, text, edit_ref_json, reply_to,
                         state, attempts, next_attempt_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (session_key, chat_json, turn_id, row.kind, row.seq, row.text, row.edit_ref_json, row.reply_to, now, now),
                )

    # ------------------------------------------------------------------ outbox
    def add_outbox(
        self,
        *,
        session_key: str,
        chat_json: str,
        kind: str,
        text: str,
        turn_id: int | None = None,
        seq: int = 0,
        reply_to: str | None = None,
        edit_ref_json: str | None = None,
    ) -> int | None:
        now = time.time()
        cur = self._exec(
            """
            INSERT OR IGNORE INTO outbox
                (session_key, chat_json, turn_id, kind, seq, text, edit_ref_json, reply_to,
                 state, attempts, next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (session_key, chat_json, turn_id, kind, seq, text, edit_ref_json, reply_to, now, now),
        )
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def due_outbox(self, now: float, limit: int = 100) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM outbox WHERE state = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (now, limit),
        )

    def pending_outbox_for_sessions(self) -> set[str]:
        rows = self._all("SELECT DISTINCT session_key FROM outbox WHERE state = 'pending'")
        return {str(r["session_key"]) for r in rows}

    def next_outbox_due(self) -> float | None:
        row = self._one("SELECT MIN(next_attempt_at) AS t FROM outbox WHERE state = 'pending'")
        if row is None or row["t"] is None:
            return None
        return float(row["t"])

    def mark_outbox_sent(self, outbox_id: int, platform_message_id: str | None) -> None:
        self._exec(
            "UPDATE outbox SET state = 'sent', platform_message_id = ?, last_error = NULL WHERE id = ?",
            (platform_message_id, outbox_id),
        )

    def mark_outbox_retry(self, outbox_id: int, attempts: int, next_attempt_at: float, error: str) -> None:
        self._exec(
            "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (attempts, next_attempt_at, error[:500], outbox_id),
        )

    def mark_outbox_dead(self, outbox_id: int, error: str) -> None:
        self._exec("UPDATE outbox SET state = 'dead', last_error = ? WHERE id = ?", (error[:500], outbox_id))

    def get_outbox(self, outbox_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM outbox WHERE id = ?", (outbox_id,))

    # ------------------------------------------------------------------ open questions
    def add_open_question(self, thread_id: str, turn_id: int | None, item_id: str | None) -> None:
        self._exec(
            "INSERT INTO open_questions (thread_id, turn_id, item_id, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, turn_id, item_id, time.time()),
        )

    def close_open_questions(self, thread_id: str) -> int:
        cur = self._exec(
            "UPDATE open_questions SET closed_at = ? WHERE thread_id = ? AND closed_at IS NULL",
            (time.time(), thread_id),
        )
        return cur.rowcount

    def open_question_count(self, thread_id: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM open_questions WHERE thread_id = ? AND closed_at IS NULL", (thread_id,)
        )
        return 0 if row is None else int(row["n"])
