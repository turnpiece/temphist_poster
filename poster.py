"""
TempHist Social Media Poster
Posts temperature history summaries to social media.

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
import logging
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, date
from io import BytesIO
from zoneinfo import ZoneInfo

import httpx
import redis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log.info("poster starting up")


# ---------------------------------------------------------------------------
# Location configuration
# ---------------------------------------------------------------------------

# Tier definitions — controls posting frequency.
# TIER_1: full schedule (daily, weekly, monthly, yearly) — preapproved locations
# TIER_2: full schedule, but only when the day is remarkable — popular non-preapproved locations

TIER_1 = "tier1"
TIER_2 = "tier2"

FAHRENHEIT_COUNTRIES = {"US"}

# Posting schedule per tier.
# None   → every day
# "first"→ 1st of month only
# [0]    → weekdays list (0=Mon … 6=Sun)
TIER_SCHEDULE = {
    TIER_1: {
        "today": None,    # daily
        "week": [0],      # Mondays
        "month": "first", # 1st of month
        "year": "first",  # 1st of month (year-to-date)
    },
    TIER_2: {
        "today": None,    # daily — only posted when remarkable
        "week": [0],      # Mondays — only posted when remarkable
        "month": "first", # 1st of month — only posted when remarkable
        "year": "first",  # 1st of month — only posted when remarkable
    },
}

# Local hour at which to post (24h)
POST_HOUR_LOCAL = 16

# ±minutes around POST_HOUR_LOCAL considered "due"
POST_WINDOW_MINUTES = 15


# ---------------------------------------------------------------------------
# Location loader
# ---------------------------------------------------------------------------

def load_locations() -> list:
    base_url = os.environ["TEMPHIST_API_URL"]
    api_key = os.environ.get("TEMPHIST_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    log.info("loading locations from API")

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        pre_resp = client.get("/v1/locations/preapproved")
        pre_resp.raise_for_status()
        pop_resp = client.get("/v1/locations/popular")
        pop_resp.raise_for_status()

    def _to_loc(raw: dict, tier: str) -> dict:
        return {
            "id": raw["id"],
            "label": raw["name"],
            "tz": raw["timezone"],
            "country": raw["country_code"],
            "tier": tier,
        }

    preapproved_ids = {loc["id"] for loc in pre_resp.json()["locations"]}
    locations = [_to_loc(loc, TIER_1) for loc in pre_resp.json()["locations"]]

    # Add popular locations that aren't already in the preapproved list as tier2.
    # Non-preapproved locations may lack timezone or country_code, so skip those.
    for loc in pop_resp.json()["locations"]:
        if loc["id"] not in preapproved_ids and loc.get("timezone") and loc.get("country_code"):
            locations.append(_to_loc(loc, TIER_2))

    tier1_count = sum(1 for loc in locations if loc["tier"] == TIER_1)
    log.info("loaded %d locations (%d tier1, %d tier2)", len(locations), tier1_count, len(locations) - tier1_count)
    return locations


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
    slope: float
    slope_error: float | None
    anomaly: float | None      # current year's deviation from historical mean
    anomaly_std_dev: float | None  # std dev of historical values for this period
    share_url: str
    chart_image: bytes
    chart_image_url: str = ""
    units: str = "celsius"  # "celsius" | "fahrenheit"
    ranking_warm: int | None = None
    ranking_cold: int | None = None
    gradient_factor: float | None = None



# ---------------------------------------------------------------------------
# TempHist API client
# ---------------------------------------------------------------------------


PERIOD_API_NAMES = {
    "today": "daily",
    "week": "weekly",
    "month": "monthly",
    "year": "yearly",
}


def preferred_units(country: str) -> str:
    return "fahrenheit" if country.upper() in FAHRENHEIT_COUNTRIES else "celsius"


def unit_symbol(units: str) -> str:
    return "°F" if units == "fahrenheit" else "°C"


def record_identifier(loc: dict, now_utc: datetime | None = None) -> str:
    now = now_utc or datetime.now(tz=ZoneInfo("UTC"))
    local = now.astimezone(ZoneInfo(loc["tz"]))
    return local.strftime("%m-%d")


def classify_trend(slope: float) -> str:
    if slope > 0:
        return "warming"
    if slope < 0:
        return "cooling"
    return "stable"


def site_url() -> str:
    return os.environ.get("TEMPHIST_URL", "https://temphist.com").rstrip("/")


def fetch_temphist_data(
    period: str, loc: dict, now_utc: datetime | None = None
) -> TempHistPost:
    log.info("fetching %s/%s", loc["id"], period)
    base_url = os.environ["TEMPHIST_API_URL"]
    api_key = os.environ.get("TEMPHIST_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    units = preferred_units(loc["country"])
    identifier = record_identifier(loc, now_utc)
    api_period = PERIOD_API_NAMES[period]
    ref_year = (
        (now_utc or datetime.now(tz=ZoneInfo("UTC")))
        .astimezone(ZoneInfo(loc["tz"]))
        .year
    )

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        meta_resp = client.get(
            f"/v1/records/{api_period}/{loc['id']}/{identifier}/meta",
            params={"unit_group": units},
        )
        meta_resp.raise_for_status()
        meta_body = meta_resp.json()
        meta = meta_body["data"]

        if ref_year in meta_body.get("metadata", {}).get("missing_years", []):
            raise ValueError(f"No data for {ref_year} at {loc['id']}/{period}")

        summary = meta["summary"]
        average = meta["average"]["mean"]
        slope = meta["trend"]["slope"]
        slope_error = meta["trend"].get("slope_error")
        gradient_factor = meta["trend"].get("gradient_factor")
        trend = classify_trend(slope)
        anomaly = meta.get("current_anomaly")
        anomaly_std_dev = meta["average"].get("standard_deviation")
        ranking = meta.get("ranking", {})

        share_resp = client.post(
            "/v1/shares",
            json={
                "location": loc["label"],
                "period": api_period,
                "identifier": identifier,
                "ref_year": ref_year,
                "unit": units,
            },
        )
        share_resp.raise_for_status()
        share = share_resp.json()

        chart_resp = client.get(f"/v1/og/{share['id']}.png")
        chart_resp.raise_for_status()

    return TempHistPost(
        period=period,
        location_id=loc["id"],
        location=loc["label"],
        country=loc["country"],
        summary=summary,
        average=average,
        trend=trend,
        slope=slope,
        slope_error=slope_error,
        anomaly=anomaly,
        anomaly_std_dev=anomaly_std_dev,
        share_url=f"{site_url()}{share['url']}",
        chart_image=chart_resp.content,
        chart_image_url=f"{base_url}/v1/og/{share['id']}.png",
        units=units,
        ranking_warm=ranking.get("warm"),
        ranking_cold=ranking.get("cold"),
        gradient_factor=gradient_factor,
    )



# ---------------------------------------------------------------------------
# Post formatters
# ---------------------------------------------------------------------------

PERIOD_LABELS = {
    "today": "Today",
    "week": "This week",
    "month": "This month",
    "year": "This year",
}


PERIOD_TAGS = {
    "today": "#weather",
    "week": "#weather #ClimateData",
    "month": "#ClimateData #ClimateChange",
    "year": "#ClimateData #ClimateChange",
}


def _anomaly_icon(post: TempHistPost) -> str:
    if post.anomaly is None or post.anomaly_std_dev is None:
        return ""
    if post.anomaly > post.anomaly_std_dev:
        return "🌡️"
    if post.anomaly < -post.anomaly_std_dev:
        return "❄️"
    return ""


def _trend_icon(post: TempHistPost) -> str:
    if post.slope_error is not None:
        if post.slope > post.slope_error:
            return "📈"
        if post.slope < -post.slope_error:
            return "📉"
        return ""
    return "📈" if post.slope > 0 else ("📉" if post.slope < 0 else "")


def _build_post_body(post: TempHistPost) -> str:
    icons = _anomaly_icon(post) + _trend_icon(post)
    label = PERIOD_LABELS.get(post.period, post.period.capitalize())
    sym = unit_symbol(post.units)
    tags = PERIOD_TAGS.get(post.period, "#weather")
    loc_tag = f"#{post.location.replace(' ', '')}"
    slope_sign = "+" if post.slope > 0 else ("-" if post.slope < 0 else "")
    slope_str = f"{slope_sign}{abs(post.slope):.2f}"
    if post.slope_error is not None:
        slope_str += f" ± {post.slope_error:.2f}"

    return (
        f"{label} in {post.location}{' ' + icons if icons else ''}\n\n"
        f"{post.summary}\n\n"
        f"Average: {post.average:.1f}{sym} · Trend: {slope_str} {sym}/decade\n\n"
        f"{tags} {loc_tag} #TempHist\n\n"
        f"{post.share_url}"
    )


def _trim_to_limit(body: str, summary: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    overhead = len(body) - len(summary)
    allowed = max_chars - overhead - 3
    trimmed = summary[: max(0, allowed)] + "..."
    return body.replace(summary, trimmed)


def format_location_post(post: TempHistPost, max_chars: int = 300) -> str:
    body = _build_post_body(post)
    return _trim_to_limit(body, post.summary, max_chars)



# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_redis_client = None


def _redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        url = os.environ["REDIS_URL"]
        log.info("connecting to Redis")
        _redis_client = redis.from_url(url)
        _redis_client.ping()
        log.info("Redis connected")
    return _redis_client


def already_posted(location_id: str, period: str) -> bool:
    return bool(
        _redis().exists(f"poster:posted:{location_id}:{period}:{date.today().isoformat()}")
    )


def mark_posted(location_id: str, period: str) -> None:
    key = f"poster:posted:{location_id}:{period}:{date.today().isoformat()}"
    _redis().set(key, datetime.now().isoformat(), ex=60 * 60 * 24 * 35)  # 35-day TTL


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



# ---------------------------------------------------------------------------
# Remarkability gate (tier 2)
# ---------------------------------------------------------------------------


def _ranking_points(rank: int) -> int:
    return {1: 10, 2: 6, 3: 4, 4: 3, 5: 2, 6: 1}.get(rank, 0)


def remarkability_score(ranking_warm: int, ranking_cold: int, gradient_factor: float) -> float:
    best_rank = min(ranking_warm, ranking_cold)
    return abs(gradient_factor) * 10 + _ranking_points(best_rank)


def is_remarkable(data: TempHistPost, threshold: float = 10.0) -> bool:
    if data.ranking_warm is None or data.ranking_cold is None or data.gradient_factor is None:
        return False
    return remarkability_score(data.ranking_warm, data.ranking_cold, data.gradient_factor) >= threshold


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
        log.error("fetch %s/%s failed: %s", loc_id, period, exc)
        print(f"  ✗ fetch {loc_id}/{period}: {exc}", file=sys.stderr)
        return

    if loc["tier"] == TIER_2 and not is_remarkable(data):
        if dry_run and data.ranking_warm is not None and data.ranking_cold is not None and data.gradient_factor is not None:
            score = remarkability_score(data.ranking_warm, data.ranking_cold, data.gradient_factor)
            log.info("[DRY RUN] skip %s/%s — not remarkable (score=%.1f, warm_rank=%s, cold_rank=%s, gf=%.2f)",
                     loc_id, period, score, data.ranking_warm, data.ranking_cold, data.gradient_factor)
        else:
            log.info("skip %s/%s — not remarkable", loc_id, period)
        return

    sym = unit_symbol(data.units)
    alt_text = (
        f"Temperature chart for {data.location}, {PERIOD_LABELS[period].lower()}. "
        f"Avg {data.average:.1f}{sym}, trend: {data.trend}."
    )

    for platform in platforms:
        text = format_location_post(data, max_chars=platform.MAX_CHARS)

        if dry_run:
            log.info("[DRY RUN] %s | %s | %s (%d chars)", platform.name.upper(), loc_id, period, len(text))
            for line in text.splitlines():
                log.info("  %s", line)
            log.info("  [image: %s]", data.chart_image_url)
            continue

        try:
            url = platform.post_with_image(text, data.chart_image, alt_text)
            print(f"  ✓ {platform.name} | {loc_id} | {period}: {url}")
        except Exception as exc:
            print(f"  ✗ {platform.name} | {loc_id} | {period}: {exc}", file=sys.stderr)

    if not dry_run:
        mark_posted(loc_id, period)



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
        choices=["today", "week", "month", "year"],
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


def _resolve_locations(location_arg: str | None, locations: list) -> list:
    if not location_arg:
        return locations
    matched = [loc for loc in locations if loc["id"] == location_arg]
    if not matched:
        log.error("unknown location: %s", location_arg)
        sys.exit(1)
    return matched


def _periods_for_location(
    loc: dict,
    force: str | None,
    location_arg: str | None,
    now_utc: datetime,
    dry_run: bool = False,
) -> list:
    if force:
        return [force]
    if dry_run or is_posting_time(loc, now_utc):
        return periods_due_today(loc, now_utc)
    return []


def main():
    args = parse_args()
    log.info("args: dry_run=%s force=%s location=%s platforms=%s",
             args.dry_run, args.force, args.location, args.platforms)
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    log.info("now_utc=%s", now_utc.isoformat())
    all_locations = load_locations()
    platforms = make_platforms(args.platforms, args.dry_run)
    locations = _resolve_locations(args.location, all_locations)

    # Per-location posts
    for loc in locations:
        periods = _periods_for_location(
            loc, args.force, args.location, now_utc, dry_run=args.dry_run
        )
        if not periods:
            log.info("skip %s — no periods due", loc["id"])
            continue

        log.info("posting %s: %s", loc["id"], periods)
        for period in periods:
            post_location_period(loc, period, platforms, dry_run=args.dry_run)

    log.info("poster done")


if __name__ == "__main__":
    main()
