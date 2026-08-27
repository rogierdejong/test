"""Offline sanity check of the active market configuration.

Run it after installing or after changing .env:

    uv run python -m tesla_mcp.selfcheck

It builds the exact API request the scraper would send — without touching the
network — and asserts the market settings are internally consistent. Handy to
confirm the Dutch setup before firing up Chrome.
"""

from __future__ import annotations

import json
import sys

from tesla_mcp.config import REGION, REGIONS, default_radius
from tesla_mcp.scraper import InventoryClient


def build_sample_url(model: str = "my", condition: str = "used") -> str:
    """Reproduce the URL fetch_page() would request, using dummy cookies."""
    client = InventoryClient({"_abck": "dummy"}, region=REGION)
    query = {
        "query": {
            "model": model,
            "condition": condition,
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            **REGION.query_location(radius=default_radius()),
        },
        "offset": 0,
        "count": 50,
        "outsideOffset": 0,
        "outsideSearch": False,
    }
    return f"{client.api_url}?query={json.dumps(query)}"


def main() -> int:
    print(f"Region preset      : {REGION.name} (available: {', '.join(REGIONS)})")
    print(f"Market / language  : {REGION.market} / {REGION.language}")
    print(f"Super region       : {REGION.super_region}")
    print(f"Currency / unit    : {REGION.currency} / {REGION.distance_unit}")
    print(f"Site               : {REGION.site_url}")
    print(f"Inventory page     : {REGION.inventory_url('used', 'my')}")
    print(f"API endpoint       : {REGION.api_url}")
    print(f"Default location   : {REGION.postal_code} (radius {default_radius()} "
          f"{REGION.distance_unit}, 0 = whole market)")
    print()
    print("Sample request:")
    print(build_sample_url())

    problems: list[str] = []
    location = REGION.query_location()
    if not REGION.market or len(REGION.market) != 2:
        problems.append(f"market {REGION.market!r} is not a 2-letter country code")
    if REGION.super_region == "europe" and REGION.distance_unit != "km":
        problems.append("European markets report distances in km")
    if REGION.locale and not REGION.inventory_url("used", "my").startswith(
        f"{REGION.site_url}/inventory"
    ):
        problems.append("inventory URL does not match the localised site URL")
    if location.get("zip") != REGION.postal_code:
        problems.append("postal code is not reaching the query payload")

    print()
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1
    print("OK — configuration is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
