"""Offline regression tests for the Dutch market configuration.

No network, no browser — the HTTP layer is stubbed, so these run anywhere:

    uv run python tests/test_offline.py

(They are plain asserts in functions, so `pytest tests/` works too.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tesla_mcp import scraper  # noqa: E402
from tesla_mcp.config import REGIONS, get_region  # noqa: E402


class _FakeResponse:
    status_code = 200

    def __init__(self, url: str, headers: dict) -> None:
        self.url = url
        self.headers = headers

    def json(self) -> dict:
        return {"total_matches_found": 0, "results": []}


class _FakeRequests:
    """Stand-in for curl_cffi.requests — records the call instead of sending it."""

    last: _FakeResponse | None = None

    @classmethod
    def get(cls, url: str, impersonate: str = "", headers: dict | None = None, **_):
        cls.last = _FakeResponse(url, headers or {})
        return cls.last


def _clear_env_overrides() -> None:
    """Ignore the developer's own .env — these tests assert the shipped defaults."""
    import os

    for key in [k for k in os.environ if k.startswith("TESLA_")]:
        del os.environ[key]


def _install_fake_curl_cffi() -> None:
    import types

    module = types.ModuleType("curl_cffi")
    module.requests = _FakeRequests
    sys.modules["curl_cffi"] = module


def _captured_query(client: scraper.InventoryClient, **kwargs) -> dict:
    client.fetch_page(model="my", condition="used", **kwargs)
    assert _FakeRequests.last is not None
    raw = parse_qs(urlparse(_FakeRequests.last.url).query)["query"][0]
    return json.loads(raw)["query"]


def test_nl_query_payload() -> None:
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("NL"))
    query = _captured_query(client)

    assert query["market"] == "NL"
    assert query["language"] == "nl"
    assert query["super_region"] == "europe"
    assert query["region"] == "NL"
    assert query["zip"] == "1012AB"
    assert query["range"] == 0
    # Amsterdam, not the upstream Georgia coordinates.
    assert round(query["lat"], 2) == 52.37
    assert round(query["lng"], 2) == 4.90


def test_nl_referer_and_language_header() -> None:
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("NL"))
    _captured_query(client)
    headers = _FakeRequests.last.headers

    assert headers["Referer"] == "https://www.tesla.com/nl_NL/inventory/used/my"
    assert headers["Accept-Language"].startswith("nl")


def test_explicit_location_overrides_default() -> None:
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("NL"))
    query = _captured_query(client, postal_code="3011AA", search_range=100)

    assert query["zip"] == "3011AA"
    assert query["range"] == 100


def test_us_preset_still_matches_upstream() -> None:
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("US"))
    query = _captured_query(client)

    assert query["market"] == "US"
    assert query["super_region"] == "north america"
    assert query["region"] == "GA"
    assert query["zip"] == "30096"
    assert _FakeRequests.last.headers["Referer"] == (
        "https://www.tesla.com/inventory/used/my"
    )


def test_unknown_region_falls_back_to_nl() -> None:
    assert get_region("does-not-exist").market == "NL"


def test_all_presets_are_coherent() -> None:
    for name, region in REGIONS.items():
        assert region.name == name
        assert len(region.market) == 2
        assert region.distance_unit in ("km", "mi")
        assert region.api_url.endswith("/inventory/api/v4/inventory-results")
        assert region.inventory_url("new", "m3").endswith("/inventory/new/m3")


def main() -> int:
    _clear_env_overrides()
    _install_fake_curl_cffi()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}", file=sys.stderr)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


_clear_env_overrides()
_install_fake_curl_cffi()

if __name__ == "__main__":
    raise SystemExit(main())
