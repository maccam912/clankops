from pathlib import Path

import pytest


def test_resolve_workspace_path_blocks_escape_without_dangerzone(tmp_path: Path):
    from states import standard

    outside = tmp_path / "outside.txt"
    outside.write_text("hi", encoding="utf-8")

    # Absolute path outside workspace should be rejected by default.
    with pytest.raises(ValueError):
        standard._resolve_workspace_path(str(outside), allow_outside_workspace=False)


def test_resolve_workspace_path_allows_escape_with_dangerzone(tmp_path: Path):
    from states import standard

    outside = tmp_path / "outside.txt"
    outside.write_text("hi", encoding="utf-8")

    resolved = standard._resolve_workspace_path(str(outside), allow_outside_workspace=True)
    assert resolved.resolve() == outside.resolve()

