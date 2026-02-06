from __future__ import annotations

import os
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
