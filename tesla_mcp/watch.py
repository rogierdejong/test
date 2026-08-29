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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tesla_mcp.config import REGION
from tesla_mcp.scraper import CookieManager, InventoryClient

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
STATE_FILE = RESULTS_DIR / "watch_state.json"
MATCHES_CSV = RESULTS_DIR / "matches.csv"
OVERVIEW_FILE = RESULTS_DIR / "overzicht.txt"
HISTORY_CSV = RESULTS_DIR / "historie.csv"

# v1 tracked only cars meeting the criteria; v2 the whole scope, so anything
# new could be reported; v3 keeps a snapshot per car so a disappearance can be
# logged as sold with its last known price.
_STATE_VERSION = 3

# One row per event, for later analysis in a spreadsheet.
_HISTORY_FIELDS = [
    "datum", "event", "VIN", "jaar", "model", "uitvoering",
    "prijs", "vorige_prijs", "verschil", "km", "kleur", "plaats",
    "voldoet_aan_eisen", "eerst_gezien", "dagen_in_voorraad", "url",
]

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
    """Deep link to this car's listing on the localised site.

    redirect=no keeps Tesla on the inventory listing instead of bouncing to a
    fresh configurator when the car is gone.
    """
    vin = vehicle.get("VIN", "")
    model = (vehicle.get("Model") or criteria.model or "my").lower()
    prefix = f"/{REGION.locale}" if REGION.locale else ""
    return f"https://www.tesla.com{prefix}/{model}/order/{vin}?redirect=no"


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


def notify_macos(title: str, body: str, url: str = "") -> bool:
    if platform.system() != "Darwin":
        return False

    # A notification posted by osascript cannot carry a link. terminal-notifier
    # can, so when it is installed the notification itself opens the listing.
    notifier = shutil.which("terminal-notifier")
    if notifier and url:
        try:
            result = subprocess.run(
                [notifier, "-title", title, "-message", body.replace("\n", " · "),
                 "-open", url],
                timeout=15, capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass  # val terug op osascript
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


def notify_ntfy(topic: str, title: str, body: str, click: str = "",
                actions: str = "") -> bool:
    """Push to a phone via ntfy.sh.

    The topic name is the only secret: anyone who knows it can read these
    messages, so use something unguessable.
    """
    if not topic:
        return False
    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    request.add_header("Title", _header_value(title))
    request.add_header("Tags", "car")
    request.add_header("Priority", "default")
    if click:
        request.add_header("Click", click)
    if actions:
        request.add_header("Actions", _header_value(actions))
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"ntfy mislukt: {exc}", file=sys.stderr)
        return False


def _lines(vehicles: list[dict], criteria: Criteria, limit: int = 5,
           with_links: bool = True) -> str:
    shown: list[str] = []
    for v in vehicles[:limit]:
        shown.append(f"• {summarize(v, criteria)}")
        if with_links and v.get("VIN"):
            shown.append(f"  {listing_url(v, criteria)}")
    if len(vehicles) > limit:
        shown.append(f"… en {len(vehicles) - limit} meer")
    return "\n".join(shown)


def _header_value(text: str) -> str:
    """Send UTF-8 through an HTTP header.

    urllib encodes header strings as latin-1, so hand it the UTF-8 bytes
    reinterpreted as latin-1: what goes over the wire is then correct UTF-8,
    which is what ntfy expects.
    """
    return text.encode("utf-8").decode("latin-1", "replace")


def ntfy_actions(vehicles: list[dict], criteria: Criteria, limit: int = 3) -> str:
    """Tap-through buttons for the top cars, as ntfy's Actions header.

    Labels stay free of commas and semicolons — those separate the fields of
    that header.
    """
    actions = []
    for v in vehicles[:limit]:
        if not v.get("VIN"):
            continue
        paint = (paint_values(v) or ["?"])[0].title()
        price = v.get("TotalPrice") or v.get("Price") or 0
        label = f"{v.get('Year', '')} {paint}".strip()
        if price:
            label += f" {price // 1000}k"
        label = label.replace(",", " ").replace(";", " ")
        actions.append(f"view, {label}, {listing_url(v, criteria)}")
    return "; ".join(actions)


def _lines_marked(vehicles: list[dict], matches: list[dict], criteria: Criteria,
                  limit: int = 5) -> str:
    """New cars, with a star on the ones that already meet the criteria."""
    match_vins = {m.get("VIN") for m in matches}
    shown: list[str] = []
    for v in vehicles[:limit]:
        star = "★ " if v.get("VIN") in match_vins else ""
        shown.append(f"• {star}{summarize(v, criteria)}")
        if v.get("VIN"):
            shown.append(f"  {listing_url(v, criteria)}")
    if len(vehicles) > limit:
        shown.append(f"… en {len(vehicles) - limit} meer")
    return "\n".join(shown)


def _price_lines(changes: list[tuple], criteria: Criteria, limit: int = 5) -> str:
    shown: list[str] = []
    for vehicle, old_price, new_price in changes[:limit]:
        delta = new_price - old_price
        arrow = "▼" if delta < 0 else "▲"
        old_text = f"€{old_price:,.0f}".replace(",", ".")
        delta_text = f"€{abs(delta):,.0f}".replace(",", ".")
        shown.append(f"• {arrow} {delta_text} (was {old_text}) — "
                     f"{summarize(vehicle, criteria)}")
        if vehicle.get("VIN"):
            shown.append(f"  {listing_url(vehicle, criteria)}")
    if len(changes) > limit:
        shown.append(f"… en {len(changes) - limit} meer")
    return "\n".join(shown)


def compose(new: list[dict], matches: list[dict], criteria: Criteria,
            total: int, first_run: bool = False,
            price_changes: list[tuple] | None = None) -> tuple[str, str]:
    """Title and body for one alert.

    Everything new leads; what meets the criteria follows as the highlight.
    """
    price_changes = price_changes or []

    if first_run:
        title = f"Wachter gestart — {total} auto's in beeld"
        sections = [f"{total} auto's in de voorraad. Vanaf nu krijg je bericht "
                    "zodra er eentje bijkomt of van prijs verandert."]
    else:
        parts = []
        if new:
            parts.append(f"{len(new)} nieuw")
        if price_changes:
            parts.append(f"{len(price_changes)} prijswijziging"
                         + ("" if len(price_changes) == 1 else "en"))
        title = " · ".join(parts) or "Voorraad bijgewerkt"
        if matches:
            title += (f" — {len(matches)} voldoet aan je eisen" if len(matches) == 1
                      else f" — {len(matches)} voldoen aan je eisen")

        sections = []
        if new:
            sections.append("Nieuw in de voorraad:\n"
                            + _lines_marked(new, matches, criteria))
        if price_changes:
            sections.append("Prijs gewijzigd:\n" + _price_lines(price_changes, criteria))

    if matches:
        sections.append(f"Voldoet aan je eisen ({criteria.describe()}):\n"
                        + _lines(matches, criteria))
    else:
        sections.append("Niets in de voorraad dat aan je eisen voldoet.")

    return title, "\n\n".join(sections)


def announce(new: list[dict], matches: list[dict], criteria: Criteria,
             total: int, first_run: bool = False, dry_run: bool = False,
             price_changes: list[tuple] | None = None) -> None:
    title, body = compose(new, matches, criteria, total, first_run, price_changes)

    if dry_run:
        print(f"\n[dry-run] melding: {title}\n{body}")
        return

    # Klik gaat naar de goedkoopste auto die aan je eisen voldoet, anders naar
    # de eerste nieuwe; de knoppen dekken de eerste drie.
    highlight = matches or new
    click = listing_url(highlight[0], criteria) if highlight else ""

    if not notify_macos(title, body, click):
        print("(geen macOS-melding verstuurd)", file=sys.stderr)

    topic = _env("NTFY_TOPIC")
    if topic:
        actions = ntfy_actions(highlight, criteria)
        if notify_ntfy(topic, title, body, click, actions):
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


def snapshot(vehicle: dict, criteria: Criteria, matched: bool) -> dict:
    """The few fields worth keeping after a car leaves the inventory."""
    return {
        "jaar": vehicle.get("Year"),
        "model": (vehicle.get("Model") or criteria.model).lower(),
        "uitvoering": vehicle.get("TrimName"),
        "prijs": vehicle.get("TotalPrice") or vehicle.get("Price"),
        "km": vehicle.get("Odometer"),
        "kleur": ", ".join(paint_values(vehicle)),
        "plaats": vehicle.get("City") or vehicle.get("MetroName"),
        "url": listing_url(vehicle, criteria),
        "voldoet_aan_eisen": "ja" if matched else "nee",
    }


def _history_row(event: str, vin: str, snap: dict, when: str,
                 first_seen: str = "", previous_price=None) -> dict:
    price = snap.get("prijs")
    row = {f: "" for f in _HISTORY_FIELDS}
    row.update({k: v for k, v in snap.items() if k in _HISTORY_FIELDS})
    row.update({"datum": when, "event": event, "VIN": vin, "prijs": price or ""})

    if previous_price is not None:
        row["vorige_prijs"] = previous_price
        if price:
            row["verschil"] = price - previous_price

    if first_seen:
        row["eerst_gezien"] = first_seen
        try:
            start = datetime.strptime(first_seen[:10], "%Y-%m-%d")
            row["dagen_in_voorraad"] = (datetime.strptime(when[:10], "%Y-%m-%d") - start).days
        except ValueError:
            pass
    return row


def append_history(rows: list[dict]) -> None:
    """Append events to results/historie.csv — the file to run analyses on."""
    if not rows:
        return
    RESULTS_DIR.mkdir(exist_ok=True)
    exists = HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_HISTORY_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


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
    """Fetch the whole scope — no year filter, so anything new is spotted.

    Akamai answers a burst of requests with 403 or 429. Fresh cookies usually
    settle it, so try that once before giving up.
    """
    manager = CookieManager()

    async def _fetch() -> list[dict]:
        cookies = await manager.acquire(model=criteria.model,
                                        condition=criteria.condition)
        client = InventoryClient(cookies)
        _, vehicles = client.fetch_top_n(
            model=criteria.model, condition=criteria.condition, n=criteria.top_n,
        )
        return vehicles

    try:
        return await _fetch()
    except RuntimeError as exc:
        print(f"Ophalen mislukt ({exc}); cookies verversen en één keer opnieuw...",
              file=sys.stderr)
        manager.invalidate()
        await asyncio.sleep(5)
        return await _fetch()


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
    match_vins = {m.get("VIN") for m in matches}

    state = load_state()
    seen = state["seen"]
    first_run = state.get("version") != _STATE_VERSION or not seen

    now = time.strftime("%Y-%m-%d %H:%M")
    current = {v["VIN"]: v for v in vehicles if v.get("VIN")}
    events: list[dict] = []

    new = [v for vin, v in current.items() if vin not in seen]

    # Price moves on cars we already knew.
    price_changes: list[tuple] = []
    for vin, v in current.items():
        before = seen.get(vin)
        if not before or before.get("status", "actief") != "actief":
            continue
        old_price, now_price = before.get("price"), v.get("TotalPrice") or v.get("Price")
        if old_price and now_price and old_price != now_price:
            price_changes.append((v, old_price, now_price))

    # A car that vanished has been sold — but only trust that when neither this
    # fetch nor the previous one hit the page cap. A capped fetch is a
    # truncated list, so cars can drop out of view while still being for sale.
    complete = len(vehicles) < criteria.top_n
    active_before = sum(1 for e in seen.values()
                        if e.get("status", "actief") == "actief")
    # Een lege of gehalveerde lijst betekent bijna altijd een geblokkeerde of
    # afgebroken ophaalronde — niet dat de voorraad is leeggekocht.
    plausible = bool(vehicles) and (
        not active_before or len(current) >= active_before / 2
    )
    trust_sold = complete and plausible and not state.get("last_fetch_capped", False)
    sold: list[tuple[str, dict]] = []
    if trust_sold and not first_run:
        sold = [(vin, entry) for vin, entry in seen.items()
                if entry.get("status", "actief") == "actief" and vin not in current]

    # ── Console ──
    print(f"Voorraad  : {len(vehicles)} auto's ({criteria.model.upper()} "
          f"{criteria.condition})")
    print(f"Jouw eisen: {criteria.describe()}")
    print(f"Voldoet   : {len(matches)}")
    if reasons:
        print("Afgevallen: " + ", ".join(f"{n}x {r}" for r, n in sorted(reasons.items())))
    print(f"Nieuw     : {'eerste run — voorraad wordt vastgelegd' if first_run else len(new)}")
    if not first_run:
        for v in new:
            star = "★ " if v.get("VIN") in match_vins else ""
            print(f"  • {star}{summarize(v, criteria)}")
            print(f"    {listing_url(v, criteria)}")
        if price_changes:
            print(f"Prijs     : {len(price_changes)} gewijzigd")
            for v, old_price, now_price in price_changes:
                arrow = "▼" if now_price < old_price else "▲"
                print(f"  {arrow} {old_price} → {now_price}  {summarize(v, criteria)}")
        if sold:
            print(f"Verdwenen : {len(sold)} (als verkocht vastgelegd)")
        elif not plausible:
            print(f"Verdwenen : niet bepaald — {len(current)} auto's opgehaald "
                  f"tegen {active_before} bekende; dat wijst op een mislukte "
                  "ophaalronde, niet op verkoop")
        elif not trust_sold:
            print("Verdwenen : niet bepaald — de voorraad raakte de paginalimiet, "
                  "dus een auto kan uit beeld vallen zonder verkocht te zijn")

    # ── Historie ──
    for v in new:
        vin = v["VIN"]
        snap = snapshot(v, criteria, vin in match_vins)
        events.append(_history_row("nieuw", vin, snap, now))
        seen[vin] = {"first_seen": now, "last_seen": now, "price": snap["prijs"],
                     "status": "actief", "snapshot": snap}

    for v, old_price, now_price in price_changes:
        vin = v["VIN"]
        snap = snapshot(v, criteria, vin in match_vins)
        events.append(_history_row("prijswijziging", vin, snap, now,
                                   first_seen=seen[vin].get("first_seen", ""),
                                   previous_price=old_price))

    for vin, entry in sold:
        snap = entry.get("snapshot") or {"prijs": entry.get("price")}
        events.append(_history_row("verkocht", vin, snap, now,
                                   first_seen=entry.get("first_seen", "")))
        entry["status"] = "verkocht"
        entry["sold_on"] = now

    # Cars still listed: refresh price and snapshot.
    for vin, v in current.items():
        snap = snapshot(v, criteria, vin in match_vins)
        entry = seen.setdefault(vin, {"first_seen": now})
        entry.update({"last_seen": now, "price": snap["prijs"],
                      "status": "actief", "snapshot": snap})
    state["version"] = _STATE_VERSION
    state["last_fetch_capped"] = not complete

    if not dry_run:
        write_overview(matches, criteria, len(vehicles))
        append_history(events)
        record_matches(matches if first_run else [v for v in new if v in matches],
                       criteria)
        save_state(state)
        print(f"Overzicht : {OVERVIEW_FILE}")
        if events:
            print(f"Historie  : {len(events)} regel(s) bij in {HISTORY_CSV}")

    # Price moves are only worth a push when they touch a car you want.
    match_price_changes = [c for c in price_changes if c[0].get("VIN") in match_vins]

    if first_run or new or match_price_changes:
        announce(new, matches, criteria, total=len(vehicles), first_run=first_run,
                 dry_run=dry_run, price_changes=match_price_changes)
    else:
        print("Geen melding — niets nieuws en geen prijswijziging binnen je eisen.")
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
        demo_url = f"https://www.tesla.com/{REGION.locale}/inventory/used/my"
        ok_mac = notify_macos("Tesla-wachter", "Testmelding — dit werkt.", demo_url)
        print(f"macOS-melding: {'verstuurd' if ok_mac else 'MISLUKT'}")
        topic = _env("NTFY_TOPIC")
        if topic:
            ok_push = notify_ntfy(
                topic, "Tesla-wachter",
                f"Testmelding — dit werkt.\n{demo_url}", demo_url,
                f"view, Voorraad bekijken, {demo_url}",
            )
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
