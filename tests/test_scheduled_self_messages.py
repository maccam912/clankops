from pathlib import Path

import pytest


def test_scheduled_self_messages_persist_and_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))

    from memory_store import MemoryStore

    store = MemoryStore()
    now = int(__import__("time").time())

    created = store.schedule_self_message(deliver_at_utc=now - 5, message="hello future me")
    assert created["ok"] is True
    msg_id = int(created["id"])

    claimed = store.claim_due_self_messages(limit=5)
    assert len(claimed) == 1
    assert int(claimed[0]["id"]) == msg_id
    assert claimed[0]["message"] == "hello future me"

    store.mark_self_message_delivered(message_id=msg_id)
    rows = store.list_scheduled_self_messages(status="delivered", limit=10)
    assert len(rows) == 1
    assert int(rows[0]["id"]) == msg_id
    assert rows[0]["status"] == "delivered"


def test_reclaim_stuck_delivering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))

    from memory_store import MemoryStore
    import sqlite3

    store = MemoryStore()
    now = int(__import__("time").time())

    created = store.schedule_self_message(deliver_at_utc=now - 5, message="x")
    msg_id = int(created["id"])

    # Simulate a crash after marking delivering.
    with sqlite3.connect(store.sqlite_path) as conn:
        conn.execute(
            "UPDATE scheduled_self_messages SET status='delivering', updated_at_utc=? WHERE id=?",
            (now - 1000, msg_id),
        )
        conn.commit()

    reclaimed = store.reclaim_stuck_delivering(stuck_seconds=300)
    assert reclaimed >= 1

    claimed = store.claim_due_self_messages(limit=5)
    assert claimed and int(claimed[0]["id"]) == msg_id
