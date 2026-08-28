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

    def __init__(self, url: str, headers: dict, body: dict | None = None) -> None:
        self.url = url
        self.headers = headers
        self._body = body if body is not None else {"total_matches_found": 0, "results": []}

    def json(self) -> dict:
        return self._body


class _FakeRequests:
    """Stand-in for curl_cffi.requests — records the call instead of sending it."""

    last: _FakeResponse | None = None
    next_body: dict | None = None

    @classmethod
    def get(cls, url: str, impersonate: str = "", headers: dict | None = None, **_):
        cls.last = _FakeResponse(url, headers or {}, cls.next_body)
        cls.next_body = None
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


def test_nodriver_parses_cookies_without_sameparty() -> None:
    """Current Chrome builds no longer send the removed "sameParty" field.

    nodriver < 0.50.3 required it, so reading the Akamai cookies blew up inside
    its background listener and cookie acquisition hung until it timed out.
    """
    from nodriver.cdp.network import Cookie

    cookie = Cookie.from_json({
        "name": "_abck", "value": "…", "domain": ".tesla.com", "path": "/",
        "size": 889, "httpOnly": False, "secure": True, "session": False,
        "priority": "Medium", "sourceScheme": "Secure", "sourcePort": 443,
        "expires": 1819449413.49, "sameSite": "None",
    })
    assert cookie.name == "_abck"


def test_new_inventory_buckets_are_flattened() -> None:
    """New inventory nests results in exact/approximate buckets, not a list.

    Before this was handled, searching condition="new" iterated the dict's keys
    and died with "'str' object has no attribute 'get'".
    """
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("NL"))
    _FakeRequests.next_body = {
        "total_matches_found": 3,
        "results": {
            "exact": [{"VIN": "A"}, {"VIN": "B"}],
            "approximate": [{"VIN": "C"}],
            "approximateOutside": [],
        },
    }
    data = client.fetch_page(model="my", condition="new")

    assert [v["VIN"] for v in data["results"]] == ["A", "B", "C"]
    assert data["total_matches_found"] == 3


def test_empty_new_inventory_is_not_an_error() -> None:
    client = scraper.InventoryClient({"_abck": "x"}, region=get_region("NL"))
    _FakeRequests.next_body = {"total_matches_found": 0, "results": {"exact": []}}

    total, vehicles = client.fetch_top_n(model="my", condition="new", n=30)
    assert (total, vehicles) == (0, [])


def test_all_presets_are_coherent() -> None:
    for name, region in REGIONS.items():
        assert region.name == name
        assert len(region.market) == 2
        assert region.distance_unit in ("km", "mi")
        assert region.api_url.endswith("/inventory/api/v4/inventory-results")
        assert region.inventory_url("new", "m3").endswith("/inventory/new/m3")


def test_chrome_start_is_retried() -> None:
    """A first launch that cannot connect is transient — retry before failing.

    Chrome refusing the debugging port right after a previous run shut down
    used to abort the whole check with "Failed to connect to browser".
    """
    import asyncio as _asyncio
    import types

    calls = {"start": 0}

    class _FakeCookie:
        def __init__(self, name):
            self.name, self.value = name, "x"

    class _FakePage:
        async def evaluate(self, _js):
            return "Nieuwe en gebruikte elektrische auto's | Tesla"

        async def send(self, _cmd):
            return [_FakeCookie("_abck"), _FakeCookie("bm_sz")]

    class _FakeBrowser:
        async def get(self, _url):
            return _FakePage()

        def stop(self):
            pass

    async def _start(**_kwargs):
        calls["start"] += 1
        if calls["start"] < 3:
            raise Exception("Failed to connect to browser")
        return _FakeBrowser()

    fake = types.ModuleType("nodriver")
    fake.start = _start
    fake.cdp = types.SimpleNamespace(
        network=types.SimpleNamespace(get_cookies=lambda: "get_cookies")
    )
    saved_module = sys.modules.get("nodriver")
    saved_sleep = _asyncio.sleep

    async def _no_sleep(_seconds):
        return None

    sys.modules["nodriver"] = fake
    _asyncio.sleep = _no_sleep
    try:
        cookies = _asyncio.run(scraper.CookieManager().acquire())
    finally:
        _asyncio.sleep = saved_sleep
        if saved_module is not None:
            sys.modules["nodriver"] = saved_module
        else:
            del sys.modules["nodriver"]

    assert calls["start"] == 3, "moet twee keer opnieuw proberen"
    assert cookies == {"_abck": "x", "bm_sz": "x"}


def test_watch_filter_picks_the_right_cars() -> None:
    from tesla_mcp.watch import Criteria, reject_reason

    criteria = Criteria()  # 2023 Model Y, tow hitch, not white
    cars = {
        "hit": {"VIN": "1", "Year": 2023, "PAINT": ["MIDNIGHTSILVER"], "TOWING": ["TW01"]},
        "wit": {"VIN": "2", "Year": 2023, "PAINT": ["PEARLWHITE"], "TOWING": ["TW01"]},
        "geen trekhaak": {"VIN": "3", "Year": 2023, "PAINT": ["DEEPBLUE"]},
        "te oud": {"VIN": "4", "Year": 2022, "PAINT": ["SOLIDBLACK"], "TOWING": ["TW01"]},
        "optiecode": {"VIN": "5", "Year": 2023, "PAINT": ["ULTRARED"],
                      "OptionCodeList": "MY23,TW01,PPMR"},
    }

    assert reject_reason(cars["hit"], criteria) is None
    assert reject_reason(cars["optiecode"], criteria) is None
    assert reject_reason(cars["wit"], criteria) == "verkeerde kleur"
    assert reject_reason(cars["geen trekhaak"], criteria) == "geen trekhaak"
    assert reject_reason(cars["te oud"], criteria) == "bouwjaar te oud"


def test_tow_detection_ignores_free_text() -> None:
    """Only option groups count — a city called Towcester is not a tow hitch."""
    from tesla_mcp.watch import has_tow_hitch

    assert not has_tow_hitch({"City": "Towcester", "TrimName": "Long Range"})
    assert has_tow_hitch({"TOWING": ["TW01"], "City": "Utrecht"})


def test_tow_detection_on_real_nl_records() -> None:
    """Shapes taken verbatim from live tesla.com/nl_NL Model Y records.

    Every car carries a SPECS_TOWING *specification* row with value "<nil>";
    only $TW01 / ADL_OPTS TOWING / an OptionCodeData group of TOWING mean a
    hitch is actually fitted. Matching "TOW" loosely passed every car.
    """
    from tesla_mcp.watch import has_tow_hitch

    specs_row = {"code": "$MTY13", "group": "SPECS_TOWING", "value": "<nil>"}

    fitted = {
        "VIN": "LRWYGCFS2PC617284",
        "ADL_OPTS": ["TOWING"],
        "OptionCodeList": "$APBS,$DV2W,$INPW0,$PPSW,$MTY13,$CPF0,$TW01",
        "OptionCodeData": [specs_row,
                           {"code": "$TW01", "group": "TOWING", "name": "Trekhaak"}],
    }
    not_fitted = {
        "VIN": "LRWYGCFS4PC674232",
        "ADL_OPTS": None,
        "OptionCodeList": "$APFS,$DV2W,$INPB0,$PMNG,$MTY13,$STY5S,$CPF0",
        "OptionCodeData": [specs_row],
    }

    assert has_tow_hitch(fitted)
    assert not has_tow_hitch(not_fitted), "SPECS_TOWING is geen trekhaak"

    # And the filter as a whole agrees.
    from tesla_mcp.watch import Criteria, reject_reason

    criteria = Criteria()
    fitted_2023 = {**fitted, "Year": 2023, "PAINT": ["BLUE"]}
    plain_2023 = {**not_fitted, "Year": 2023, "PAINT": ["GREY"]}
    assert reject_reason(fitted_2023, criteria) is None
    assert reject_reason(plain_2023, criteria) == "geen trekhaak"


def test_watch_price_and_odometer_limits() -> None:
    from tesla_mcp.watch import Criteria, reject_reason

    criteria = Criteria(require_tow=False, exclude_paint=(), max_price=35000,
                        odometer_max=50000)
    assert reject_reason({"Year": 2023, "TotalPrice": 39900}, criteria) == "te duur"
    assert reject_reason({"Year": 2023, "TotalPrice": 30000,
                          "Odometer": 61000}, criteria) == "te veel kilometers"
    assert reject_reason({"Year": 2023, "TotalPrice": 30000,
                          "Odometer": 40000}, criteria) is None


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
