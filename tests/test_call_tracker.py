import pytest

from tests.conftest import GEMINI_MODEL_PRIORITY


@pytest.mark.asyncio
async def test_can_make_call_allows_under_limit(call_tracker):
    model = GEMINI_MODEL_PRIORITY[0]
    can_call, reason = await call_tracker.can_make_call("gemini", "generate", model, "1111")
    assert can_call is True


@pytest.mark.asyncio
async def test_rpm_limit_enforced(call_tracker):
    model = GEMINI_MODEL_PRIORITY[0]  # quota rpm=15
    for _ in range(15):
        await call_tracker.record_call("gemini", "generate", model, "1111", True, "ok", total_tokens=10)

    can_call, reason = await call_tracker.can_make_call("gemini", "generate", model, "1111")
    assert can_call is False
    assert "RPM" in reason


@pytest.mark.asyncio
async def test_unknown_model_rejected(call_tracker):
    can_call, reason = await call_tracker.can_make_call("gemini", "generate", "not-a-real-model", "1111")
    assert can_call is False
    assert "Unknown" in reason


@pytest.mark.asyncio
async def test_input_output_tokens_persisted_separately(call_tracker, fake_redis):
    """Regression test: the source repo's APICallTracker only ever persisted
    total_token_count — input/output were logged to console but never stored. This
    service must store both, which is why record_call takes them as distinct params
    and get_quota_summary's tokens_day sums the (separately supplied) total.
    """
    model = GEMINI_MODEL_PRIORITY[0]
    await call_tracker.record_call(
        "gemini", "generate", model, "1111", True, "ok",
        input_tokens=40, output_tokens=60, total_tokens=100,
    )

    summary = await call_tracker.get_quota_summary(["1111"])
    assert summary["gemini"][model]["tokens_day"] == 100


@pytest.mark.asyncio
async def test_get_retry_after_seconds_zero_when_under_limit(call_tracker):
    model = GEMINI_MODEL_PRIORITY[0]
    wait = await call_tracker.get_retry_after_seconds("gemini", "generate", model, "1111")
    assert wait == 0.0


@pytest.mark.asyncio
async def test_failures_tracked_in_quota_summary(call_tracker):
    model = GEMINI_MODEL_PRIORITY[0]
    await call_tracker.record_call("gemini", "generate", model, "1111", False, "boom", total_tokens=0)

    summary = await call_tracker.get_quota_summary(["1111"])
    assert summary["gemini"][model]["failures_day"] == 1
