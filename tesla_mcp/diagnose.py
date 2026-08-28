"""Diagnose a stalled or failing cookie acquisition.

    uv run python -m tesla_mcp.diagnose

Runs the same browser steps the scraper uses, one at a time, printing what it
finds — Chrome binary, page titles, cookie names — plus the checks that are
easy to miss: is Chrome already running, does this machine reach tesla.com at
all, is there a desktop session for a non-headless browser?
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys

from tesla_mcp.config import REGION, chrome_executable
from tesla_mcp.scraper import CookieManager


def _print(label: str, value: str) -> None:
    print(f"{label:<22}: {value}")


def check_environment() -> None:
    print("=== Omgeving ===")
    _print("Platform", f"{platform.system()} {platform.release()}")
    _print("Markt", f"{REGION.market} ({REGION.site_url})")

    chrome = chrome_executable()
    _print("TESLA_CHROME_PATH", chrome or "niet gezet (nodriver zoekt zelf)")
    if chrome and not os.path.exists(chrome):
        _print("LET OP", f"dit pad bestaat niet: {chrome}")

    if platform.system() == "Darwin":
        try:
            running = subprocess.run(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True, text=True, timeout=10,
            ).stdout.split()
            if running:
                _print("Chrome draait al", f"ja, {len(running)} proces(sen) — "
                                           "sluit Chrome af met Cmd-Q en probeer opnieuw")
            else:
                _print("Chrome draait al", "nee (goed)")
        except (OSError, subprocess.SubprocessError):
            pass
    elif not os.environ.get("DISPLAY"):
        _print("LET OP", "geen DISPLAY — de browser draait niet headless en "
                         "heeft een desktopsessie nodig")


def check_connectivity() -> None:
    print("\n=== Verbinding ===")
    try:
        from curl_cffi import requests as cf_requests

        resp = cf_requests.get(REGION.site_url, impersonate="chrome131", timeout=20)
        _print("tesla.com/nl_NL", f"HTTP {resp.status_code}")
        if resp.status_code == 403:
            print("  → Tesla blokkeert dit IP-adres al zonder browser "
                  "(VPN actief? probeer die uit te zetten)")
    except Exception as exc:  # noqa: BLE001 — diagnostics, report anything
        _print("tesla.com/nl_NL", f"onbereikbaar: {type(exc).__name__}: {exc}")


async def check_cookies() -> int:
    print("\n=== Cookies ophalen (Chrome opent, laat het venster staan) ===")
    try:
        cookies = await CookieManager().acquire()
    except Exception as exc:  # noqa: BLE001 — diagnostics, report anything
        print(f"\nMISLUKT: {type(exc).__name__}: {exc}")
        return 1

    print(f"\nGELUKT: {len(cookies)} cookies, _abck aanwezig: {'_abck' in cookies}")
    print("Namen:", ", ".join(sorted(cookies)[:15]) or "(geen)")
    if "_abck" not in cookies:
        print("  → zonder _abck weigert de API; probeer opnieuw of verhoog de "
              "wachttijd in tesla_mcp/scraper.py")
        return 1
    return 0


def main() -> int:
    check_environment()
    check_connectivity()
    return asyncio.run(check_cookies())


if __name__ == "__main__":
    raise SystemExit(main())
