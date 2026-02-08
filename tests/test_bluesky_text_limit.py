from states.standard import BLUESKY_TEXT_MAX_CHARS, _maybe_clip_bluesky_text


def test_bluesky_text_is_clipped_to_limit():
    args = {"text": "x" * (BLUESKY_TEXT_MAX_CHARS + 50), "root": "at://root", "parent": "at://parent"}
    new_args, warnings = _maybe_clip_bluesky_text("bluesky", "reply_to_post", args)
    assert isinstance(new_args, dict)
    assert isinstance(new_args["text"], str)
    assert len(new_args["text"]) <= BLUESKY_TEXT_MAX_CHARS
    assert warnings


def test_non_bluesky_text_is_not_modified():
    args = {"text": "x" * (BLUESKY_TEXT_MAX_CHARS + 50)}
    new_args, warnings = _maybe_clip_bluesky_text("filesystem", "write_file", args)
    assert new_args == args
    assert warnings == []

