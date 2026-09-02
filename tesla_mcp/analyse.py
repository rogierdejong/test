"""Analyse results/historie.csv — prijsniveaus, ritme en prijsbewegingen.

    uv run python -m tesla_mcp.analyse
    uv run python -m tesla_mcp.analyse --dagen 14
    uv run python -m tesla_mcp.analyse --bestand ~/backup/historie.csv

Leest de gebeurtenissenlog die de wachter bijhoudt en beantwoordt de vragen
waarvoor die log bedoeld is: wat kost welk type, wanneer past Tesla prijzen aan,
hoe hard zakken ze, en wat gebeurt er vlak voor een auto verdwijnt.

Alles is beschrijvend. Er wordt niets voorspeld en niets gladgestreken; bij
kleine groepen staat het aantal er altijd bij, zodat je zelf kunt zien hoeveel
gewicht een getal kan dragen.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
HISTORY_CSV = RESULTS_DIR / "historie.csv"

_TIJD = "%Y-%m-%d %H:%M"


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tijd(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip()[:16], _TIJD)
    except (TypeError, ValueError, AttributeError):
        return None


def laad(path: Path, dagen: int = 0) -> list[dict]:
    """Lees de historie, oudste eerst, eventueel beperkt tot de laatste N dagen."""
    if not path.exists():
        return []
    rows = [r for r in csv.DictReader(path.open()) if r.get("VIN") and r.get("datum")]
    rows.sort(key=lambda r: r["datum"])
    if dagen and rows:
        laatste = _tijd(rows[-1]["datum"])
        if laatste:
            grens = laatste - timedelta(days=dagen)
            rows = [r for r in rows if (_tijd(r["datum"]) or laatste) >= grens]
    return rows


def laatste_per_auto(rows: list[dict], inclusief_verkocht: bool = False) -> list[dict]:
    """De meest recente regel per auto — standaard alleen auto's die nog staan."""
    laatste: dict[str, dict] = {}
    for r in rows:
        laatste[r["VIN"]] = r
    return [r for r in laatste.values()
            if inclusief_verkocht or r["event"] != "verkocht"]


# ── Ritme ─────────────────────────────────────────────────────────────


def ronden(rows: list[dict]) -> list[dict]:
    """Per ophaalronde: wat er gebeurde, en hoe lang het gat ervoor was."""
    per_ronde: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        per_ronde[r["datum"]].append(r)

    uit: list[dict] = []
    vorige: datetime | None = None
    for datum in sorted(per_ronde):
        rs = per_ronde[datum]
        deltas = [_int(r["verschil"]) for r in rs if r["event"] == "prijswijziging"]
        deltas = [d for d in deltas if d is not None]
        nu = _tijd(datum)
        gat = (nu - vorige).total_seconds() / 3600 if nu and vorige else None
        uit.append({
            "datum": datum,
            "gat_uren": gat,
            "nieuw": sum(1 for r in rs if r["event"] == "nieuw"),
            "verkocht": sum(1 for r in rs if r["event"] == "verkocht"),
            "wijzigingen": len(deltas),
            "mediaan": st.median(deltas) if deltas else None,
            "dalers": sum(1 for d in deltas if d < 0),
            "stijgers": sum(1 for d in deltas if d > 0),
        })
        vorige = nu or vorige
    return uit


def per_uur(rows: list[dict]) -> list[dict]:
    """Prijswijzigingen naar tijdstip van de dag — laat een herprijsvenster zien."""
    emmers: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["event"] != "prijswijziging":
            continue
        delta = _int(r["verschil"])
        if delta is not None:
            emmers[r["datum"][11:13]].append(delta)
    return [{"uur": u, "aantal": len(d), "mediaan": st.median(d)}
            for u, d in sorted(emmers.items())]


def per_dag(rows: list[dict]) -> list[dict]:
    emmers: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["event"] != "prijswijziging":
            continue
        delta = _int(r["verschil"])
        if delta is not None:
            emmers[r["datum"][:10]].append(delta)
    return [{"dag": d, "aantal": len(v), "mediaan": st.median(v),
             "dalers": sum(1 for x in v if x < 0),
             "stijgers": sum(1 for x in v if x > 0)}
            for d, v in sorted(emmers.items())]


# ── Prijsniveaus ──────────────────────────────────────────────────────


def prijs_per_type(rows: list[dict]) -> list[dict]:
    groepen: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for r in laatste_per_auto(rows):
        prijs, km = _int(r["prijs"]), _int(r["km"])
        if prijs:
            groepen[(r["jaar"], r["uitvoering"])].append((prijs, km or 0))

    uit = []
    for (jaar, uitvoering), waarden in sorted(groepen.items()):
        prijzen = [p for p, _ in waarden]
        kms = [k for _, k in waarden if k]
        uit.append({
            "jaar": jaar, "uitvoering": uitvoering, "aantal": len(prijzen),
            "mediaan": st.median(prijzen), "min": min(prijzen), "max": max(prijzen),
            "mediaan_km": st.median(kms) if kms else None,
            "helling": helling_km(waarden),
        })
    return uit


def helling_km(waarden: list[tuple[int, int]], minimum: int = 5) -> float | None:
    """Euro per 10.000 km binnen een groep, via kleinste kwadraten.

    None bij te weinig auto's of te weinig spreiding — een helling door drie
    punten zegt niets.
    """
    punten = [(k, p) for p, k in waarden if k]
    if len(punten) < minimum:
        return None
    gem_km = st.mean(k for k, _ in punten)
    noemer = sum((k - gem_km) ** 2 for k, _ in punten)
    if not noemer:
        return None
    gem_p = st.mean(p for _, p in punten)
    helling = sum((k - gem_km) * (p - gem_p) for k, p in punten) / noemer
    return helling * 10000


def kleuren(rows: list[dict]) -> list[tuple[str, int]]:
    telling = Counter(r["kleur"] or "onbekend" for r in laatste_per_auto(rows))
    return telling.most_common()


# ── Beweging per auto en verkopen ─────────────────────────────────────


def verloop(rows: list[dict]) -> list[dict]:
    """Netto prijsverloop per auto over de gemeten periode."""
    stappen: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["event"] == "prijswijziging" and _int(r["verschil"]) is not None:
            stappen[r["VIN"]].append(r)

    uit = []
    for vin, rs in stappen.items():
        start, eind = _int(rs[0]["vorige_prijs"]), _int(rs[-1]["prijs"])
        if not start or not eind:
            continue
        uit.append({
            "VIN": vin, "jaar": rs[-1]["jaar"], "kleur": rs[-1]["kleur"],
            "van": start, "naar": eind, "verschil": eind - start,
            "procent": (eind - start) / start * 100, "stappen": len(rs),
            "voldoet": rs[-1]["voldoet_aan_eisen"],
        })
    return sorted(uit, key=lambda x: x["verschil"])


def verkopen(rows: list[dict]) -> list[dict]:
    geschiedenis: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        geschiedenis[r["VIN"]].append(r)

    uit = []
    for r in rows:
        if r["event"] != "verkocht":
            continue
        eerder = [x for x in geschiedenis[r["VIN"]]
                  if x["event"] == "prijswijziging" and x["datum"] < r["datum"]]
        laatste_stap = _int(eerder[-1]["verschil"]) if eerder else None
        uit.append({
            "VIN": r["VIN"], "datum": r["datum"], "jaar": r["jaar"],
            "uitvoering": r["uitvoering"], "kleur": r["kleur"],
            "prijs": _int(r["prijs"]), "km": _int(r["km"]),
            "laatste_stap": laatste_stap,
            "dagen": _int(r["dagen_in_voorraad"]),
        })
    return uit


# ── Weergave ──────────────────────────────────────────────────────────


def _euro(bedrag) -> str:
    if bedrag is None:
        return "-"
    return f"€{bedrag:,.0f}".replace(",", ".")


def _getal(waarde) -> str:
    if waarde is None:
        return "-"
    return f"{waarde:,.0f}".replace(",", ".")


def rapport(rows: list[dict]) -> None:
    if not rows:
        print("Geen historie gevonden. De wachter vult results/historie.csv "
              "zodra hij een ronde heeft gedraaid.")
        return

    events = Counter(r["event"] for r in rows)
    print(f"Periode   : {rows[0]['datum']} → {rows[-1]['datum']}")
    print(f"Auto's    : {len({r['VIN'] for r in rows})} gezien, "
          f"{len(laatste_per_auto(rows))} nu in de voorraad")
    print("Events    : " + ", ".join(f"{n}x {e}" for e, n in events.most_common()))

    print("\n=== Wanneer past Tesla prijzen aan? ===")
    uren = per_uur(rows)
    totaal = sum(u["aantal"] for u in uren)
    for u in uren:
        aandeel = u["aantal"] / totaal * 100 if totaal else 0
        print(f"  {u['uur']}:xx   {u['aantal']:>4} wijzigingen ({aandeel:>4.0f}%)   "
              f"mediaan {u['mediaan']:+.0f}")
    print("  Let op het gat ervoor: veel wijzigingen na een kort nachtelijk gat en")
    print("  geen na een lang gat overdag wijst op een herprijzing 's nachts.")

    print("\n=== Per ophaalronde ===")
    print(f"  {'ronde':<18}{'gat':>6}{'nieuw':>7}{'prijs':>7}{'verkocht':>10}"
          f"{'mediaan':>10}{'dalers':>8}{'stijgers':>10}")
    for r in ronden(rows):
        gat = f"{r['gat_uren']:.0f}u" if r["gat_uren"] is not None else "-"
        med = f"{r['mediaan']:+.0f}" if r["mediaan"] is not None else "-"
        print(f"  {r['datum']:<18}{gat:>6}{r['nieuw']:>7}{r['wijzigingen']:>7}"
              f"{r['verkocht']:>10}{med:>10}{r['dalers']:>8}{r['stijgers']:>10}")

    print("\n=== Prijsbeweging per dag ===")
    for d in per_dag(rows):
        print(f"  {d['dag']}   {d['aantal']:>3} aangepast   mediaan {d['mediaan']:+7.0f}"
              f"   {d['dalers']} omlaag / {d['stijgers']} omhoog")

    print("\n=== Prijs per type (auto's die nu in de voorraad staan) ===")
    print(f"  {'jaar':<6}{'uitvoering':<30}{'n':>3}{'mediaan':>10}{'min':>10}"
          f"{'max':>10}{'med. km':>10}{'per 10.000 km':>16}")
    for g in prijs_per_type(rows):
        helling = f"{g['helling']:+,.0f}".replace(",", ".") if g["helling"] else "-"
        print(f"  {g['jaar']:<6}{g['uitvoering'][:29]:<30}{g['aantal']:>3}"
              f"{_euro(g['mediaan']):>10}{_euro(g['min']):>10}{_euro(g['max']):>10}"
              f"{_getal(g['mediaan_km']):>10}{helling:>16}")
    print("  De helling is een ruwe kleinste-kwadratenlijn door de auto's in die")
    print("  groep, en wordt pas getoond vanaf vijf auto's.")

    print("\n=== Kleurverdeling ===")
    kleur_lijst = kleuren(rows)
    totaal_kleur = sum(n for _, n in kleur_lijst)
    for kleur, n in kleur_lijst:
        print(f"  {kleur:<10}{n:>3}  ({n / totaal_kleur * 100:>4.0f}%)")

    verkocht = verkopen(rows)
    if verkocht:
        print("\n=== Verdwenen uit de voorraad ===")
        for v in verkocht:
            stap = f"{v['laatste_stap']:+d}" if v["laatste_stap"] is not None else "geen gezien"
            dagen = f", {v['dagen']} dagen te koop" if v["dagen"] is not None else ""
            print(f"  {v['datum'][:10]}  {v['VIN']}  {v['jaar']} {v['kleur']:<7}"
                  f"{_euro(v['prijs']):>9}  {_getal(v['km'])} km  "
                  f"laatste stap: {stap}{dagen}")
        stappen = [v["laatste_stap"] for v in verkocht if v["laatste_stap"] is not None]
        if stappen:
            omlaag = sum(1 for s in stappen if s < 0)
            print(f"  → bij {omlaag} van de {len(stappen)} was de laatste stap een "
                  "verlaging")

    print("\n=== Netto verloop per auto ===")
    for v in verloop(rows):
        ster = "★ " if v["voldoet"] == "ja" else "  "
        print(f"  {ster}{v['VIN']}  {v['jaar']} {v['kleur']:<7}"
              f"{_euro(v['van']):>9} → {_euro(v['naar']):<9}"
              f"{v['verschil']:>+7} ({v['procent']:+5.1f}%) in {v['stappen']} stappen")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bestand", default=str(HISTORY_CSV),
                        help="pad naar historie.csv")
    parser.add_argument("--dagen", type=int, default=0,
                        help="alleen de laatste N dagen meenemen")
    args = parser.parse_args(argv)

    rapport(laad(Path(args.bestand).expanduser(), dagen=args.dagen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
