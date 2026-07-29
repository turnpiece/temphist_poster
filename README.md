# TempHist Poster

Automated social media bot that posts temperature history charts from [TempHist](https://temphist.com) to Bluesky and Mastodon.

Each afternoon, it checks which locations are at 4 PM local time and posts a temperature chart with a short summary for each one that's due. Locations and their tier come from the TempHist API at runtime, not a static list in this repo — see "Location tiers" below. Preapproved locations get daily + weekly + monthly + yearly posts unconditionally; popular and topical locations get the same schedule but only on a statistically remarkable day.

## What gets posted

Each per-location post includes:
- A temperature chart image for the period (today / this week / this month / this year)
- Average temperature and trend (warming / stable / cooling)
- A shareable link to the TempHist chart
- Relevant hashtags

## Deployment

Designed to run on Railway as a cron service that fires every 30 minutes. The script checks the schedule on each invocation and posts only when a location is due.

```
# Procfile / start command
python poster.py
```

Deduplication and the opportunistic-record cooldown are stored in Redis (`REDIS_URL`) — see "Deduplication" below — so no persistent volume or local file is needed.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TEMPHIST_API_URL` | Yes | Base URL of the TempHist API |
| `TEMPHIST_API_KEY` | No | Bearer token for authenticated API access |
| `TEMPHIST_URL` | No | Public site URL used to build share links (default: `https://temphist.com`) |
| `REDIS_URL` | Yes | Redis connection URL — used for post deduplication and the opportunistic-record repost cooldown |
| `BLUESKY_HANDLE` | Yes (Bluesky) | Bluesky handle, e.g. `temphist.bsky.social` |
| `BLUESKY_APP_PASSWORD` | Yes (Bluesky) | Bluesky app password |
| `MASTODON_ACCESS_TOKEN` | Yes (Mastodon) | Mastodon OAuth access token |
| `MASTODON_API_BASE_URL` | Yes (Mastodon) | Mastodon instance URL, e.g. `https://mastodon.social` |
| `TOPICAL_LOCATIONS` | No | Comma-separated list of admin-selected locations to add to the posting pool. Each entry is either an `id` already present in that run's preapproved/popular results, or a self-contained `id:tz:country:label` tuple for a location not yet surfaced by those endpoints, e.g. `TOPICAL_LOCATIONS=bordeaux,biarritz:Europe/Paris:FR:Biarritz`. Still gated by the same remarkability check as Tier 2. Run `python resolve_topical_location.py "<city name>"` to look up the right entry instead of assembling it by hand. |

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
```

## Location tiers

Tiers are assigned dynamically on every run by `load_locations()` — nothing is hardcoded in
this repo:

| Tier | Source | Schedule | Gate |
|---|---|---|---|
| Tier 1 (preapproved) | `/v1/locations/preapproved` API response | Daily · Weekly (Mon) · Monthly (1st) · Year-to-date (1st) | None — posts unconditionally |
| Tier 2 (popular) | `/v1/locations/popular` API response, excluding anything already preapproved | Same as Tier 1, plus month/year checked opportunistically every day | Only posts on a statistically remarkable day |
| Topical | Manually set via `TOPICAL_LOCATIONS` | Same as Tier 1/2 | Same remarkability gate as Tier 2 |

Topical locations let the operator manually add a location to the pool — e.g. reacting to a
heatwave making a location's month or year record-setting — without waiting for it to
accumulate enough organic app selections to surface via the popular-locations API. They get
the same full posting schedule as Tier 1/2, but remain subject to the remarkability gate:
topicality is how the location gets considered, not a way to bypass the check that its data is
actually noteworthy. A location already in the preapproved (Tier 1) list is left unchanged if
also listed in `TOPICAL_LOCATIONS`, since Tier 1 already posts unconditionally.

## Deduplication

Post state lives in Redis (`REDIS_URL`), keyed per location/period/day:
- `poster:posted:{location_id}:{period}:{date}` marks a period as posted for the day (35-day TTL).
- `poster:record-cooldown:{location_id}:{period}:{scope}` throttles opportunistic month/year
  rechecks to once every 10 days per still-standing record window (a calendar month or year),
  so a record that holds for weeks doesn't get reposted every day.

No local file or persistent volume is needed — any Redis instance that survives restarts is
sufficient.

## Adding a location

Locations aren't hardcoded in this repo — `load_locations()` fetches them from the TempHist API
on every run:

- **Tier 1 (preapproved)**: curated in the API's own location data. To add one permanently, add
  it there (in the `api` repo), not here.
- **Tier 2 (popular)**: surfaces automatically once enough users select it in the app — nothing
  to do here.
- **Topical**: for an immediate, admin-controlled addition (e.g. reacting to a location's
  temperature record becoming newsworthy), set `TOPICAL_LOCATIONS` — see the env var table
  above. Run `python resolve_topical_location.py "<city name>"` to look up the right entry
  instead of assembling it by hand.

US locations automatically use Fahrenheit; all others use Celsius.
