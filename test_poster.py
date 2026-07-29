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
    _redis_mod.Redis = object
    sys.modules["redis"] = _redis_mod

import poster  # noqa: E402  (must come after stubs)

from poster import (  # noqa: E402
    TIER_1,
    TIER_2,
    TIER_TOPICAL,
    TempHistPost,
    _parse_topical_locations,
    _ranking_points,
    already_posted,
    format_location_post,
    is_posting_time,
    is_remarkable,
    is_scheduled_period,
    mark_posted,
    mark_record_reposted,
    periods_due_today,
    post_location_period,
    preferred_units,
    record_repost_on_cooldown,
    remarkability_score,
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
        slope_error=None,
        anomaly=None,
        anomaly_std_dev=None,
        share_url="https://temphist.com/s/abc123",
        chart_image=b"\x89PNG",
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

    def test_excludes_share_url_when_include_url_false(self):
        text = format_location_post(make_post(), include_url=False)
        assert "https://temphist.com/s/abc123" not in text

    def test_contains_climate_hashtag(self):
        assert "#climate" in format_location_post(make_post())

    def test_location_hashtag_no_spaces(self):
        text = format_location_post(make_post(location="New York", location_id="new_york"))
        assert "#NewYork" in text

    def test_warming_trend_emoji(self):
        text = format_location_post(make_post(trend="warming", slope=0.5))
        assert "📈" in text

    def test_cooling_trend_emoji(self):
        text = format_location_post(make_post(trend="cooling", slope=-0.5))
        assert "📉" in text

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

    def test_month_and_year_are_due_every_day_opportunistically(self):
        loc = make_loc(tier=TIER_1)
        mid_month = utc(2026, 6, 16, 16)
        periods = periods_due_today(loc, mid_month)
        assert "month" in periods
        assert "year" in periods

    def test_tier2_returns_full_schedule(self):
        loc = make_loc(tier=TIER_2)
        monday = utc(2026, 6, 15, 16)
        periods = periods_due_today(loc, monday)
        assert set(periods) == {"today", "week", "month", "year"}

    def test_tier2_month_year_due_on_non_first_day_too(self):
        loc = make_loc(tier=TIER_2)
        mid_month = utc(2026, 6, 16, 16)
        periods = periods_due_today(loc, mid_month)
        assert set(periods) == {"today", "month", "year"}


class TestIsScheduledPeriod:
    def test_today_always_scheduled(self):
        loc = make_loc(tier=TIER_1)
        assert is_scheduled_period(loc, "today", utc(2026, 6, 16, 16)) is True

    def test_week_scheduled_on_monday_only(self):
        loc = make_loc(tier=TIER_1)
        assert is_scheduled_period(loc, "week", utc(2026, 6, 15, 16)) is True  # Monday
        assert is_scheduled_period(loc, "week", utc(2026, 6, 16, 16)) is False  # Tuesday

    def test_month_scheduled_on_first_only(self):
        loc = make_loc(tier=TIER_1)
        assert is_scheduled_period(loc, "month", utc(2026, 6, 1, 16)) is True
        assert is_scheduled_period(loc, "month", utc(2026, 6, 16, 16)) is False

    def test_year_scheduled_on_first_only(self):
        loc = make_loc(tier=TIER_1)
        assert is_scheduled_period(loc, "year", utc(2026, 6, 1, 16)) is True
        assert is_scheduled_period(loc, "year", utc(2026, 6, 16, 16)) is False


# ---------------------------------------------------------------------------
# Location loading: topical locations
# ---------------------------------------------------------------------------


class TestParseTopicalLocations:
    def test_empty_env_is_noop(self):
        locations = [make_loc(id="london", tier=TIER_1)]
        result = _parse_topical_locations("", locations)
        assert result == []
        assert locations[0]["tier"] == TIER_1

    def test_bare_id_retags_existing_tier2(self):
        loc = make_loc(id="singapore", tier=TIER_2)
        result = _parse_topical_locations("singapore", [loc])
        assert loc["tier"] == TIER_TOPICAL
        assert result == ["singapore"]

    def test_bare_id_leaves_tier1_untouched(self):
        loc = make_loc(id="london", tier=TIER_1)
        result = _parse_topical_locations("london", [loc])
        assert loc["tier"] == TIER_1
        assert result == []

    def test_bare_id_not_found_is_skipped_not_crashed(self):
        result = _parse_topical_locations("nowhere", [make_loc(id="london")])
        assert result == []

    def test_full_tuple_appends_new_location(self):
        locations = [make_loc(id="london", tier=TIER_1)]
        result = _parse_topical_locations(
            "biarritz:Europe/Paris:FR:Biarritz", locations,
        )
        assert result == ["biarritz"]
        added = next(l for l in locations if l["id"] == "biarritz")
        assert added == {
            "id": "biarritz", "label": "Biarritz",
            "tz": "Europe/Paris", "country": "FR", "tier": TIER_TOPICAL,
        }

    def test_full_tuple_for_existing_tier2_id_retags_in_place(self):
        loc = make_loc(id="singapore", tier=TIER_2, tz="Asia/Singapore", country="SG")
        _parse_topical_locations("singapore:Asia/Singapore:SG:Singapore", [loc])
        assert loc["tier"] == TIER_TOPICAL

    def test_malformed_entry_skipped_others_still_processed(self):
        locations = [make_loc(id="singapore", tier=TIER_2)]
        result = _parse_topical_locations("a:b:c,singapore", locations)
        assert result == ["singapore"]

    def test_multiple_comma_separated_entries_with_whitespace(self):
        locations = [make_loc(id="singapore", tier=TIER_2)]
        result = _parse_topical_locations(
            " singapore , biarritz:Europe/Paris:FR:Biarritz ", locations,
        )
        assert set(result) == {"singapore", "biarritz"}


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.fixture(autouse=True)
    def fake_redis(self):
        fr = FakeRedis()
        with patch.object(poster, "_redis", return_value=fr):
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


class TestRecordRepostCooldown:
    @pytest.fixture(autouse=True)
    def fake_redis(self):
        fr = FakeRedis()
        with patch.object(poster, "_redis", return_value=fr):
            yield fr

    def test_not_on_cooldown_initially(self):
        loc = make_loc(tier=TIER_2)
        assert record_repost_on_cooldown(loc, "month", utc(2026, 6, 16, 16)) is False

    def test_on_cooldown_after_marking(self):
        loc = make_loc(tier=TIER_2)
        now = utc(2026, 6, 16, 16)
        mark_record_reposted(loc, "month", now)
        assert record_repost_on_cooldown(loc, "month", now) is True

    def test_cooldown_scoped_per_month_not_per_day(self):
        loc = make_loc(tier=TIER_2)
        mark_record_reposted(loc, "month", utc(2026, 6, 16, 16))
        # Still within the same month a few days later -> still on cooldown.
        assert record_repost_on_cooldown(loc, "month", utc(2026, 6, 20, 16)) is True
        # A new month is a different scope -> not on cooldown.
        assert record_repost_on_cooldown(loc, "month", utc(2026, 7, 1, 16)) is False

    def test_cooldown_scoped_per_year_not_per_day(self):
        loc = make_loc(tier=TIER_2)
        mark_record_reposted(loc, "year", utc(2026, 6, 16, 16))
        assert record_repost_on_cooldown(loc, "year", utc(2026, 12, 1, 16)) is True
        assert record_repost_on_cooldown(loc, "year", utc(2027, 1, 1, 16)) is False

    def test_different_period_not_affected(self):
        loc = make_loc(tier=TIER_2)
        now = utc(2026, 6, 16, 16)
        mark_record_reposted(loc, "month", now)
        assert record_repost_on_cooldown(loc, "year", now) is False

    def test_different_location_not_affected(self):
        now = utc(2026, 6, 16, 16)
        mark_record_reposted(make_loc(id="london"), "month", now)
        assert record_repost_on_cooldown(make_loc(id="paris"), "month", now) is False


# ---------------------------------------------------------------------------
# Remarkability gate by tier, and the --force --location bypass
# ---------------------------------------------------------------------------


class FakePlatform:
    name = "fake"
    MAX_CHARS = 300
    LINK_IN_TEXT = True
    ATTACH_MEDIA = False

    def __init__(self):
        self.posted = []

    def post_with_image(self, *args, **kwargs):
        self.posted.append(("image", args, kwargs))
        return "https://example.com/1"

    def post_text(self, text):
        self.posted.append(("text", text))
        return "https://example.com/1"


class TestRemarkabilityGateByTier:
    @pytest.fixture(autouse=True)
    def fake_redis(self):
        fr = FakeRedis()
        with patch.object(poster, "_redis", return_value=fr):
            yield fr

    def _run(self, tier, remarkable, skip_gate=False):
        loc = make_loc(id="test_loc", tier=tier)
        platform = FakePlatform()
        post = make_post(
            ranking_warm=1 if remarkable else 7,
            ranking_cold=51,
            gradient_factor=0.0,
        )
        with patch.object(poster, "fetch_temphist_data", return_value=post):
            post_location_period(
                loc, "today", [platform], utc(2026, 6, 16, 16),
                skip_remarkability_gate=skip_gate,
            )
        return platform

    def test_tier1_posts_even_when_not_remarkable(self):
        assert self._run(TIER_1, remarkable=False).posted

    def test_tier2_skips_when_not_remarkable(self):
        assert self._run(TIER_2, remarkable=False).posted == []

    def test_topical_skips_when_not_remarkable(self):
        assert self._run(TIER_TOPICAL, remarkable=False).posted == []

    def test_topical_posts_when_remarkable(self):
        assert self._run(TIER_TOPICAL, remarkable=True).posted

    def test_skip_gate_forces_post_for_non_remarkable_topical(self):
        assert self._run(TIER_TOPICAL, remarkable=False, skip_gate=True).posted

    def test_skip_gate_does_not_affect_dedup_check(self):
        mark_posted("test_loc", "today")
        assert self._run(TIER_TOPICAL, remarkable=False, skip_gate=True).posted == []


# ---------------------------------------------------------------------------
# Remarkability
# ---------------------------------------------------------------------------

def _make_post(**kwargs) -> TempHistPost:
    defaults = dict(
        period="today",
        location_id="london",
        location="London",
        country="GB",
        summary="Test summary",
        average=15.0,
        trend="warming",
        slope=0.1,
        slope_error=None,
        anomaly=None,
        anomaly_std_dev=None,
        share_url="https://example.com/s/1",
        chart_image=b"",
        units="celsius",
        ranking_warm=5,
        ranking_cold=10,
        gradient_factor=0.5,
    )
    defaults.update(kwargs)
    return TempHistPost(**defaults)


class TestRankingPoints:
    def test_rank_1(self):
        assert _ranking_points(1) == 10

    def test_rank_2(self):
        assert _ranking_points(2) == 6

    def test_rank_3(self):
        assert _ranking_points(3) == 4

    def test_rank_4(self):
        assert _ranking_points(4) == 3

    def test_rank_5(self):
        assert _ranking_points(5) == 2

    def test_rank_6(self):
        assert _ranking_points(6) == 1

    def test_rank_7_and_above(self):
        assert _ranking_points(7) == 0
        assert _ranking_points(51) == 0


class TestRemarkabilityScore:
    def test_rank1_no_trend(self):
        assert remarkability_score(1, 51, 0.0) == 10.0

    def test_rank7_moderate_trend(self):
        assert remarkability_score(7, 51, 0.3) == pytest.approx(3.0)

    def test_uses_best_of_warm_cold(self):
        # cold rank 2 beats warm rank 10
        assert remarkability_score(10, 2, 0.0) == 6.0

    def test_trend_adds_to_ranking(self):
        score = remarkability_score(2, 51, 0.5)
        assert score == pytest.approx(6.0 + 5.0)

    def test_negative_gradient_factor_treated_as_absolute(self):
        assert remarkability_score(7, 51, -0.5) == pytest.approx(5.0)


class TestIsRemarkable:
    def test_remarkable_when_score_meets_threshold(self):
        post = _make_post(ranking_warm=1, ranking_cold=51, gradient_factor=0.0)
        assert is_remarkable(post) is True

    def test_not_remarkable_when_score_below_threshold(self):
        post = _make_post(ranking_warm=7, ranking_cold=51, gradient_factor=0.3)
        assert is_remarkable(post) is False

    def test_exactly_at_threshold_is_remarkable(self):
        # rank 6 (1 pt) + gf=0.9 (9.0) = 10.0 — meets threshold
        post = _make_post(ranking_warm=6, ranking_cold=51, gradient_factor=0.9)
        assert is_remarkable(post) is True

    def test_missing_ranking_warm_returns_false(self):
        post = _make_post(ranking_warm=None)
        assert is_remarkable(post) is False

    def test_missing_ranking_cold_returns_false(self):
        post = _make_post(ranking_cold=None)
        assert is_remarkable(post) is False

    def test_missing_gradient_factor_returns_false(self):
        post = _make_post(gradient_factor=None)
        assert is_remarkable(post) is False

    def test_custom_threshold(self):
        post = _make_post(ranking_warm=7, ranking_cold=51, gradient_factor=0.3)
        assert is_remarkable(post, threshold=3.0) is True
