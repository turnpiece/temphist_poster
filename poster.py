"""
TempHist Social Media Poster
Posts temperature history summaries and cross-location aggregates to social media.

Usage:
    python poster.py                    # run normally (checks schedule)
    python poster.py --dry-run          # preview without posting
    python poster.py --force today      # force a specific period
    python poster.py --location london  # single location only

Deploy on Railway with a cron that runs every 30 minutes.
The script checks which locations are at ~16:00 local time and posts for those.

Dependencies:
    pip install atproto httpx Mastodon.py python-dotenv
"""

import argparse
import hashlib
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Location configuration
# ---------------------------------------------------------------------------

# Tier definitions — controls posting behaviour.
# TIER_1: post all periods on their full schedule
# TIER_2: daily posts only in v1
# TIER_3: conditional/remarkable posts only — stub for v2

TIER_1 = "tier1"
TIER_2 = "tier2"
TIER_3 = "tier3"

FAHRENHEIT_COUNTRIES = {"US"}

LOCATIONS = [
    # --- Tier 1: English-speaking, high-interest, full schedule ---
    {
        "id": "london",
        "label": "London",
        "tz": "Europe/London",
        "country": "GB",
        "tier": TIER_1,
    },
    {
        "id": "new_york",
        "label": "New York",
        "tz": "America/New_York",
        "country": "US",
        "tier": TIER_1,
    },
    {
        "id": "los_angeles",
        "label": "Los Angeles",
        "tz": "America/Los_Angeles",
        "country": "US",
        "tier": TIER_1,
    },
    {
        "id": "chicago",
        "label": "Chicago",
        "tz": "America/Chicago",
        "country": "US",
        "tier": TIER_1,
    },
    {
        "id": "sydney",
        "label": "Sydney",
        "tz": "Australia/Sydney",
        "country": "AU",
        "tier": TIER_1,
    },
    {
        "id": "toronto",
        "label": "Toronto",
        "tz": "America/Toronto",
        "country": "CA",
        "tier": TIER_1,
    },
    {
        "id": "dublin",
        "label": "Dublin",
        "tz": "Europe/Dublin",
        "country": "IE",
        "tier": TIER_1,
    },
    {
        "id": "auckland",
        "label": "Auckland",
        "tz": "Pacific/Auckland",
        "country": "NZ",
        "tier": TIER_1,
    },
    # --- Tier 2: English widely spoken, daily only in v1 ---
    {
        "id": "singapore",
        "label": "Singapore",
        "tz": "Asia/Singapore",
        "country": "SG",
        "tier": TIER_2,
    },
    {
        "id": "johannesburg",
        "label": "Johannesburg",
        "tz": "Africa/Johannesburg",
        "country": "ZA",
        "tier": TIER_2,
    },
    {
        "id": "nairobi",
        "label": "Nairobi",
        "tz": "Africa/Nairobi",
        "country": "KE",
        "tier": TIER_2,
    },
    {
        "id": "mumbai",
        "label": "Mumbai",
        "tz": "Asia/Kolkata",
        "country": "IN",
        "tier": TIER_2,
    },
    # --- Tier 3: conditional/remarkable posts only — v2 ---
    # is_remarkable(loc, period) will gate these; skipped entirely in v1.
    {
        "id": "tokyo",
        "label": "Tokyo",
        "tz": "Asia/Tokyo",
        "country": "JP",
        "tier": TIER_3,
    },
    {
        "id": "amsterdam",
        "label": "Amsterdam",
        "tz": "Europe/Amsterdam",
        "country": "NL",
        "tier": TIER_3,
    },
    {
        "id": "dubai",
        "label": "Dubai",
        "tz": "Asia/Dubai",
        "country": "AE",
        "tier": TIER_3,
    },
]

# Posting schedule per tier.
# None   → every day
# "first"→ 1st of month only
# [0]    → weekdays list (0=Mon … 6=Sun)
TIER_SCHEDULE = {
    TIER_1: {
        "today": None,  # daily
        "week": [0],  # Mondays
        "month": "first",  # 1st of month
        "year": "first",  # monthly (year-to-date is useful for climate audience)
    },
    TIER_2: {
        "today": None,  # daily only
    },
    TIER_3: {
        # v2: "today": "remarkable_only"
    },
}

# Aggregate weekly post fires on Fridays
AGGREGATE_POST_DAY = 4  # 0=Mon

# Local hour at which to post (24h)
POST_HOUR_LOCAL = 16

# ±minutes around POST_HOUR_LOCAL considered "due"
POST_WINDOW_MINUTES = 15


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TempHistPost:
    period: str
    location_id: str
    location: str
    country: str
    summary: str
    average: float
    trend: str  # "warming" | "cooling" | "stable"
    share_url: str
    chart_image: bytes
    units: str = "metric"  # "metric" | "us"


@dataclass
class LocationSummary:
    """Lightweight summary used for the aggregate post — no image needed."""

    location: str
    average: float
    trend: str
    units: str


@dataclass
class AggregateData:
    date: date
    summaries: list

    @property
    def warming_count(self):
        return sum(1 for s in self.summaries if s.trend.lower() == "warming")

    @property
    def cooling_count(self):
        return sum(1 for s in self.summaries if s.trend.lower() == "cooling")

    @property
    def stable_count(self):
        return sum(1 for s in self.summaries if s.trend.lower() == "stable")

    @property
    def most_warming(self):
        warming = [s for s in self.summaries if s.trend.lower() == "warming"]
        return max(warming, key=lambda s: s.average, default=None)

    @property
    def most_cooling(self):
        cooling = [s for s in self.summaries if s.trend.lower() == "cooling"]
        return min(cooling, key=lambda s: s.average, default=None)


# ---------------------------------------------------------------------------
# TempHist API client
# ---------------------------------------------------------------------------


def preferred_units(country: str) -> str:
    return "us" if country.upper() in FAHRENHEIT_COUNTRIES else "metric"


def unit_symbol(units: str) -> str:
    return "°F" if units == "us" else "°C"


def fetch_temphist_data(period: str, loc: dict) -> TempHistPost:
    base_url = os.environ["TEMPHIST_API_URL"]
    api_key = os.environ.get("TEMPHIST_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    units = preferred_units(loc["country"])

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        resp = client.get(
            f"/summary/{loc['id']}",
            params={"period": period, "unitGroup": units},
        )
        resp.raise_for_status()
        data = resp.json()

        chart_resp = client.get(
            f"/chart/{loc['id']}",
            params={"period": period, "unitGroup": units},
        )
        chart_resp.raise_for_status()

    return TempHistPost(
        period=period,
        location_id=loc["id"],
        location=loc["label"],
        country=loc["country"],
        summary=data["summary"],
        average=data["average"],
        trend=data["trend"],
        share_url=data["share_url"],
        chart_image=chart_resp.content,
        units=units,
    )


def fetch_aggregate_data(period: str = "today") -> AggregateData:
    """Fetch lightweight summaries for all tier 1 locations (no chart images)."""
    base_url = os.environ["TEMPHIST_API_URL"]
    api_key = os.environ.get("TEMPHIST_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    tier1 = [loc for loc in LOCATIONS if loc["tier"] == TIER_1]
    summaries = []

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        for loc in tier1:
            units = preferred_units(loc["country"])
            resp = client.get(
                f"/summary/{loc['id']}",
                params={"period": period, "unitGroup": units},
            )
            if resp.is_success:
                d = resp.json()
                summaries.append(
                    LocationSummary(
                        location=loc["label"],
                        average=d["average"],
                        trend=d["trend"],
                        units=units,
                    )
                )

    return AggregateData(date=date.today(), summaries=summaries)


# ---------------------------------------------------------------------------
# Post formatters
# ---------------------------------------------------------------------------

PERIOD_LABELS = {
    "today": "Today",
    "week": "This week",
    "month": "This month",
    "year": "This year",
}

TREND_EMOJI = {
    "warming": "🌡️📈",
    "cooling": "❄️📉",
    "stable": "➡️",
}

PERIOD_TAGS = {
    "today": "#weather",
    "week": "#weather #ClimateData",
    "month": "#ClimateData #ClimateChange",
    "year": "#ClimateData #ClimateChange",
}


def format_location_post(post: TempHistPost, max_chars: int = 300) -> str:
    trend_icon = TREND_EMOJI.get(post.trend.lower(), "🌡️")
    label = PERIOD_LABELS.get(post.period, post.period.capitalize())
    sym = unit_symbol(post.units)
    tags = PERIOD_TAGS.get(post.period, "#weather")
    loc_tag = f"#{post.location.replace(' ', '')}"

    body = (
        f"{label} in {post.location} {trend_icon}\n\n"
        f"{post.summary}\n\n"
        f"Avg: {post.average:.1f}{sym} · Trend: {post.trend.capitalize()}\n\n"
        f"{tags} {loc_tag} #TempHist\n\n"
        f"{post.share_url}"
    )

    if len(body) > max_chars:
        overhead = len(body) - len(post.summary)
        allowed = max_chars - overhead - 3
        trimmed = post.summary[: max(0, allowed)] + "..."
        body = body.replace(post.summary, trimmed)

    return body


def format_aggregate_post(agg: AggregateData, max_chars: int = 300) -> str:
    """
    Weekly cross-location climate snapshot. Text only — no image.

    Example:
        🌍 TempHist weekly snapshot — 18 Apr

        8 locations today:
        📈 5 warming · ➡️ 2 stable · ❄️ 1 cooling

        Fastest warming: London
        Most cooling: Auckland

        #ClimateData #ClimateChange #TempHist
    """
    today = agg.date.strftime("%-d %b")
    warmest = agg.most_warming
    coolest = agg.most_cooling

    lines = [
        f"🌍 TempHist weekly snapshot — {today}",
        "",
        f"{len(agg.summaries)} locations today:",
        f"📈 {agg.warming_count} warming · ➡️ {agg.stable_count} stable · ❄️ {agg.cooling_count} cooling",
    ]

    if warmest:
        lines.append(f"\nFastest warming: {warmest.location}")
    if coolest:
        lines.append(f"Most cooling: {coolest.location}")

    lines += [
        "",
        "#ClimateData #ClimateChange #TempHist",
    ]

    return "\n".join(lines)[:max_chars]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
# File-based for v1. On Railway, mount a persistent volume and set POST_LOG_PATH.
# For v2, swap _load_log/_save_log for Firebase reads/writes.

LOG_PATH = Path(
    os.environ.get("POST_LOG_PATH", str(Path.home() / ".cache" / "temphist" / "post_log.json"))
)


def _load_log() -> dict:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_log(log: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(log, indent=2))


def _post_key(location_id: str, period: str) -> str:
    day = date.today().isoformat()
    return hashlib.md5(f"{location_id}:{period}:{day}".encode()).hexdigest()


def already_posted(location_id: str, period: str) -> bool:
    return _post_key(location_id, period) in _load_log()


def mark_posted(location_id: str, period: str) -> None:
    log = _load_log()
    log[_post_key(location_id, period)] = datetime.now().isoformat()
    # Prune entries from before this month
    cutoff = date.today().replace(day=1).isoformat()
    _save_log({k: v for k, v in log.items() if v >= cutoff})


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def is_posting_time(loc: dict, now_utc: datetime) -> bool:
    local_now = now_utc.astimezone(ZoneInfo(loc["tz"]))
    diff = abs(local_now.hour * 60 + local_now.minute - POST_HOUR_LOCAL * 60)
    return diff <= POST_WINDOW_MINUTES


def periods_due_today(loc: dict, now_utc: datetime) -> list:
    schedule = TIER_SCHEDULE.get(loc["tier"], {})
    today = now_utc.date()
    due = []

    for period, rule in schedule.items():
        if (
            rule is None
            or (rule == "first" and today.day == 1)
            or (isinstance(rule, list) and today.weekday() in rule)
        ):
            due.append(period)

    return due


def is_aggregate_due(now_utc: datetime) -> bool:
    return (
        now_utc.weekday() == AGGREGATE_POST_DAY
        and abs(now_utc.hour * 60 + now_utc.minute - POST_HOUR_LOCAL * 60)
        <= POST_WINDOW_MINUTES
    )


# ---------------------------------------------------------------------------
# v2 stub: remarkable-day conditional posting
# ---------------------------------------------------------------------------


def is_remarkable(loc: dict, period: str) -> bool:
    """
    v2: return True if today's data for this location/period is statistically
    notable (e.g. record high, top-5% anomaly, longest warming streak).

    Will need:
    - A historical baseline endpoint on the TempHist API
    - A threshold definition (configurable per location/period)
    - Deduplication: don't post "warmest month-to-date" two days running

    Raises NotImplementedError until implemented.
    """
    raise NotImplementedError("Remarkable-day logic not yet implemented (v2)")


# ---------------------------------------------------------------------------
# Platform abstraction
# ---------------------------------------------------------------------------


class SocialPlatform(ABC):
    name: str
    MAX_CHARS: int

    @abstractmethod
    def post_with_image(self, text: str, image: bytes, alt_text: str = "") -> str: ...

    @abstractmethod
    def post_text(self, text: str) -> str: ...


class BlueskyPlatform(SocialPlatform):
    name = "bluesky"
    MAX_CHARS = 300

    def __init__(self):
        from atproto import Client

        self.client = Client()
        self.client.login(
            os.environ["BLUESKY_HANDLE"],
            os.environ["BLUESKY_APP_PASSWORD"],
        )

    def post_with_image(self, text: str, image: bytes, alt_text: str = "") -> str:
        upload = self.client.upload_blob(image)
        response = self.client.send_image(
            text=text, image=upload.blob, image_alt=alt_text
        )
        rkey = response.uri.split("/")[-1]
        return f"https://bsky.app/profile/{os.environ['BLUESKY_HANDLE']}/post/{rkey}"

    def post_text(self, text: str) -> str:
        response = self.client.send_post(text=text)
        rkey = response.uri.split("/")[-1]
        return f"https://bsky.app/profile/{os.environ['BLUESKY_HANDLE']}/post/{rkey}"


class MastodonPlatform(SocialPlatform):
    name = "mastodon"
    MAX_CHARS = 500

    def __init__(self):
        from mastodon import Mastodon

        self.client = Mastodon(
            access_token=os.environ["MASTODON_ACCESS_TOKEN"],
            api_base_url=os.environ["MASTODON_API_BASE_URL"],
        )

    def post_with_image(self, text: str, image: bytes, alt_text: str = "") -> str:
        media = self.client.media_post(
            BytesIO(image), mime_type="image/png", description=alt_text
        )
        status = self.client.status_post(
            text, media_ids=[media["id"]], visibility="public"
        )
        return status["url"]

    def post_text(self, text: str) -> str:
        return self.client.status_post(text, visibility="public")["url"]


PLATFORMS: dict[str, type] = {
    "bluesky": BlueskyPlatform,
    "mastodon": MastodonPlatform,
}


# ---------------------------------------------------------------------------
# Posting actions
# ---------------------------------------------------------------------------


def post_location_period(
    loc: dict,
    period: str,
    platforms: list,
    dry_run: bool = False,
) -> None:
    loc_id = loc["id"]

    if not dry_run and already_posted(loc_id, period):
        print(f"  skip {loc_id}/{period} — already posted today")
        return

    try:
        data = fetch_temphist_data(period, loc)
    except Exception as exc:
        print(f"  ✗ fetch {loc_id}/{period}: {exc}", file=sys.stderr)
        return

    sym = unit_symbol(data.units)
    alt_text = (
        f"Temperature chart for {data.location}, {PERIOD_LABELS[period].lower()}. "
        f"Avg {data.average:.1f}{sym}, trend: {data.trend}."
    )

    for platform in platforms:
        text = format_location_post(data, max_chars=platform.MAX_CHARS)

        if dry_run:
            print(
                f"\n── {platform.name.upper()} | {loc_id} | {period} ({len(text)} chars) ──"
            )
            print(text)
            continue

        try:
            url = platform.post_with_image(text, data.chart_image, alt_text)
            print(f"  ✓ {platform.name} | {loc_id} | {period}: {url}")
        except Exception as exc:
            print(f"  ✗ {platform.name} | {loc_id} | {period}: {exc}", file=sys.stderr)

    if not dry_run:
        mark_posted(loc_id, period)


def post_aggregate(platforms: list, dry_run: bool = False) -> None:
    if not dry_run and already_posted("__aggregate__", "week"):
        print("  skip aggregate — already posted today")
        return

    try:
        agg = fetch_aggregate_data(period="today")
    except Exception as exc:
        print(f"  ✗ fetch aggregate: {exc}", file=sys.stderr)
        return

    for platform in platforms:
        text = format_aggregate_post(agg, max_chars=platform.MAX_CHARS)

        if dry_run:
            print(f"\n── {platform.name.upper()} | AGGREGATE ({len(text)} chars) ──")
            print(text)
            continue

        try:
            url = platform.post_text(text)
            print(f"  ✓ {platform.name} | aggregate: {url}")
        except Exception as exc:
            print(f"  ✗ {platform.name} | aggregate: {exc}", file=sys.stderr)

    if not dry_run:
        mark_posted("__aggregate__", "week")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def make_platforms(names: list, dry_run: bool) -> list:
    """Instantiate platform objects. On dry-run, skip auth."""
    if dry_run:
        platforms = []
        for name in names:
            cls = PLATFORMS[name]
            obj = object.__new__(cls)
            obj.name = cls.name
            obj.MAX_CHARS = cls.MAX_CHARS
            platforms.append(obj)
        return platforms
    return [PLATFORMS[name]() for name in names]


def parse_args():
    parser = argparse.ArgumentParser(description="Post TempHist data to social media.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        choices=["today", "week", "month", "year", "aggregate"],
        help="Force a specific period, bypassing schedule check",
    )
    parser.add_argument("--location", help="Run for one location ID only")
    parser.add_argument(
        "--platforms",
        nargs="+",
        choices=list(PLATFORMS.keys()),
        default=list(PLATFORMS.keys()),
    )
    return parser.parse_args()


def _resolve_locations(location_arg: str | None) -> list:
    if not location_arg:
        return LOCATIONS
    matched = [loc for loc in LOCATIONS if loc["id"] == location_arg]
    if not matched:
        print(f"Unknown location: {location_arg}", file=sys.stderr)
        sys.exit(1)
    return matched


def _periods_for_location(loc: dict, force: str | None, location_arg: str | None, now_utc: datetime) -> list:
    if loc["tier"] == TIER_3 and not force and not location_arg:
        return []
    if force and force != "aggregate":
        return [force]
    if is_posting_time(loc, now_utc):
        return periods_due_today(loc, now_utc)
    return []


def main():
    args = parse_args()
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    platforms = make_platforms(args.platforms, args.dry_run)
    locations = _resolve_locations(args.location)

    # Aggregate post (Friday, or forced)
    if args.force == "aggregate" or (
        not args.force and not args.location and is_aggregate_due(now_utc)
    ):
        print("Posting aggregate...")
        post_aggregate(platforms, dry_run=args.dry_run)

    # Per-location posts
    for loc in locations:
        periods = _periods_for_location(loc, args.force, args.location, now_utc)
        if not periods:
            continue

        print(f"{loc['label']} ({loc['tier']}):")
        for period in periods:
            post_location_period(loc, period, platforms, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
