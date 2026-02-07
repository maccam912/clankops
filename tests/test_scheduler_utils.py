import pytest


def test_parse_every_to_interval_seconds():
    import scheduler_utils

    seconds, interpreted = scheduler_utils.parse_every_to_interval_seconds("every hour")
    assert seconds == 60 * 60
    assert "every hour" in interpreted.lower()

    seconds, _ = scheduler_utils.parse_every_to_interval_seconds("every 2 hours")
    assert seconds == 2 * 60 * 60

    seconds, _ = scheduler_utils.parse_every_to_interval_seconds("every 30m")
    assert seconds == 30 * 60

    seconds, _ = scheduler_utils.parse_every_to_interval_seconds("hourly")
    assert seconds == 60 * 60


def test_is_recurring_when():
    import scheduler_utils

    assert scheduler_utils.is_recurring_when("every hour") is True
    assert scheduler_utils.is_recurring_when("hourly") is True
    assert scheduler_utils.is_recurring_when("in 10 minutes") is False


def test_parse_every_rejects_invalid():
    import scheduler_utils

    with pytest.raises(ValueError):
        scheduler_utils.parse_every_to_interval_seconds("every potatoes")

