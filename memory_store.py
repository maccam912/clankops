from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IDENTITY = (
    "Name: ClankOps Assistant\n"
    "Style: clear, practical, concise\n"
    "Personality: calm, direct, and helpful\n"
)

DEFAULT_HUMAN = (
    "Name: Unknown\n"
    "Likes: Unknown\n"
    "Dislikes: Unknown\n"
    "Bio: No details recorded yet.\n"
)
JOURNAL_HEADER = "Journal entries are appended below."


@dataclass
class MemoryStore:
    memory_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("MEMORY_DIR", "memory"))
    )

    def __post_init__(self):
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.touch(exist_ok=True)
        self._ensure_sqlite_schema()
        self._migrate_legacy_memory_to_sqlite()

    @property
    def identity_path(self) -> Path:
        return self.memory_dir / "identity.txt"

    @property
    def human_path(self) -> Path:
        return self.memory_dir / "human.txt"

    @property
    def journal_dir(self) -> Path:
        return self.memory_dir / "journal"

    @property
    def legacy_journal_path(self) -> Path:
        return self.memory_dir / "journal.txt"

    @property
    def sqlite_path(self) -> Path:
        return self.memory_dir / "memory.sqlite3"

    def _ensure_sqlite_schema(self):
        """Create tables needed by the app (idempotent)."""
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_type TEXT NOT NULL, -- identity|human
                    user_id INTEGER, -- NULL for global blocks (identity)
                    content TEXT NOT NULL,
                    created_at_utc INTEGER NOT NULL,
                    updated_at_utc INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_blocks_type_user "
                "ON memory_blocks(block_type, user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc INTEGER NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_entries_created "
                "ON journal_entries(created_at_utc)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER,
                    role TEXT NOT NULL, -- user|assistant
                    state TEXT,
                    content TEXT NOT NULL,
                    created_at_utc INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_user_time "
                "ON chat_messages(user_id, created_at_utc)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_time "
                "ON chat_messages(created_at_utc)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_self_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc INTEGER NOT NULL,
                    deliver_at_utc INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    recurrence_seconds INTEGER, -- NULL for one-off; otherwise interval in seconds
                    end_at_utc INTEGER, -- NULL means run forever
                    status TEXT NOT NULL DEFAULT 'pending', -- pending|delivering|delivered|canceled
                    attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc INTEGER NOT NULL,
                    delivered_at_utc INTEGER,
                    canceled_at_utc INTEGER,
                    last_error TEXT
                )
                """
            )
            self._ensure_scheduled_self_messages_columns(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_self_messages_pending_deliver "
                "ON scheduled_self_messages(status, deliver_at_utc)"
            )
            conn.commit()

    @staticmethod
    def _ensure_scheduled_self_messages_columns(conn: sqlite3.Connection) -> None:
        existing = set()
        for row in conn.execute("PRAGMA table_info(scheduled_self_messages)").fetchall():
            # row[1] is the column name
            existing.add(str(row[1]))

        # Backwards-compatible migration for databases created before recurring scheduling existed.
        add_columns: list[str] = []
        if "recurrence_seconds" not in existing:
            add_columns.append("recurrence_seconds INTEGER")
        if "end_at_utc" not in existing:
            add_columns.append("end_at_utc INTEGER")
        if "delivery_count" not in existing:
            add_columns.append("delivery_count INTEGER NOT NULL DEFAULT 0")

        for col_def in add_columns:
            conn.execute(f"ALTER TABLE scheduled_self_messages ADD COLUMN {col_def}")

    @staticmethod
    def _now_utc_epoch() -> int:
        return int(time.time())

    def schedule_self_message(
        self,
        *,
        deliver_at_utc: int,
        message: str,
        recurrence_seconds: int | None = None,
        end_at_utc: int | None = None,
    ) -> dict[str, object]:
        text = (message or "").strip()
        if not text:
            raise ValueError("message is empty.")

        now = self._now_utc_epoch()
        deliver_at = int(deliver_at_utc)
        if deliver_at < 0:
            raise ValueError("deliver_at_utc must be a positive epoch timestamp.")

        recurrence: int | None = None
        if recurrence_seconds is not None:
            recurrence = int(recurrence_seconds)
            if recurrence <= 0:
                raise ValueError("recurrence_seconds must be a positive integer (seconds).")

        end_at: int | None = None
        if end_at_utc is not None:
            end_at = int(end_at_utc)
            if end_at < 0:
                raise ValueError("end_at_utc must be a positive epoch timestamp.")

        if recurrence is not None and end_at is not None and end_at < deliver_at:
            raise ValueError("end_at_utc must be after the first delivery time.")

        with sqlite3.connect(self.sqlite_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_self_messages (
                    created_at_utc, deliver_at_utc, message, recurrence_seconds, end_at_utc, status, attempts, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
                """,
                (now, deliver_at, text, recurrence, end_at, now),
            )
            conn.commit()
            msg_id = int(cur.lastrowid)

        return {
            "ok": True,
            "id": msg_id,
            "deliver_at_utc": deliver_at,
            "recurrence_seconds": recurrence,
            "end_at_utc": end_at,
            "status": "pending",
        }

    def list_scheduled_self_messages(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        safe_limit = max(1, min(int(limit), 200))
        status_raw = (status or "").strip().lower()
        params: tuple[object, ...]
        if status_raw:
            sql = (
                "SELECT id, created_at_utc, deliver_at_utc, recurrence_seconds, end_at_utc, status, attempts, "
                "delivery_count, updated_at_utc, "
                "delivered_at_utc, canceled_at_utc, last_error, message "
                "FROM scheduled_self_messages "
                "WHERE status = ? "
                "ORDER BY deliver_at_utc ASC "
                "LIMIT ?"
            )
            params = (status_raw, safe_limit)
        else:
            sql = (
                "SELECT id, created_at_utc, deliver_at_utc, recurrence_seconds, end_at_utc, status, attempts, "
                "delivery_count, updated_at_utc, "
                "delivered_at_utc, canceled_at_utc, last_error, message "
                "FROM scheduled_self_messages "
                "ORDER BY deliver_at_utc ASC "
                "LIMIT ?"
            )
            params = (safe_limit,)

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def expire_ended_recurring(self) -> int:
        """Mark recurring schedules as complete once their end time has passed.

        Policy: if now is after end_at_utc, we stop without delivering missed occurrences.
        """
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_self_messages
                SET status = 'delivered', updated_at_utc = ?, last_error = NULL
                WHERE status = 'pending'
                  AND recurrence_seconds IS NOT NULL
                  AND end_at_utc IS NOT NULL
                  AND end_at_utc < ?
                """,
                (now, now),
            )
            conn.commit()
            return int(cur.rowcount)

    def cancel_scheduled_self_message(self, *, message_id: int) -> dict[str, object]:
        msg_id = int(message_id)
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_self_messages
                SET status = 'canceled', canceled_at_utc = ?, updated_at_utc = ?
                WHERE id = ? AND status IN ('pending', 'delivering')
                """,
                (now, now, msg_id),
            )
            conn.commit()
            return {"ok": True, "id": msg_id, "canceled": cur.rowcount > 0}

    def reclaim_stuck_delivering(self, *, stuck_seconds: int = 300) -> int:
        """If the process crashed mid-delivery, make messages deliverable again."""
        now = self._now_utc_epoch()
        cutoff = now - max(1, int(stuck_seconds))
        with sqlite3.connect(self.sqlite_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_self_messages
                SET status = 'pending', updated_at_utc = ?
                WHERE status = 'delivering' AND updated_at_utc < ?
                """,
                (now, cutoff),
            )
            conn.commit()
            return int(cur.rowcount)

    def next_pending_self_message_time_utc(self) -> int | None:
        self.expire_ended_recurring()
        with sqlite3.connect(self.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT MIN(
                    CASE
                        WHEN recurrence_seconds IS NOT NULL
                             AND end_at_utc IS NOT NULL
                             AND end_at_utc < deliver_at_utc
                        THEN end_at_utc
                        ELSE deliver_at_utc
                    END
                ) AS next_deliver
                FROM scheduled_self_messages
                WHERE status = 'pending'
                """
            ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])

    def claim_due_self_messages(self, *, limit: int = 10) -> list[dict[str, object]]:
        """Atomically claim up to limit due messages (mark as delivering) and return them."""
        safe_limit = max(1, min(int(limit), 50))
        now = self._now_utc_epoch()

        self.expire_ended_recurring()

        # Reclaim obviously stuck deliveries before claiming more.
        self.reclaim_stuck_delivering(stuck_seconds=300)

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, created_at_utc, deliver_at_utc, message, attempts
                FROM scheduled_self_messages
                WHERE status = 'pending'
                  AND deliver_at_utc <= ?
                  AND (end_at_utc IS NULL OR ? <= end_at_utc)
                ORDER BY deliver_at_utc ASC, id ASC
                LIMIT ?
                """,
                (now, now, safe_limit),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                qmarks = ",".join("?" for _ in ids)
                params: list[object] = [now]
                params.extend(ids)
                conn.execute(
                    f"""
                    UPDATE scheduled_self_messages
                    SET status = 'delivering', attempts = attempts + 1, updated_at_utc = ?
                    WHERE id IN ({qmarks}) AND status = 'pending'
                    """,
                    params,
                )
            conn.commit()

        claimed: list[dict[str, object]] = []
        for r in rows:
            claimed.append(
                {
                    "id": int(r["id"]),
                    "created_at_utc": int(r["created_at_utc"]),
                    "deliver_at_utc": int(r["deliver_at_utc"]),
                    "message": str(r["message"]),
                    "attempts": int(r["attempts"]) + 1,
                }
            )
        return claimed

    def mark_self_message_delivered(self, *, message_id: int) -> None:
        msg_id = int(message_id)
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            row = conn.execute(
                "SELECT recurrence_seconds, end_at_utc FROM scheduled_self_messages WHERE id = ?",
                (msg_id,),
            ).fetchone()

            recurrence = int(row[0]) if row and row[0] is not None else None
            end_at = int(row[1]) if row and row[1] is not None else None

            if recurrence is None:
                conn.execute(
                    """
                    UPDATE scheduled_self_messages
                    SET status = 'delivered',
                        delivered_at_utc = ?,
                        delivery_count = delivery_count + 1,
                        updated_at_utc = ?,
                        last_error = NULL
                    WHERE id = ?
                    """,
                    (now, now, msg_id),
                )
            else:
                next_deliver_at = now + recurrence
                if end_at is not None and next_deliver_at > end_at:
                    # Completed: no further deliveries.
                    conn.execute(
                        """
                        UPDATE scheduled_self_messages
                        SET status = 'delivered',
                            delivered_at_utc = ?,
                            delivery_count = delivery_count + 1,
                            updated_at_utc = ?,
                            last_error = NULL
                        WHERE id = ?
                        """,
                        (now, now, msg_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE scheduled_self_messages
                        SET status = 'pending',
                            deliver_at_utc = ?,
                            delivered_at_utc = ?,
                            delivery_count = delivery_count + 1,
                            updated_at_utc = ?,
                            last_error = NULL
                        WHERE id = ?
                        """,
                        (next_deliver_at, now, now, msg_id),
                    )
            conn.commit()

    def mark_self_message_failed(self, *, message_id: int, error: str, retry_delay_seconds: int = 60) -> None:
        msg_id = int(message_id)
        now = self._now_utc_epoch()
        delay = max(5, min(int(retry_delay_seconds), 3600))
        new_deliver_at = now + delay
        err_text = (error or "").strip()[:2000]
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE scheduled_self_messages
                SET status = 'pending', deliver_at_utc = ?, updated_at_utc = ?, last_error = ?
                WHERE id = ?
                """,
                (new_deliver_at, now, err_text, msg_id),
            )
            conn.commit()

    def load_identity(self) -> str:
        row = self._get_memory_block(block_type="identity", user_id=None)
        return row if row else DEFAULT_IDENTITY.strip()

    def load_human(self, *, user_id: int) -> str:
        uid = int(user_id or 0)
        row = self._get_memory_block(block_type="human", user_id=uid)
        if row:
            return row
        # Keep the legacy structure but annotate user_id for clarity.
        return (
            f"Name: Unknown (Telegram user_id: {uid})\n"
            "Likes: Unknown\n"
            "Dislikes: Unknown\n"
            "Bio: No details recorded yet.\n"
        ).strip()

    def save_identity(self, content: str):
        text = (content or "").strip()
        if not text:
            return
        self._set_memory_block(block_type="identity", user_id=None, content=text)

    def save_human(self, *, user_id: int, content: str):
        text = (content or "").strip()
        if not text:
            return
        self._set_memory_block(block_type="human", user_id=int(user_id or 0), content=text)

    def load_journal_entries(self) -> list[str]:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT content FROM journal_entries ORDER BY created_at_utc ASC, id ASC"
            ).fetchall()
        out: list[str] = []
        for r in rows:
            text = str(r["content"]).strip()
            if text:
                out.append(text)
        return out

    def append_journal_entry(self, content: str):
        text = content.strip()
        if not text:
            return
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO journal_entries(created_at_utc, content) VALUES (?, ?)",
                (now, text),
            )
            conn.commit()

    @staticmethod
    def _ensure_file(path: Path, default_text: str):
        if not path.exists():
            path.write_text(default_text, encoding="utf-8")

    @staticmethod
    def _load_or_default(path: Path, default_text: str) -> str:
        text = path.read_text(encoding="utf-8").strip()
        return text if text else default_text.strip()

    def append_chat_message(
        self,
        *,
        user_id: int,
        role: str,
        content: str,
        state: str | None = None,
        chat_id: int | None = None,
    ) -> None:
        text = (content or "").strip()
        if not text:
            return
        safe_role = (role or "").strip().lower()
        if safe_role not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'.")
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                "INSERT INTO chat_messages(user_id, chat_id, role, state, content, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(user_id or 0), int(chat_id) if chat_id is not None else None, safe_role, (state or "").strip() or None, text, now),
            )
            conn.commit()

    def load_recent_chat_messages(
        self,
        *,
        user_id: int,
        limit: int = 20,
        since_utc: int | None = None,
    ) -> list[dict[str, object]]:
        safe_limit = max(1, min(int(limit), 200))
        uid = int(user_id or 0)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            if since_utc is None:
                rows = conn.execute(
                    "SELECT role, content, created_at_utc FROM chat_messages "
                    "WHERE user_id = ? "
                    "ORDER BY created_at_utc DESC, id DESC "
                    "LIMIT ?",
                    (uid, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT role, content, created_at_utc FROM chat_messages "
                    "WHERE user_id = ? AND created_at_utc >= ? "
                    "ORDER BY created_at_utc DESC, id DESC "
                    "LIMIT ?",
                    (uid, int(since_utc), safe_limit),
                ).fetchall()
        out = [dict(r) for r in reversed(rows)]
        return out

    def list_user_ids_with_recent_messages(self, *, since_utc: int) -> list[int]:
        cutoff = int(since_utc)
        with sqlite3.connect(self.sqlite_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM chat_messages "
                "WHERE role = 'user' AND created_at_utc >= ?",
                (cutoff,),
            ).fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]

    def _get_memory_block(self, *, block_type: str, user_id: int | None) -> str | None:
        bt = (block_type or "").strip().lower()
        if bt not in ("identity", "human"):
            raise ValueError("block_type must be 'identity' or 'human'.")
        with sqlite3.connect(self.sqlite_path) as conn:
            row = conn.execute(
                "SELECT content FROM memory_blocks WHERE block_type = ? AND user_id IS ?",
                (bt, user_id),
            ).fetchone()
        if not row:
            return None
        text = str(row[0]).strip()
        return text or None

    def _set_memory_block(self, *, block_type: str, user_id: int | None, content: str) -> None:
        bt = (block_type or "").strip().lower()
        if bt not in ("identity", "human"):
            raise ValueError("block_type must be 'identity' or 'human'.")
        text = (content or "").strip()
        if not text:
            return
        now = self._now_utc_epoch()
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_blocks(block_type, user_id, content, created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(block_type, user_id)
                DO UPDATE SET content = excluded.content, updated_at_utc = excluded.updated_at_utc
                """,
                (bt, user_id, text, now, now),
            )
            conn.commit()

    def _migrate_legacy_memory_to_sqlite(self) -> None:
        """One-way migrate identity/human/journal text files into SQLite when SQLite is empty."""
        # Ensure legacy dirs exist for reading if present; do not create/write new legacy files.
        self.journal_dir.mkdir(parents=True, exist_ok=True)

        # Identity
        if not self._get_memory_block(block_type="identity", user_id=None):
            legacy_identity: str | None = None
            if self.identity_path.exists():
                legacy_identity = self.identity_path.read_text(encoding="utf-8").strip()
            self._set_memory_block(
                block_type="identity",
                user_id=None,
                content=(legacy_identity or DEFAULT_IDENTITY).strip(),
            )

        # Human: legacy was a single shared file; migrate into main user if known, else user_id=0.
        with sqlite3.connect(self.sqlite_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM memory_blocks WHERE block_type='human' LIMIT 1"
            ).fetchone()
        if not row:
            main_uid = 0
            raw = os.environ.get("TELEGRAM_USER_ID", "").strip()
            try:
                main_uid = int(raw) if raw else 0
            except Exception:
                main_uid = 0
            legacy_human: str | None = None
            if self.human_path.exists():
                legacy_human = self.human_path.read_text(encoding="utf-8").strip()
            self._set_memory_block(
                block_type="human",
                user_id=int(main_uid),
                content=(legacy_human or DEFAULT_HUMAN).strip(),
            )

        # Journal: migrate legacy journal.txt (if any) plus journal/*.txt, but only if SQLite has none.
        with sqlite3.connect(self.sqlite_path) as conn:
            has_any = conn.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone()
        if has_any:
            return

        to_insert: list[tuple[int, str]] = []

        if self.legacy_journal_path.exists():
            text = self.legacy_journal_path.read_text(encoding="utf-8").strip()
            if text.startswith(JOURNAL_HEADER):
                text = text[len(JOURNAL_HEADER) :].strip()
            if text:
                chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
                for chunk in chunks:
                    to_insert.append((self._now_utc_epoch(), chunk))

        # Migrate per-entry files.
        for path in sorted(self.journal_dir.glob("*.txt")):
            txt = path.read_text(encoding="utf-8").strip()
            if not txt:
                continue
            created_at = int(path.stat().st_mtime)
            # Try to parse filenames like 20260208-010814-322814.txt
            name = path.stem
            try:
                # YYYYMMDD-HHMMSS-ffffff
                dt = datetime.strptime(name.split("-")[0] + name.split("-")[1], "%Y%m%d%H%M%S")
                created_at = int(dt.timestamp())
            except Exception:
                pass
            to_insert.append((created_at, txt))

        if not to_insert:
            return

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.executemany(
                "INSERT INTO journal_entries(created_at_utc, content) VALUES (?, ?)",
                to_insert,
            )
            conn.commit()
