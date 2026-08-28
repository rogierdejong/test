"""Region / market configuration for the Tesla inventory scraper.

Upstream this project was hardcoded for the US market (market=US, language=en,
super_region="north america", ZIP codes, miles, USD). This module lifts those
constants into named region presets so the same code can scrape the Dutch
site (tesla.com/nl_NL) — or any other European market.

Defaults to the Netherlands. Everything is overridable through environment
variables (a `.env` file in the project root is loaded automatically), so no
code changes are needed to switch markets:

    TESLA_REGION=NL              # preset name: NL, BE, DE, FR, US
    TESLA_POSTAL_CODE=1012AB     # default search location
    TESLA_RADIUS=0               # 0 = whole country
    TESLA_LAT / TESLA_LNG        # coordinates used by the API for distance
    TESLA_MARKET / TESLA_LANGUAGE / TESLA_SUPER_REGION / TESLA_API_REGION
    TESLA_LOCALE                 # URL path segment, e.g. nl_NL
    TESLA_API_LOCALE_PREFIX      # set to nl_NL to call the locale-prefixed API
    TESLA_CHROME_PATH            # Chrome binary, if nodriver cannot find it
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://www.tesla.com"


@dataclass(frozen=True)
class RegionConfig:
    """Everything that differs between Tesla's national inventory sites."""

    name: str               # preset key, e.g. "NL"
    market: str             # API "market" — country code
    language: str           # API "language"
    super_region: str       # API "super_region"
    api_region: str         # API "region" — US state code, country code in EU
    locale: str             # URL path segment, e.g. "nl_NL" ("" for US)
    postal_code: str        # default search location
    lat: float
    lng: float
    currency: str           # informational: currency of TotalPrice
    distance_unit: str      # "km" or "mi" — unit of Odometer / range
    api_locale_prefix: str = ""  # "" = market-neutral API endpoint

    # ── Derived URLs ──────────────────────────────────────────────

    @property
    def site_url(self) -> str:
        """Homepage for this market — first stop for cookie acquisition."""
        return f"{BASE_URL}/{self.locale}" if self.locale else BASE_URL

    def inventory_url(self, condition: str, model: str) -> str:
        """Public inventory page, e.g. .../nl_NL/inventory/new/my."""
        prefix = f"/{self.locale}" if self.locale else ""
        return f"{BASE_URL}{prefix}/inventory/{condition}/{model}"

    @property
    def api_url(self) -> str:
        """v4 inventory API endpoint."""
        prefix = f"/{self.api_locale_prefix}" if self.api_locale_prefix else ""
        return f"{BASE_URL}{prefix}/inventory/api/v4/inventory-results"

    # ── Query fragment ────────────────────────────────────────────

    def query_location(self, postal_code: str | None = None, radius: int = 0) -> dict:
        """Market + location part of the API query payload."""
        payload = {
            "market": self.market,
            "language": self.language,
            "super_region": self.super_region,
            "lng": self.lng,
            "lat": self.lat,
            "zip": postal_code or self.postal_code,
            "range": radius,
        }
        if self.api_region:
            payload["region"] = self.api_region
        return payload


# ── Presets ───────────────────────────────────────────────────────────
#
# lat/lng are the geographic anchor the API sorts distance against:
# Amsterdam for NL, Brussels for BE, Berlin for DE, Paris for FR.

REGIONS: dict[str, RegionConfig] = {
    "NL": RegionConfig(
        name="NL", market="NL", language="nl", super_region="europe",
        api_region="NL", locale="nl_NL", postal_code="1012AB",
        lat=52.3676, lng=4.9041, currency="EUR", distance_unit="km",
    ),
    "BE": RegionConfig(
        name="BE", market="BE", language="nl", super_region="europe",
        api_region="BE", locale="nl_BE", postal_code="1000",
        lat=50.8476, lng=4.3572, currency="EUR", distance_unit="km",
    ),
    "DE": RegionConfig(
        name="DE", market="DE", language="de", super_region="europe",
        api_region="DE", locale="de_DE", postal_code="10115",
        lat=52.5200, lng=13.4050, currency="EUR", distance_unit="km",
    ),
    "FR": RegionConfig(
        name="FR", market="FR", language="fr", super_region="europe",
        api_region="FR", locale="fr_FR", postal_code="75001",
        lat=48.8566, lng=2.3522, currency="EUR", distance_unit="km",
    ),
    # Upstream default, kept so US scraping still works.
    "US": RegionConfig(
        name="US", market="US", language="en", super_region="north america",
        api_region="GA", locale="", postal_code="30096",
        lat=33.9837, lng=-84.1487, currency="USD", distance_unit="mi",
    ),
}

DEFAULT_REGION = "NL"


def _env_float(key: str, fallback: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _env_int(key: str, fallback: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def get_region(name: str | None = None) -> RegionConfig:
    """Return the active region preset with environment overrides applied.

    Unknown names fall back to the default region (NL) rather than raising —
    a typo in .env should not take the MCP server down.
    """
    key = (name or os.getenv("TESLA_REGION") or DEFAULT_REGION).strip().upper()
    base = REGIONS.get(key, REGIONS[DEFAULT_REGION])

    return replace(
        base,
        market=os.getenv("TESLA_MARKET", base.market),
        language=os.getenv("TESLA_LANGUAGE", base.language),
        super_region=os.getenv("TESLA_SUPER_REGION", base.super_region),
        api_region=os.getenv("TESLA_API_REGION", base.api_region),
        locale=os.getenv("TESLA_LOCALE", base.locale),
        postal_code=os.getenv("TESLA_POSTAL_CODE", base.postal_code),
        lat=_env_float("TESLA_LAT", base.lat),
        lng=_env_float("TESLA_LNG", base.lng),
        api_locale_prefix=os.getenv("TESLA_API_LOCALE_PREFIX", base.api_locale_prefix),
    )


def chrome_executable() -> str | None:
    """Explicit Chrome binary path, or None to let nodriver auto-detect."""
    path = os.getenv("TESLA_CHROME_PATH", "").strip()
    return path or None


def default_radius() -> int:
    """Default search radius in km (mi for US). 0 = entire market."""
    return _env_int("TESLA_RADIUS", 0)


# Active configuration, resolved once at import time.
REGION = get_region()
