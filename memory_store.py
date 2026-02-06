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
        self._ensure_file(self.identity_path, DEFAULT_IDENTITY)
        self._ensure_file(self.human_path, DEFAULT_HUMAN)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.touch(exist_ok=True)
        self._ensure_sqlite_schema()
        self._migrate_legacy_journal_file()

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
                CREATE TABLE IF NOT EXISTS scheduled_self_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc INTEGER NOT NULL,
                    deliver_at_utc INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', -- pending|delivering|delivered|canceled
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc INTEGER NOT NULL,
                    delivered_at_utc INTEGER,
                    canceled_at_utc INTEGER,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_self_messages_pending_deliver "
                "ON scheduled_self_messages(status, deliver_at_utc)"
            )
            conn.commit()

    @staticmethod
    def _now_utc_epoch() -> int:
        return int(time.time())

    def schedule_self_message(self, *, deliver_at_utc: int, message: str) -> dict[str, object]:
        text = (message or "").strip()
        if not text:
            raise ValueError("message is empty.")

        now = self._now_utc_epoch()
        deliver_at = int(deliver_at_utc)
        if deliver_at < 0:
            raise ValueError("deliver_at_utc must be a positive epoch timestamp.")

        with sqlite3.connect(self.sqlite_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_self_messages (
                    created_at_utc, deliver_at_utc, message, status, attempts, updated_at_utc
                ) VALUES (?, ?, ?, 'pending', 0, ?)
                """,
                (now, deliver_at, text, now),
            )
            conn.commit()
            msg_id = int(cur.lastrowid)

        return {"ok": True, "id": msg_id, "deliver_at_utc": deliver_at, "status": "pending"}

    def list_scheduled_self_messages(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        safe_limit = max(1, min(int(limit), 200))
        status_raw = (status or "").strip().lower()
        params: tuple[object, ...]
        if status_raw:
            sql = (
                "SELECT id, created_at_utc, deliver_at_utc, status, attempts, updated_at_utc, "
                "delivered_at_utc, canceled_at_utc, last_error, message "
                "FROM scheduled_self_messages "
                "WHERE status = ? "
                "ORDER BY deliver_at_utc ASC "
                "LIMIT ?"
            )
            params = (status_raw, safe_limit)
        else:
            sql = (
                "SELECT id, created_at_utc, deliver_at_utc, status, attempts, updated_at_utc, "
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
        with sqlite3.connect(self.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT MIN(deliver_at_utc) AS next_deliver
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

        # Reclaim obviously stuck deliveries before claiming more.
        self.reclaim_stuck_delivering(stuck_seconds=300)

        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, created_at_utc, deliver_at_utc, message, attempts
                FROM scheduled_self_messages
                WHERE status = 'pending' AND deliver_at_utc <= ?
                ORDER BY deliver_at_utc ASC, id ASC
                LIMIT ?
                """,
                (now, safe_limit),
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
            conn.execute(
                """
                UPDATE scheduled_self_messages
                SET status = 'delivered', delivered_at_utc = ?, updated_at_utc = ?, last_error = NULL
                WHERE id = ?
                """,
                (now, now, msg_id),
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
        return self._load_or_default(self.identity_path, DEFAULT_IDENTITY)

    def load_human(self) -> str:
        return self._load_or_default(self.human_path, DEFAULT_HUMAN)

    def save_identity(self, content: str):
        self._save(self.identity_path, content)

    def save_human(self, content: str):
        self._save(self.human_path, content)

    def load_journal_entries(self) -> list[str]:
        entries: list[str] = []
        for path in sorted(self.journal_dir.glob("*.txt")):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                entries.append(text)
        return entries

    def append_journal_entry(self, content: str):
        text = content.strip()
        if not text:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.journal_dir / f"{stamp}.txt"
        suffix = 1
        while path.exists():
            path = self.journal_dir / f"{stamp}-{suffix}.txt"
            suffix += 1
        path.write_text(f"{text}\n", encoding="utf-8")

    def _migrate_legacy_journal_file(self):
        if not self.legacy_journal_path.exists():
            return

        if any(self.journal_dir.glob("*.txt")):
            self.legacy_journal_path.unlink(missing_ok=True)
            return

        text = self.legacy_journal_path.read_text(encoding="utf-8").strip()
        if text.startswith(JOURNAL_HEADER):
            text = text[len(JOURNAL_HEADER) :].strip()

        if text:
            chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
            for chunk in chunks:
                self.append_journal_entry(chunk)

        self.legacy_journal_path.unlink(missing_ok=True)

    @staticmethod
    def _save(path: Path, content: str):
        text = content.strip()
        if not text:
            return
        path.write_text(f"{text}\n", encoding="utf-8")

    @staticmethod
    def _ensure_file(path: Path, default_text: str):
        if not path.exists():
            path.write_text(default_text, encoding="utf-8")

    @staticmethod
    def _load_or_default(path: Path, default_text: str) -> str:
        text = path.read_text(encoding="utf-8").strip()
        return text if text else default_text.strip()
