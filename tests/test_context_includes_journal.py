def test_memory_blocks_prompt_includes_2_most_recent_journal_entries_by_default():
    from machine import SessionContext

    ctx = SessionContext()
    ctx.identity_block = "id"
    ctx.human_block = "human"
    ctx.journal_entries = [f"entry-{i}" for i in range(1, 9)]  # 8 entries total

    prompt = ctx.memory_blocks_prompt()

    assert "RECENT JOURNAL ENTRIES" in prompt
    assert "entry-7" in prompt
    assert "entry-8" in prompt
    assert "entry-6" not in prompt
