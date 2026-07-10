from app.pool.backoff import compute_backoff_seconds


def test_streak_1_is_roughly_base_delay():
    delay = compute_backoff_seconds("rate_limit", streak=1)
    assert 30.0 <= delay <= 30.0 * 1.25


def test_delay_doubles_with_streak():
    d1 = compute_backoff_seconds("rate_limit", streak=1, jitter_fraction=0.0)
    d2 = compute_backoff_seconds("rate_limit", streak=2, jitter_fraction=0.0)
    d3 = compute_backoff_seconds("rate_limit", streak=3, jitter_fraction=0.0)
    assert d2 == d1 * 2
    assert d3 == d1 * 4


def test_streak_caps_at_max_streak():
    d8 = compute_backoff_seconds("rate_limit", streak=8, jitter_fraction=0.0)
    d20 = compute_backoff_seconds("rate_limit", streak=20, jitter_fraction=0.0)
    assert d8 == d20


def test_delay_never_exceeds_max_backoff():
    delay = compute_backoff_seconds("rate_limit", streak=100)
    assert delay <= 1800.0 * 1.25


def test_high_demand_uses_smaller_base():
    rate_limit_delay = compute_backoff_seconds("rate_limit", streak=1, jitter_fraction=0.0)
    high_demand_delay = compute_backoff_seconds("high_demand", streak=1, jitter_fraction=0.0)
    assert high_demand_delay < rate_limit_delay


def test_unknown_failure_type_falls_back_to_default_base():
    delay = compute_backoff_seconds("something_else", streak=1, jitter_fraction=0.0)
    assert delay == 30.0
