"""Watch the inventory for cars matching a saved filter and alert on new hits.

Default filter: a 2023 Model Y occasion with a tow hitch, in any colour but
white. Run it once by hand, or every few hours from launchd:

    uv run python -m tesla_mcp.watch              # check now, notify on new hits
    uv run python -m tesla_mcp.watch --explain    # show the raw option fields
    uv run python -m tesla_mcp.watch --from-file results/raw.json --dry-run

Every match is appended to results/matches.csv. VINs already reported are kept
in results/watch_state.json, so a car is announced once, not every three hours.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from tesla_mcp.config import REGION
from tesla_mcp.scraper import CookieManager, InventoryClient

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
STATE_FILE = RESULTS_DIR / "watch_state.json"
MATCHES_CSV = RESULTS_DIR / "matches.csv"

# Option groups Tesla returns in upper case (PAINT, WHEELS, TOWING, ...). The
# tow hitch shows up in one of these, or in the raw option-code list.
_OPTION_CODE_KEYS = ("OptionCodeList", "OptionCodeSpecs", "OptionCodeData")
_TOW_MARKERS = ("TOW", "TRAILER", "HITCH", "TW01", "TW02")


def _env(key: str, fallback: str = "") -> str:
    return os.getenv(key, fallback).strip()


def _env_int(key: str, fallback: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


# ── Filter ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Criteria:
    """What counts as a hit. Every field is overridable from .env."""

    model: str = "my"
    condition: str = "used"
    year_min: int = 2023
    year_max: int = 2023
    require_tow: bool = True
    exclude_paint: tuple[str, ...] = ("WHITE",)
    max_price: int = 0        # 0 = no ceiling
    odometer_max: int = 0     # 0 = no limit, in km
    top_n: int = 200

    @classmethod
    def from_env(cls) -> Criteria:
        exclude = _env("WATCH_EXCLUDE_PAINT", "WHITE")
        return cls(
            model=_env("WATCH_MODEL", "my"),
            condition=_env("WATCH_CONDITION", "used"),
            year_min=_env_int("WATCH_YEAR_MIN", 2023),
            year_max=_env_int("WATCH_YEAR_MAX", 2023),
            require_tow=_env("WATCH_REQUIRE_TOW", "1") not in ("0", "false", "no"),
            exclude_paint=tuple(p.strip().upper() for p in exclude.split(",") if p.strip()),
            max_price=_env_int("WATCH_MAX_PRICE", 0),
            odometer_max=_env_int("WATCH_ODOMETER_MAX", 0),
            top_n=_env_int("WATCH_TOP_N", 200),
        )

    def describe(self) -> str:
        bits = [f"{self.model.upper()} {self.condition}"]
        if self.year_min or self.year_max:
            bits.append(f"{self.year_min or '…'}-{self.year_max or '…'}")
        if self.require_tow:
            bits.append("met trekhaak")
        if self.exclude_paint:
            bits.append("niet " + "/".join(p.lower() for p in self.exclude_paint))
        if self.max_price:
            bits.append(f"≤ €{self.max_price:,}".replace(",", "."))
        if self.odometer_max:
            bits.append(f"≤ {self.odometer_max} km")
        return ", ".join(bits)


def _as_strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_as_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_as_strings(item))
        return out
    return [str(value)]


def paint_values(vehicle: dict) -> list[str]:
    return [p.upper() for p in _as_strings(vehicle.get("PAINT"))]


def option_blob(vehicle: dict) -> str:
    """All option-ish values as one upper-case string.

    Only option groups (Tesla returns those with upper-case keys) and the raw
    option-code fields — never free text like City, which could contain "tow"
    by accident.
    """
    parts: list[str] = []
    for key, value in vehicle.items():
        if key.isupper() or key in _OPTION_CODE_KEYS:
            parts.extend(_as_strings(value))
    return " ".join(parts).upper()


def has_tow_hitch(vehicle: dict) -> bool:
    return any(marker in option_blob(vehicle) for marker in _TOW_MARKERS)


def reject_reason(vehicle: dict, criteria: Criteria) -> str | None:
    """None when the car is a hit; otherwise why it was skipped."""
    year = vehicle.get("Year") or 0
    if criteria.year_min and year < criteria.year_min:
        return "bouwjaar te oud"
    if criteria.year_max and year > criteria.year_max:
        return "bouwjaar te nieuw"

    paints = paint_values(vehicle)
    for unwanted in criteria.exclude_paint:
        if any(unwanted in paint for paint in paints):
            return "verkeerde kleur"

    if criteria.require_tow and not has_tow_hitch(vehicle):
        return "geen trekhaak"

    price = vehicle.get("TotalPrice") or vehicle.get("Price") or 0
    if criteria.max_price and price and price > criteria.max_price:
        return "te duur"

    odometer = vehicle.get("Odometer") or 0
    if criteria.odometer_max and odometer > criteria.odometer_max:
        return "te veel kilometers"

    return None


# ── Presentation ──────────────────────────────────────────────────────


def listing_url(vehicle: dict, criteria: Criteria) -> str:
    """Best-effort deep link to the listing on the localised site."""
    vin = vehicle.get("VIN", "")
    model = (vehicle.get("Model") or criteria.model or "my").lower()
    prefix = f"/{REGION.locale}" if REGION.locale else ""
    return f"https://www.tesla.com{prefix}/{model}/order/{vin}"


def summarize(vehicle: dict, criteria: Criteria) -> str:
    price = vehicle.get("TotalPrice") or vehicle.get("Price") or 0
    odo = vehicle.get("Odometer") or 0
    paint = ", ".join(paint_values(vehicle)) or "onbekende kleur"
    city = vehicle.get("City") or vehicle.get("MetroName") or "?"
    price_text = f"€{price:,.0f}".replace(",", ".") if price else "prijs onbekend"
    odo_text = f"{odo:,.0f}".replace(",", ".")
    trim = vehicle.get("TrimName") or criteria.model.upper()
    return (f"{vehicle.get('Year', '?')} {trim} — {price_text}, "
            f"{odo_text} km, {paint}, {city}")


# ── Notifications ─────────────────────────────────────────────────────


def notify_macos(title: str, body: str) -> bool:
    if platform.system() != "Darwin":
        return False
    script = (
        f'display notification {json.dumps(body)} '
        f'with title {json.dumps(title)} sound name "Glass"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=15,
                       capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def notify_ntfy(topic: str, title: str, body: str, click: str = "") -> bool:
    """Push to a phone via ntfy.sh.

    The topic name is the only secret: anyone who knows it can read these
    messages, so use something unguessable.
    """
    if not topic:
        return False
    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    request.add_header("Title", title.encode("utf-8").decode("latin-1", "replace"))
    request.add_header("Tags", "car")
    request.add_header("Priority", "default")
    if click:
        request.add_header("Click", click)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"ntfy mislukt: {exc}", file=sys.stderr)
        return False


def announce(vehicles: list[dict], criteria: Criteria, dry_run: bool = False) -> None:
    topic = _env("NTFY_TOPIC")
    count = len(vehicles)
    title = f"{count} nieuwe Tesla{'s' if count != 1 else ''} gevonden"
    lines = [summarize(v, criteria) for v in vehicles]
    body = "\n".join(lines[:5])
    if count > 5:
        body += f"\n… en {count - 5} meer"

    if dry_run:
        print(f"[dry-run] melding: {title}\n{body}")
        return

    if not notify_macos(title, body):
        print("(geen macOS-melding verstuurd)", file=sys.stderr)
    if topic:
        click = listing_url(vehicles[0], criteria) if count == 1 else ""
        if notify_ntfy(topic, title, body, click):
            print(f"Push verstuurd naar ntfy topic {topic!r}")
    else:
        print("NTFY_TOPIC niet gezet — geen push naar je telefoon", file=sys.stderr)


# ── State ─────────────────────────────────────────────────────────────


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": {}}
    try:
        state = json.loads(STATE_FILE.read_text())
        state.setdefault("seen", {})
        return state
    except (OSError, json.JSONDecodeError):
        return {"seen": {}}


def save_state(state: dict) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_matches(vehicles: list[dict], criteria: Criteria) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    fields = ["found_at", "VIN", "Year", "TrimName", "TotalPrice", "Odometer",
              "PAINT", "City", "url"]
    exists = MATCHES_CSV.exists()
    with MATCHES_CSV.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for v in vehicles:
            writer.writerow({
                "found_at": time.strftime("%Y-%m-%d %H:%M"),
                "VIN": v.get("VIN", ""),
                "Year": v.get("Year", ""),
                "TrimName": v.get("TrimName", ""),
                "TotalPrice": v.get("TotalPrice") or v.get("Price") or "",
                "Odometer": v.get("Odometer", ""),
                "PAINT": ", ".join(paint_values(v)),
                "City": v.get("City", ""),
                "url": listing_url(v, criteria),
            })


# ── Run ───────────────────────────────────────────────────────────────


async def fetch_vehicles(criteria: Criteria) -> list[dict]:
    cookies = await CookieManager().acquire(
        model=criteria.model, condition=criteria.condition
    )
    client = InventoryClient(cookies)
    _, vehicles = client.fetch_top_n(
        model=criteria.model,
        condition=criteria.condition,
        n=criteria.top_n,
        year_min=criteria.year_min,
        year_max=criteria.year_max,
    )
    return vehicles


def explain(vehicles: list[dict], limit: int = 3) -> None:
    print("\n=== Optievelden van de eerste auto's (om het trekhaakfilter te ijken) ===")
    for v in vehicles[:limit]:
        print(f"\nVIN {v.get('VIN')} — {v.get('TrimName')}")
        for key, value in sorted(v.items()):
            if key.isupper() or key in _OPTION_CODE_KEYS:
                print(f"  {key} = {value!r}")
        print(f"  trekhaak gedetecteerd: {has_tow_hitch(v)}")


def run(vehicles: list[dict], criteria: Criteria, dry_run: bool) -> int:
    reasons: dict[str, int] = {}
    matches: list[dict] = []
    for v in vehicles:
        reason = reject_reason(v, criteria)
        if reason is None:
            matches.append(v)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    print(f"Filter    : {criteria.describe()}")
    print(f"Bekeken   : {len(vehicles)} auto's")
    print(f"Match     : {len(matches)}")
    if reasons:
        print("Afgevallen: " + ", ".join(f"{n}x {r}" for r, n in sorted(reasons.items())))

    state = load_state()
    seen = state["seen"]
    fresh = [v for v in matches if v.get("VIN") and v["VIN"] not in seen]

    for v in matches:
        vin = v.get("VIN")
        if vin:
            seen[vin] = {
                "last_seen": time.strftime("%Y-%m-%d %H:%M"),
                "price": v.get("TotalPrice") or v.get("Price"),
            }

    if not fresh:
        print("Nieuw     : geen (alles al eerder gemeld)")
        if not dry_run:
            save_state(state)
        return 0

    print(f"Nieuw     : {len(fresh)}")
    for v in fresh:
        print(f"  • {summarize(v, criteria)}")
        print(f"    {listing_url(v, criteria)}")

    if not dry_run:
        record_matches(fresh, criteria)
        save_state(state)
    announce(fresh, criteria, dry_run=dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="niets opslaan, geen melding versturen")
    parser.add_argument("--explain", action="store_true",
                        help="toon de optievelden van de eerste auto's")
    parser.add_argument("--from-file", metavar="PATH",
                        help="lees voertuigen uit een eerder opgeslagen JSON-bestand")
    args = parser.parse_args(argv)

    criteria = Criteria.from_env()

    if args.from_file:
        data = json.loads(Path(args.from_file).read_text())
        vehicles = data.get("results", data) if isinstance(data, dict) else data
    else:
        try:
            vehicles = asyncio.run(fetch_vehicles(criteria))
        except Exception as exc:
            # Runs unattended from launchd — one clear line beats a traceback.
            print(f"Ophalen mislukt: {exc}", file=sys.stderr)
            return 1

    if args.explain:
        explain(vehicles)

    return run(vehicles, criteria, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
