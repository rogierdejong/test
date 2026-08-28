"""Watch the inventory: report everything new, and what meets your criteria.

Two layers. The *scope* is what gets tracked — by default every Model Y
occasion in the market. Anything appearing there is announced, whatever its
specs. The *criteria* are your own requirements (by default a 2023 Model Y
with a tow hitch, in any colour but white); every alert carries the current
list of cars that meet them.

Run it once by hand, or every few hours from launchd:

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
OVERVIEW_FILE = RESULTS_DIR / "overzicht.txt"

# Bumped when the meaning of the state file changes: v1 only tracked cars that
# met the criteria, v2 tracks the whole scope so anything new can be reported.
_STATE_VERSION = 2

# Option groups Tesla returns in upper case (PAINT, WHEELS, ADL_OPTS, ...),
# plus the raw option-code fields.
_OPTION_CODE_KEYS = ("OptionCodeList", "OptionCodeSpecs", "OptionCodeData")

# Tesla's option code for the tow hitch ("Trekhaak", group TOWING).
_TOW_OPTION_CODES = {"$TW01", "$TW02", "TW01", "TW02"}


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


def has_tow_hitch(vehicle: dict) -> bool:
    """True only when a tow hitch is actually fitted.

    Three signals, all taken from live NL data:

      * OptionCodeList contains $TW01 — the fitted-options list, authoritative
      * ADL_OPTS holds "TOWING"
      * OptionCodeData has an entry in group TOWING (code $TW01, "Trekhaak")

    Deliberately exact: every Model Y also carries a *specification* row with
    group SPECS_TOWING and value "<nil>". Matching the word "TOW" anywhere
    therefore marks every car as having a hitch.
    """
    codes = {c.strip().upper() for c in _as_strings(vehicle.get("OptionCodeList"))
             for c in c.split(",")}
    if codes & _TOW_OPTION_CODES:
        return True

    if any(value.upper() == "TOWING" for value in _as_strings(vehicle.get("ADL_OPTS"))):
        return True

    if _as_strings(vehicle.get("TOWING")):
        return True

    for entry in vehicle.get("OptionCodeData") or []:
        if isinstance(entry, dict) and str(entry.get("group", "")).upper() == "TOWING":
            return True

    return False


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


def _applescript_string(text: str) -> str:
    """Quote a string for AppleScript.

    Not json.dumps: it escapes non-ASCII as backslash-u sequences, which
    AppleScript does not understand — an em dash in the message aborted the
    whole script with "syntax error: unknown token". AppleScript takes UTF-8
    as-is and only needs backslashes and double quotes escaped.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify_macos(title: str, body: str) -> bool:
    if platform.system() != "Darwin":
        return False
    # Notifications carry a single line; osascript renders newlines poorly.
    one_line = body.replace("\n", " · ")
    script = (
        f"display notification {_applescript_string(one_line)} "
        f"with title {_applescript_string(title)}"
    )
    try:
        result = subprocess.run(["osascript", "-e", script], timeout=15,
                                capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"macOS-melding mislukt: {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exitcode {result.returncode}"
        print(f"macOS-melding mislukt: {detail}", file=sys.stderr)
        print("  → geef je terminal toestemming onder Systeeminstellingen › "
              "Berichtgeving", file=sys.stderr)
        return False
    return True


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


def _lines(vehicles: list[dict], criteria: Criteria, limit: int = 5) -> str:
    shown = [f"• {summarize(v, criteria)}" for v in vehicles[:limit]]
    if len(vehicles) > limit:
        shown.append(f"… en {len(vehicles) - limit} meer")
    return "\n".join(shown)


def compose(new: list[dict], matches: list[dict], criteria: Criteria,
            total: int, first_run: bool) -> tuple[str, str]:
    """Title and body for one alert."""
    if first_run:
        title = f"Wachter gestart — {total} auto's in beeld"
        opening = (f"{total} auto's in de voorraad. Vanaf nu krijg je bericht "
                   "zodra er eentje bijkomt.")
    else:
        title = f"{len(new)} nieuw in de voorraad"
        if matches:
            title += f" — {len(matches)} voldoet aan je eisen" if len(matches) == 1 \
                else f" — {len(matches)} voldoen aan je eisen"
        opening = _lines(new, criteria)

    if matches:
        body = (f"{opening}\n\n"
                f"Voldoet aan je eisen ({criteria.describe()}):\n"
                f"{_lines(matches, criteria)}")
    else:
        body = f"{opening}\n\nNiets in de voorraad dat aan je eisen voldoet."
    return title, body


def announce(new: list[dict], matches: list[dict], criteria: Criteria,
             total: int, first_run: bool = False, dry_run: bool = False) -> None:
    title, body = compose(new, matches, criteria, total, first_run)

    if dry_run:
        print(f"\n[dry-run] melding: {title}\n{body}")
        return

    if not notify_macos(title, body):
        print("(geen macOS-melding verstuurd)", file=sys.stderr)

    topic = _env("NTFY_TOPIC")
    if topic:
        # Klik gaat naar de goedkoopste auto die aan je eisen voldoet, anders
        # naar de eerste nieuwe.
        target = (matches or new or [None])[0]
        click = listing_url(target, criteria) if target else ""
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


def write_overview(matches: list[dict], criteria: Criteria, total: int) -> None:
    """Full list of currently matching cars — the alert only shows the first few."""
    RESULTS_DIR.mkdir(exist_ok=True)
    lines = [
        f"Overzicht van {time.strftime('%Y-%m-%d %H:%M')}",
        f"Voorraad in beeld : {total} auto's",
        f"Jouw eisen        : {criteria.describe()}",
        f"Voldoet daaraan   : {len(matches)}",
        "",
    ]
    for v in matches:
        lines.append(summarize(v, criteria))
        lines.append(f"    {listing_url(v, criteria)}")
    OVERVIEW_FILE.write_text("\n".join(lines) + "\n")


# ── Run ───────────────────────────────────────────────────────────────


async def fetch_vehicles(criteria: Criteria) -> list[dict]:
    """Fetch the whole scope — no year filter, so anything new is spotted."""
    cookies = await CookieManager().acquire(
        model=criteria.model, condition=criteria.condition
    )
    client = InventoryClient(cookies)
    _, vehicles = client.fetch_top_n(
        model=criteria.model,
        condition=criteria.condition,
        n=criteria.top_n,
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
    matches: list[dict] = []
    reasons: dict[str, int] = {}
    for v in vehicles:
        reason = reject_reason(v, criteria)
        if reason is None:
            matches.append(v)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    state = load_state()
    seen = state["seen"]
    # A v1 state only held matching cars, so treating it as "already seen"
    # would hide everything else forever; seed instead of flooding.
    first_run = state.get("version") != _STATE_VERSION or not seen

    new = [v for v in vehicles if v.get("VIN") and v["VIN"] not in seen]

    print(f"Voorraad  : {len(vehicles)} auto's ({criteria.model.upper()} "
          f"{criteria.condition})")
    print(f"Jouw eisen: {criteria.describe()}")
    print(f"Voldoet   : {len(matches)}")
    if reasons:
        print("Afgevallen: " + ", ".join(f"{n}x {r}" for r, n in sorted(reasons.items())))
    print(f"Nieuw     : {'eerste run — voorraad wordt vastgelegd' if first_run else len(new)}")

    if not first_run:
        for v in new:
            print(f"  • {summarize(v, criteria)}")
    for v in matches:
        marker = "NIEUW " if any(v is n for n in new) and not first_run else ""
        print(f"  ✓ {marker}{summarize(v, criteria)}")
        print(f"    {listing_url(v, criteria)}")

    now = time.strftime("%Y-%m-%d %H:%M")
    for v in vehicles:
        vin = v.get("VIN")
        if vin:
            seen[vin] = {"last_seen": now,
                         "price": v.get("TotalPrice") or v.get("Price")}
    state["version"] = _STATE_VERSION

    if not dry_run:
        write_overview(matches, criteria, len(vehicles))
        record_matches([v for v in matches if v in new] if not first_run else matches,
                       criteria)
        save_state(state)
        print(f"Overzicht : {OVERVIEW_FILE}")

    if first_run or new:
        announce(new, matches, criteria, total=len(vehicles),
                 first_run=first_run, dry_run=dry_run)
    else:
        print("Geen melding — er is niets bijgekomen.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="niets opslaan, geen melding versturen")
    parser.add_argument("--explain", action="store_true",
                        help="toon de optievelden van de eerste auto's")
    parser.add_argument("--from-file", metavar="PATH",
                        help="lees voertuigen uit een eerder opgeslagen JSON-bestand")
    parser.add_argument("--test-notify", action="store_true",
                        help="stuur een testmelding en stop")
    args = parser.parse_args(argv)

    criteria = Criteria.from_env()

    if args.test_notify:
        ok_mac = notify_macos("Tesla-wachter", "Testmelding — dit werkt.")
        print(f"macOS-melding: {'verstuurd' if ok_mac else 'MISLUKT'}")
        topic = _env("NTFY_TOPIC")
        if topic:
            ok_push = notify_ntfy(topic, "Tesla-wachter", "Testmelding — dit werkt.")
            print(f"Push naar {topic!r}: {'verstuurd' if ok_push else 'MISLUKT'}")
        else:
            print("NTFY_TOPIC niet gezet — geen push getest")
        return 0

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
