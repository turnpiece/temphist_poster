# TempHist Poster

Automated social media bot that posts temperature history charts from [TempHist](https://temphist.com) to Bluesky and Mastodon.

Each afternoon, it checks which configured cities are at 4 PM local time and posts a temperature chart with a short summary for each one. Post frequency scales with tier — major English-speaking cities get daily + weekly + monthly posts; secondary cities get daily only; a third tier (planned for v2) gates posts on statistically remarkable days.

A weekly aggregate post fires every Friday with a cross-location snapshot.

## What gets posted

Each per-location post includes:
- A temperature chart image for the period (today / this week / this month / this year)
- Average temperature and trend (warming / stable / cooling)
- A shareable link to the TempHist chart
- Relevant hashtags

The Friday aggregate is text-only — a tally of how many cities are warming, cooling, or stable, with callouts for the fastest warming and most cooling locations.

## Deployment

Designed to run on Railway as a cron service that fires every 30 minutes. The script checks the schedule on each invocation and posts only when a location is due.

```
# Procfile / start command
python poster.py
```

Mount a persistent Railway volume and set `POST_LOG_PATH` to avoid duplicate posts across restarts.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TEMPHIST_API_URL` | Yes | Base URL of the TempHist API |
| `TEMPHIST_API_KEY` | No | Bearer token for authenticated API access |
| `BLUESKY_HANDLE` | Yes (Bluesky) | Bluesky handle, e.g. `temphist.bsky.social` |
| `BLUESKY_APP_PASSWORD` | Yes (Bluesky) | Bluesky app password |
| `MASTODON_ACCESS_TOKEN` | Yes (Mastodon) | Mastodon OAuth access token |
| `MASTODON_API_BASE_URL` | Yes (Mastodon) | Mastodon instance URL, e.g. `https://mastodon.social` |
| `POST_LOG_PATH` | No | Path to the deduplication log file (default: `/tmp/temphist_post_log.json`) |
| `TOPICAL_LOCATIONS` | No | Comma-separated list of admin-selected locations to add to the posting pool. Each entry is either an `id` already present in that run's preapproved/popular results, or a self-contained `id:tz:country:label` tuple for a location not yet surfaced by those endpoints, e.g. `TOPICAL_LOCATIONS=bordeaux,biarritz:Europe/Paris:FR:Biarritz`. Still gated by the same remarkability check as Tier 2. |

## Installation

```bash
pip install atproto httpx "Mastodon.py" python-dotenv
```

Copy `.env.example` to `.env` and fill in the values above.

## Usage

```bash
# Normal run — checks schedule, posts if due
python poster.py

# Preview today's due posts without posting or authenticating
# (bypasses the 4pm local-time window; still respects day-of-week rules)
python poster.py --dry-run

# Force a specific period for all (or one) location
python poster.py --force today
python poster.py --force week --location london

# Force a specific location past the remarkability gate too (only when
# --force is combined with --location — a plain --force still respects it)
python poster.py --force today --location bordeaux

# Post only to one platform
python poster.py --platforms bluesky

# Force the weekly aggregate post
python poster.py --force aggregate --dry-run
```

## Location tiers

| Tier | Locations | Schedule |
|---|---|---|
| Tier 1 | London, New York, Los Angeles, Chicago, Sydney, Toronto, Dublin, Auckland | Daily · Weekly (Mon) · Monthly (1st) · Year-to-date (1st) |
| Tier 2 | Singapore, Johannesburg, Nairobi, Mumbai | Daily only |
| Tier 3 | Tokyo, Amsterdam, Dubai | Planned for v2 — remarkable days only |
| Topical | Manually set via `TOPICAL_LOCATIONS` | Daily · Weekly (Mon) · Monthly (1st) · Year-to-date (1st) — same as Tier 1/2, gated by remarkability like Tier 2 |

Topical locations let the operator manually add a location to the pool — e.g. reacting to a
heatwave making a location's month or year record-setting — without waiting for it to
accumulate enough organic app selections to surface via the popular-locations API. They get
the same full posting schedule as Tier 1/2, but remain subject to the remarkability gate:
topicality is how the location gets considered, not a way to bypass the check that its data is
actually noteworthy. A location already in the preapproved (Tier 1) list is left unchanged if
also listed in `TOPICAL_LOCATIONS`, since Tier 1 already posts unconditionally.

## Deduplication

A JSON log file tracks posts keyed by `location:period:date`. Entries older than the current calendar month are pruned on each write. Set `POST_LOG_PATH` to a path on a persistent volume in production.

## Adding a location

Add an entry to `LOCATIONS` in `poster.py`:

```python
{
    "id": "cape_town",        # used in API calls and deduplication keys
    "label": "Cape Town",     # display name in posts
    "tz": "Africa/Johannesburg",
    "country": "ZA",
    "tier": TIER_2,
}
```

US locations automatically use Fahrenheit; all others use Celsius.
