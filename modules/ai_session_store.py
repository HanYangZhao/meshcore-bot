#!/usr/bin/env python3
"""Per-sender AI conversation history backed by the bot's SQLite database."""

import sqlite3
import time
from contextlib import contextmanager
from typing import Generator


class AiSessionStore:
    """Read/write per-sender LLM conversation history from the ai_sessions table.

    Session key is sender_pubkey. Each row is one turn (role + content).
    History is ordered by turn_order and capped at max_history total rows per sender.
    Rows older than expire_after seconds (measured from the latest turn) are treated
    as expired — get() returns [] and prune_expired() removes them.
    """

    def __init__(
        self,
        db_path: str,
        max_history: int = 20,
        expire_after: int = 86400,
    ) -> None:
        self.db_path = db_path
        self.max_history = max_history
        self.expire_after = expire_after

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get(self, pubkey: str) -> list[dict]:
        """Return history for sender as [{role, content}, ...], oldest first.

        Returns [] if the session is absent or expired.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT MAX(updated_at) FROM ai_sessions WHERE pubkey = ?",
                (pubkey,),
            )
            row = cursor.fetchone()
            if not row or row[0] is None:
                return []
            latest = row[0]
            if time.time() - latest > self.expire_after:
                return []
            cursor = conn.execute(
                "SELECT role, content FROM ai_sessions"
                " WHERE pubkey = ? ORDER BY turn_order ASC",
                (pubkey,),
            )
            return [{"role": r["role"], "content": r["content"]} for r in cursor.fetchall()]

    def append(self, pubkey: str, role: str, content: str) -> None:
        """Append one turn and trim history to max_history rows for this sender."""
        now = int(time.time())
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT COALESCE(MAX(turn_order), 0) FROM ai_sessions WHERE pubkey = ?",
                (pubkey,),
            )
            next_order = (cursor.fetchone()[0] or 0) + 1
            conn.execute(
                "INSERT INTO ai_sessions (pubkey, role, content, updated_at, turn_order)"
                " VALUES (?, ?, ?, ?, ?)",
                (pubkey, role, content, now, next_order),
            )
            # Trim oldest rows beyond max_history
            conn.execute(
                "DELETE FROM ai_sessions WHERE pubkey = ? AND turn_order NOT IN ("
                "  SELECT turn_order FROM ai_sessions WHERE pubkey = ?"
                "  ORDER BY turn_order DESC LIMIT ?"
                ")",
                (pubkey, pubkey, self.max_history),
            )
            conn.commit()

    def reset(self, pubkey: str) -> None:
        """Delete all history for this sender."""
        with self._conn() as conn:
            conn.execute("DELETE FROM ai_sessions WHERE pubkey = ?", (pubkey,))
            conn.commit()

    def prune_expired(self) -> int:
        """Delete all rows whose session has been inactive for longer than expire_after.

        Returns the number of rows deleted.
        """
        cutoff = int(time.time()) - self.expire_after
        with self._conn() as conn:
            # Find pubkeys whose latest turn is before the cutoff
            cursor = conn.execute(
                "SELECT pubkey FROM ai_sessions"
                " GROUP BY pubkey HAVING MAX(updated_at) < ?",
                (cutoff,),
            )
            expired = [row[0] for row in cursor.fetchall()]
            if not expired:
                return 0
            placeholders = ",".join("?" * len(expired))
            conn.execute(
                f"DELETE FROM ai_sessions WHERE pubkey IN ({placeholders})",
                expired,
            )
            deleted = conn.execute("SELECT changes()").fetchone()[0]
            conn.commit()
            return deleted
