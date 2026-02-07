import pytest


def test_coerce_json_object_accepts_dict():
    from states.standard import _coerce_json_object

    assert _coerce_json_object({"limit": 5}) == {"limit": 5}


def test_coerce_json_object_accepts_json_string():
    from states.standard import _coerce_json_object

    assert _coerce_json_object('{"limit": 5}') == {"limit": 5}


def test_coerce_json_object_rejects_non_object_json():
    from states.standard import _coerce_json_object

    with pytest.raises(ValueError):
        _coerce_json_object("[1, 2, 3]")

