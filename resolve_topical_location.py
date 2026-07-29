"""
Resolve a location name into a TOPICAL_LOCATIONS entry.

Looks the name up via the TempHist API's location search, checks whether
it's already known as preapproved (tier1) or popular (tier2) — in which
case no TOPICAL_LOCATIONS entry is needed at all, or a bare id suffices —
and otherwise prompts for the one thing no API endpoint provides: the IANA
timezone. See README.md > "Adding a location" for the format this feeds.

Usage:
    python resolve_topical_location.py "Biarritz"
"""

import argparse
import os
import re
import sys
from zoneinfo import available_timezones

import httpx
from dotenv import load_dotenv

load_dotenv()


def slugify(name: str) -> str:
    """Same slug rule the TempHist API uses to derive a canonical location id
    from a name (routers/locations.py:_resolve_canonical_id in the api repo) —
    matching it means a manually-added topical location converges onto the
    same id if it later accumulates enough app selections to go popular."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def _client() -> httpx.Client:
    base_url = os.environ["TEMPHIST_API_URL"]
    api_key = os.environ.get("TEMPHIST_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.Client(base_url=base_url, headers=headers, timeout=30)


def search(client: httpx.Client, query: str) -> list:
    resp = client.get("/v1/locations/search", params={"q": query})
    resp.raise_for_status()
    return resp.json()["locations"]


def find_existing_tier(client: httpx.Client, loc_id: str) -> str | None:
    """Return 'tier1'/'tier2' if loc_id is already preapproved/popular, else None."""
    preapproved = client.get("/v1/locations/preapproved").json()["locations"]
    if any(loc["id"] == loc_id for loc in preapproved):
        return "tier1"
    popular = client.get("/v1/locations/popular").json()["locations"]
    if any(loc["id"] == loc_id for loc in popular):
        return "tier2"
    return None


def choose_result(results: list) -> dict:
    if len(results) == 1:
        return results[0]
    print(f"{len(results)} matches — pick one:")
    for i, r in enumerate(results):
        admin1 = f"{r['admin1']}, " if r.get("admin1") else ""
        print(f"  [{i}] {r['name']}, {admin1}{r['country_name']} ({r['country_code']})")
    idx = int(input("Number: "))
    return results[idx]


def prompt_timezone() -> str:
    zones = available_timezones()
    while True:
        tz = input("IANA timezone (e.g. Europe/Paris): ").strip()
        if tz in zones:
            return tz
        print(f"  {tz!r} is not a recognised IANA timezone name — try again.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a location name into a TOPICAL_LOCATIONS entry."
    )
    parser.add_argument("query", help="City name to search for, e.g. 'Biarritz'")
    args = parser.parse_args()

    with _client() as client:
        results = search(client, args.query)
        if not results:
            print(f"No matches for {args.query!r}.", file=sys.stderr)
            sys.exit(1)

        result = choose_result(results)
        name = result["name"]
        country = result["country_code"]
        loc_id = result.get("location_id") or slugify(name)

        tier = find_existing_tier(client, loc_id)

    if tier == "tier1":
        print(f"'{name}' is already tier1 (preapproved) — it posts unconditionally already.")
        print("No need to add it to TOPICAL_LOCATIONS.")
        return

    if tier == "tier2":
        print(f"'{name}' is already tier2 (popular), as id={loc_id!r}.")
        print(f"Just add the bare id:\n\n  TOPICAL_LOCATIONS={loc_id}")
        return

    print(f"'{name}' isn't known to the API yet — need a full tuple.")
    tz = prompt_timezone()
    entry = f"{loc_id}:{tz}:{country}:{name}"
    print(f"\nTOPICAL_LOCATIONS entry:\n\n  {entry}")


if __name__ == "__main__":
    main()
