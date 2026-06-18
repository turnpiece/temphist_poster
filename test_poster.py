"""Unit tests for poster.py."""

import os
import sys
import types
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Stub heavy optional imports so tests run without the social SDK packages
# ---------------------------------------------------------------------------

os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

for mod in ("atproto", "mastodon", "Mastodon"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

if "redis" not in sys.modules:
    _redis_mod = types.ModuleType("redis")
    _redis_mod.from_url = lambda url: None  # poster.r patched per-test
    sys.modules["redis"] = _redis_mod

import poster  # noqa: E402  (must come after stubs)

from poster import (  # noqa: E402
    TIER_1,
    TIER_2,
    TIER_3,
    AggregateData,
    LocationSummary,
    TempHistPost,
    already_posted,
    format_aggregate_post,
    format_location_post,
    is_aggregate_due,
    is_posting_time,
    mark_posted,
    periods_due_today,
    preferred_units,
    unit_symbol,
)


class FakeRedis:
    """In-memory Redis stand-in for deduplication tests."""

    def __init__(self):
        self._store: dict = {}

    def exists(self, key):
        return key in self._store

    def set(self, key, value, ex=None):
        self._store[key] = value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = ZoneInfo("UTC")

def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_post(**kwargs) -> TempHistPost:
    defaults = dict(
        period="today",
        location_id="london",
        location="London",
        country="GB",
        summary="Mild temperatures across the city.",
        average=14.5,
        trend="stable",
        slope=0.0,
        slope_error=0.05,
        share_url="https://temphist.com/s/abc123",
        chart_image=b"\x89PNG",
        chart_image_url="https://api.temphist.com/v1/og/abc123.png",
        units="celsius",
    )
    return TempHistPost(**{**defaults, **kwargs})


def make_loc(
    id="london",
    label="London",
    tz="Europe/London",
    country="GB",
    tier=TIER_1,
):
    return {"id": id, "label": label, "tz": tz, "country": country, "tier": tier}


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


class TestUnits:
    def test_us_country_gets_fahrenheit(self):
        assert preferred_units("US") == "fahrenheit"

    def test_non_us_gets_celsius(self):
        for c in ("GB", "AU", "CA", "SG", "ZA"):
            assert preferred_units(c) == "celsius"

    def test_unit_symbol_fahrenheit(self):
        assert unit_symbol("fahrenheit") == "°F"

    def test_unit_symbol_celsius(self):
        assert unit_symbol("celsius") == "°C"


# ---------------------------------------------------------------------------
# Post formatting
# ---------------------------------------------------------------------------


class TestFormatLocationPost:
    def test_contains_location_name(self):
        text = format_location_post(make_post())
        assert "London" in text

    def test_contains_average(self):
        text = format_location_post(make_post(average=14.5, units="celsius"))
        assert "14.5°C" in text

    def test_us_units_show_fahrenheit(self):
        text = format_location_post(make_post(country="US", units="fahrenheit", average=57.2))
        assert "°F" in text

    def test_contains_share_url(self):
        text = format_location_post(make_post())
        assert "https://temphist.com/s/abc123" in text

    def test_contains_temphist_hashtag(self):
        assert "#TempHist" in format_location_post(make_post())

    def test_location_hashtag_no_spaces(self):
        text = format_location_post(make_post(location="New York", location_id="new_york"))
        assert "#NewYork" in text

    def test_warming_trend_emoji(self):
        text = format_location_post(make_post(trend="warming"))
        assert "📈" in text

    def test_cooling_trend_emoji(self):
        text = format_location_post(make_post(trend="cooling"))
        assert "❄️" in text

    def test_long_summary_truncated_to_max_chars(self):
        long_summary = "X" * 500
        text = format_location_post(make_post(summary=long_summary), max_chars=300)
        assert len(text) <= 300

    def test_short_post_not_truncated(self):
        post = make_post(summary="Short summary.")
        text = format_location_post(post, max_chars=300)
        assert "Short summary." in text

    def test_period_label_today(self):
        assert "Today" in format_location_post(make_post(period="today"))

    def test_period_label_week(self):
        assert "This week" in format_location_post(make_post(period="week"))


class TestFormatAggregatePost:
    def _make_agg(self, trends):
        summaries = [
            LocationSummary(location=f"City{i}", average=15.0, trend=t, units="metric")
            for i, t in enumerate(trends)
        ]
        return AggregateData(date=date(2026, 4, 18), summaries=summaries)

    def test_contains_temphist_hashtag(self):
        agg = self._make_agg(["warming", "stable", "cooling"])
        assert "#TempHist" in format_aggregate_post(agg)

    def test_counts_trends(self):
        agg = self._make_agg(["warming", "warming", "stable", "cooling"])
        text = format_aggregate_post(agg)
        assert "2 warming" in text
        assert "1 stable" in text
        assert "1 cooling" in text

    def test_respects_max_chars(self):
        agg = self._make_agg(["warming"] * 20)
        text = format_aggregate_post(agg, max_chars=300)
        assert len(text) <= 300

    def test_shows_date(self):
        agg = self._make_agg(["stable"])
        assert "18 Apr" in format_aggregate_post(agg)


# ---------------------------------------------------------------------------
# AggregateData computed properties
# ---------------------------------------------------------------------------


class TestAggregateData:
    def _make(self, trends, averages=None):
        if averages is None:
            averages = [15.0] * len(trends)
        summaries = [
            LocationSummary(location=f"City{i}", average=avg, trend=t, units="metric")
            for i, (t, avg) in enumerate(zip(trends, averages))
        ]
        return AggregateData(date=date.today(), summaries=summaries)

    def test_warming_count(self):
        agg = self._make(["warming", "warming", "stable"])
        assert agg.warming_count == 2

    def test_cooling_count(self):
        agg = self._make(["cooling", "stable", "stable"])
        assert agg.cooling_count == 1

    def test_stable_count(self):
        agg = self._make(["stable", "warming"])
        assert agg.stable_count == 1

    def test_most_warming_is_highest_average(self):
        agg = self._make(["warming", "warming"], [10.0, 20.0])
        assert agg.most_warming.average == 20.0

    def test_most_cooling_is_lowest_average(self):
        agg = self._make(["cooling", "cooling"], [5.0, 2.0])
        assert agg.most_cooling.average == 2.0

    def test_most_warming_none_when_no_warming(self):
        agg = self._make(["stable", "cooling"])
        assert agg.most_warming is None

    def test_most_cooling_none_when_no_cooling(self):
        agg = self._make(["stable", "warming"])
        assert agg.most_cooling is None


# ---------------------------------------------------------------------------
# Schedule: is_posting_time
# ---------------------------------------------------------------------------


class TestIsPostingTime:
    # London is UTC+0 in winter, UTC+1 in summer.
    # Use a winter date so UTC == local for simplicity.

    def test_exactly_at_post_hour(self):
        loc = make_loc(tz="Europe/London")
        # 2026-01-15 is winter (UTC = local)
        now = utc(2026, 1, 15, 16, 0)
        assert is_posting_time(loc, now) is True

    def test_within_window_before(self):
        loc = make_loc(tz="Europe/London")
        now = utc(2026, 1, 15, 15, 50)
        assert is_posting_time(loc, now) is True

    def test_within_window_after(self):
        loc = make_loc(tz="Europe/London")
        now = utc(2026, 1, 15, 16, 14)
        assert is_posting_time(loc, now) is True

    def test_outside_window(self):
        loc = make_loc(tz="Europe/London")
        now = utc(2026, 1, 15, 12, 0)
        assert is_posting_time(loc, now) is False

    def test_timezone_offset_applied(self):
        # New York is UTC-5 in winter; 4 PM local = 21:00 UTC
        loc = make_loc(tz="America/New_York")
        now = utc(2026, 1, 15, 21, 0)
        assert is_posting_time(loc, now) is True

    def test_wrong_timezone_misses_window(self):
        loc = make_loc(tz="America/New_York")
        # 16:00 UTC is 11 AM NY — not posting time
        now = utc(2026, 1, 15, 16, 0)
        assert is_posting_time(loc, now) is False


# ---------------------------------------------------------------------------
# Schedule: periods_due_today
# ---------------------------------------------------------------------------


class TestPeriodsDueToday:
    def test_tier1_monday_includes_week(self):
        loc = make_loc(tier=TIER_1)
        monday = utc(2026, 6, 15, 16)  # Monday
        periods = periods_due_today(loc, monday)
        assert "week" in periods
        assert "today" in periods

    def test_tier1_tuesday_no_week(self):
        loc = make_loc(tier=TIER_1)
        tuesday = utc(2026, 6, 16, 16)
        periods = periods_due_today(loc, tuesday)
        assert "week" not in periods
        assert "today" in periods

    def test_tier1_first_of_month_includes_month_and_year(self):
        loc = make_loc(tier=TIER_1)
        first = utc(2026, 6, 1, 16)
        periods = periods_due_today(loc, first)
        assert "month" in periods
        assert "year" in periods

    def test_tier2_only_returns_today(self):
        loc = make_loc(tier=TIER_2)
        monday = utc(2026, 6, 15, 16)
        periods = periods_due_today(loc, monday)
        assert periods == ["today"]

    def test_tier3_returns_empty(self):
        loc = make_loc(tier=TIER_3)
        now = utc(2026, 6, 15, 16)
        assert periods_due_today(loc, now) == []


# ---------------------------------------------------------------------------
# Schedule: is_aggregate_due
# ---------------------------------------------------------------------------


class TestIsAggregateDue:
    def test_friday_at_post_hour(self):
        # 2026-06-19 is a Friday; aggregate check uses UTC directly
        now = utc(2026, 6, 19, 16, 0)
        assert is_aggregate_due(now) is True

    def test_friday_outside_window(self):
        now = utc(2026, 6, 19, 10, 0)
        assert is_aggregate_due(now) is False

    def test_thursday_at_post_hour(self):
        now = utc(2026, 6, 18, 16, 0)
        assert is_aggregate_due(now) is False


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.fixture(autouse=True)
    def fake_redis(self):
        fr = FakeRedis()
        with patch.object(poster, "r", fr):
            yield fr

    def test_not_posted_initially(self):
        assert already_posted("london", "today") is False

    def test_posted_after_mark(self):
        mark_posted("london", "today")
        assert already_posted("london", "today") is True

    def test_different_location_not_affected(self):
        mark_posted("london", "today")
        assert already_posted("new_york", "today") is False

    def test_different_period_not_affected(self):
        mark_posted("london", "today")
        assert already_posted("london", "week") is False

    def test_key_includes_date(self, fake_redis):
        mark_posted("london", "today")
        today = date.today().isoformat()
        assert any(today in k for k in fake_redis._store)
