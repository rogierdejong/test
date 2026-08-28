"""Tesla inventory scraper — nodriver cookies + curl_cffi API client."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from tesla_mcp.config import REGION, RegionConfig, chrome_executable, default_radius

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

# Chrome occasionally refuses the first connection; retry before giving up.
_BROWSER_START_ATTEMPTS = 3
_BROWSER_RETRY_DELAY = 4.0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _step(label: str, coro, timeout: float):
    """Await one browser step, logging it and failing loudly if it stalls.

    Without this a wedged Chrome (an already-running instance, a profile
    picker, a page that never finishes loading) just hangs forever with no
    indication of which step is stuck.
    """
    _log(f"  -> {label}...")
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Chrome stalled while {label} (no response within {timeout:.0f}s). "
            "Quit Chrome completely (Cmd-Q on macOS) and try again; if it keeps "
            "stalling, run: uv run python -m tesla_mcp.diagnose"
        ) from None


# Buckets Tesla uses for new-inventory results, in the order it shows them.
_RESULT_BUCKETS = ("exact", "approximate", "approximateOutside")


def _normalize_response(data: dict) -> dict:
    """Make "results" a flat list, whatever shape the API used.

    Used inventory returns a plain list. New inventory returns a dict of
    buckets instead — exact / approximate / approximateOutside — each holding
    its own list. Callers iterating the raw value then walked over dict *keys*
    (strings) and blew up on the first v.get(...).
    """
    results = data.get("results")

    if isinstance(results, list):
        return data
    if results is None:
        return {**data, "results": []}
    if not isinstance(results, dict):
        return {**data, "results": []}

    flat: list[dict] = []
    seen_buckets: set[str] = set()
    for bucket in _RESULT_BUCKETS:
        items = results.get(bucket)
        seen_buckets.add(bucket)
        if isinstance(items, list):
            flat.extend(items)
    # Tolerate bucket names Tesla adds later.
    for bucket, items in results.items():
        if bucket not in seen_buckets and isinstance(items, list):
            flat.extend(items)

    return {**data, "results": flat}


# ── Cookie acquisition via nodriver ──────────────────────────────────


class CookieManager:
    """Acquire and cache Akamai cookies using nodriver (undetected Chrome)."""

    def __init__(self, ttl: int = 600, region: RegionConfig = REGION) -> None:
        self._cookies: dict[str, str] = {}
        self._acquired_at: float = 0
        self._ttl = ttl  # seconds
        self._region = region

    @property
    def valid(self) -> bool:
        return bool(self._cookies) and (time.time() - self._acquired_at < self._ttl)

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    async def acquire(self, model: str = "my", condition: str = "used") -> dict[str, str]:
        """Launch Chrome, visit Tesla inventory, extract Akamai cookies.

        Visits the localised site (e.g. tesla.com/nl_NL) so Akamai hands out
        cookies for the same market the API calls will use.

        Returns cached cookies if still valid (within TTL).
        """
        if self.valid:
            _log(f"Reusing cached cookies ({len(self._cookies)} cookies, "
                 f"{int(self._ttl - (time.time() - self._acquired_at))}s remaining)")
            return self.cookies

        import nodriver as uc

        # nodriver auto-detects Chrome; TESLA_CHROME_PATH overrides that when
        # the binary lives somewhere unusual.
        chrome_path = chrome_executable()
        _log(f"Launching Chrome to acquire Akamai cookies{f' ({chrome_path})' if chrome_path else ''}...")

        # A first launch that cannot connect is usually transient: a previous
        # Chrome is still shutting down and holding the debugging port.
        browser = None
        for attempt in range(1, _BROWSER_START_ATTEMPTS + 1):
            try:
                browser = await _step(
                    f"starting Chrome (poging {attempt}/{_BROWSER_START_ATTEMPTS})",
                    uc.start(
                        headless=False,
                        browser_args=["--no-first-run", "--no-default-browser-check"],
                        **({"browser_executable_path": chrome_path} if chrome_path else {}),
                    ),
                    timeout=60,
                )
                break
            except Exception as exc:
                if attempt == _BROWSER_START_ATTEMPTS:
                    raise RuntimeError(
                        f"Chrome kon niet gestart worden na {_BROWSER_START_ATTEMPTS} "
                        f"pogingen ({exc}). Sluit Chrome volledig af (Cmd-Q op macOS) "
                        "en probeer opnieuw; werkt het dan nog niet, zet "
                        "TESLA_CHROME_PATH in .env naar je Chrome-binary."
                    ) from exc
                _log(f"  Chrome start mislukt ({exc}); opnieuw over "
                     f"{_BROWSER_RETRY_DELAY:.0f}s...")
                await asyncio.sleep(_BROWSER_RETRY_DELAY)

        try:
            # Warm up on the localised homepage — lets Akamai JS set initial cookies
            page = await _step(
                f"loading {self._region.site_url}",
                browser.get(self._region.site_url),
                timeout=60,
            )
            await asyncio.sleep(5)

            # Navigate to inventory page — triggers full Akamai challenge
            inventory_url = self._region.inventory_url(condition, model)
            page = await _step(
                f"loading {inventory_url}",
                browser.get(inventory_url),
                timeout=60,
            )
            _log("  waiting 10s for the Akamai challenge to complete...")
            await asyncio.sleep(10)

            # Verify the page loaded (not Access Denied / Toegang geweigerd)
            title = await _step(
                "reading the page title",
                page.evaluate("document.title"),
                timeout=30,
            )
            _log(f"  page title: {title!r}")
            if title and ("Access Denied" in title or "Toegang geweigerd" in title):
                raise RuntimeError("Akamai still blocking — try increasing sleep time")

            # Extract cookies via CDP
            cdp_cookies = await _step(
                "reading cookies",
                page.send(uc.cdp.network.get_cookies()),
                timeout=30,
            )
            self._cookies = {c.name: c.value for c in cdp_cookies}
            self._acquired_at = time.time()

            _log(f"Got {len(self._cookies)} cookies (_abck={'_abck' in self._cookies})")
            return self.cookies

        finally:
            browser.stop()

    def invalidate(self) -> None:
        """Force re-acquisition on next call."""
        self._cookies.clear()
        self._acquired_at = 0


# ── API client via curl_cffi ─────────────────────────────────────────


class InventoryClient:
    """Fetch Tesla inventory via the v4 API using curl_cffi."""

    def __init__(self, cookies: dict[str, str], region: RegionConfig = REGION) -> None:
        self._cookies = cookies
        self._region = region

    @property
    def api_url(self) -> str:
        return self._region.api_url

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def fetch_page(
        self,
        model: str,
        condition: str,
        postal_code: str | None = None,
        search_range: int | None = None,
        offset: int = 0,
        count: int = 50,
        arrangeby: str = "Price",
        order: str = "asc",
        options: dict | None = None,
    ) -> dict:
        """Fetch one page of inventory results."""
        from curl_cffi import requests as cf_requests

        if search_range is None:
            search_range = default_radius()

        query = {
            "query": {
                "model": model,
                "condition": condition,
                "options": options or {},
                "arrangeby": arrangeby,
                "order": order,
                **self._region.query_location(postal_code, search_range),
            },
            "offset": offset,
            "count": count,
            "outsideOffset": 0,
            "outsideSearch": False,
        }

        url = f"{self.api_url}?query={json.dumps(query)}"

        resp = cf_requests.get(
            url,
            impersonate="chrome131",
            headers={
                "Cookie": self._cookie_header(),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": f"{self._region.language},en;q=0.8",
                "Referer": self._region.inventory_url(condition, model),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        )

        if resp.status_code != 200:
            raise RuntimeError(f"API returned {resp.status_code}: {resp.text[:300]}")
        return _normalize_response(resp.json())

    def fetch_all(
        self,
        model: str,
        condition: str,
        postal_code: str | None = None,
        search_range: int | None = None,
        max_results: int = 5000,
        delay: float = 1.5,
        arrangeby: str = "Price",
        order: str = "asc",
    ) -> tuple[int, list[dict]]:
        """Paginate through all results. Returns (total, results_list)."""
        first = self.fetch_page(
            model, condition, postal_code, search_range,
            arrangeby=arrangeby, order=order,
        )
        total = first.get("total_matches_found", 0)
        all_results = first.get("results", [])
        _log(f"[{model}] total={total}, first batch={len(all_results)}")

        offset = len(all_results)
        while offset < total and offset < max_results:
            time.sleep(delay)
            data = self.fetch_page(
                model, condition, postal_code, search_range, offset=offset,
                arrangeby=arrangeby, order=order,
            )
            batch = data.get("results", [])
            if not batch:
                break
            all_results.extend(batch)
            offset += len(batch)
            _log(f"[{model}] fetched {len(batch)} more → {len(all_results)}/{total}")

        return total, all_results

    def fetch_top_n(
        self,
        model: str,
        condition: str,
        n: int = 30,
        postal_code: str | None = None,
        search_range: int | None = None,
        arrangeby: str = "Price",
        order: str = "asc",
        options: dict | None = None,
        year_min: int = 0,
        year_max: int = 0,
        odometer_max: int = 0,
        delay: float = 1.5,
        max_pages: int = 10,
    ) -> tuple[int, list[dict]]:
        """Paginate and deduplicate until we have N unique vehicles.

        `odometer_max` is in the market's distance unit (km in the EU).

        Returns (total_matches, unique_vehicles[:n]).
        On HTTP error, returns whatever was collected so far (graceful degradation).
        """
        seen: dict[str, dict] = {}  # VIN → vehicle (insertion order = sort order)
        total = 0
        offset = 0

        for page_num in range(max_pages):
            if page_num > 0:
                time.sleep(delay)

            try:
                data = self.fetch_page(
                    model=model,
                    condition=condition,
                    postal_code=postal_code,
                    search_range=search_range,
                    offset=offset,
                    arrangeby=arrangeby,
                    order=order,
                    options=options,
                )
            except Exception as exc:
                _log(f"[{model}] fetch_top_n page {page_num} error: {exc} — returning {len(seen)} collected")
                break

            if page_num == 0:
                total = data.get("total_matches_found", 0)

            results = data.get("results", [])
            if not results:
                _log(f"[{model}] page {page_num} empty — done")
                break

            for v in results:
                vin = v.get("VIN")
                if not vin or vin in seen:
                    continue
                # Client-side filters
                if year_min and v.get("Year", 0) < year_min:
                    continue
                if year_max and v.get("Year", 9999) > year_max:
                    continue
                if odometer_max and v.get("Odometer", 999999) > odometer_max:
                    continue
                seen[vin] = v

            _log(f"[{model}] page {page_num}: +{len(results)} raw → {len(seen)} unique so far")

            if len(seen) >= n:
                break

            offset += len(results)
            if offset >= total:
                break

        return total, list(seen.values())[:n]


# Module-level singleton
cookie_manager = CookieManager()
